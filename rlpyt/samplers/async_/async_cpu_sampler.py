
import time
import numpy as np
import multiprocessing as mp
import ctypes
import queue

from rlpyt.samplers.base import BaseSampler
from rlpyt.samplers.utils import build_samples_buffer, build_step_buffer
from rlpyt.samplers.parallel_worker import sampling_process
from rlpyt.samplers.cpu.collectors import EvalCollector
from rlpyt.utils.seed import make_seed
from rlpyt.utils.logging import logger
from rlpyt.agents.base import AgentInputs
from rlpyt.utils.collections import AttrDict


EVAL_TRAJ_CHECK = 0.1  # Seconds.


class AsyncCpuSampler(BaseSampler):

    ###########################################################################
    # Master runner methods.
    ###########################################################################

    def master_runner_initialize(self, agent, bootstrap_value=False,
            traj_info_kwargs=None, seed=None):
        self.seed = make_seed() if seed is None else seed
        # Construct an example of each kind of data that needs to be stored.
        env = self.EnvCls(**self.env_kwargs)
        agent.initialize(env.spaces, share_memory=True)  # Actual agent initialization, keep.
        _, samples_np, examples = build_samples_buffer(agent, env,
            self.batch_spec, bootstrap_value, agent_shared=True, env_shared=True,
            subprocess=False)  # Would like subprocess=True, but might hang?
        _, samples_np2, _ = build_samples_buffer(agent, env, self.batch_spec,
            bootstrap_value, agent_shared=True, env_shared=True, subprocess=False)
        env.close()
        del env
        if traj_info_kwargs:
            for k, v in traj_info_kwargs.items():
                setattr(self.TrajInfoCls, "_" + k, v)
        self.double_buffer = double_buffer = (samples_np, samples_np2)
        self.examples = examples
        self.agent = agent
        return double_buffer, examples

    ###########################################################################
    # Sampler runner methods (forked).
    ###########################################################################

    def sampler_process_initialize(self, affinity):
        n_worker = len(affinity["workers_cpus"])
        n_envs_list = [self.batch_spec.B // n_worker] * n_worker
        if not self.batch_spec.B % n_worker == 0:
            logger.log("WARNING: unequal number of envs per process, from "
                f"batch_B {self.batch_spec.B} and n_parallel {n_worker} "
                "(possible suboptimal speed).")
            for b in range(self.batch_spec.B % n_worker):
                n_envs_list[b] += 1

        if self.eval_n_envs > 0:
            eval_n_envs_per = max(1, self.eval_n_envs // len(n_envs_list))
            eval_n_envs = eval_n_envs_per * n_worker
            logger.log(f"Total parallel evaluation envs: {eval_n_envs}.")
            self.eval_max_T = 1 + int(self.eval_max_steps // eval_n_envs)
            self.eval_n_envs_per = eval_n_envs_per
        else:
            self.eval_n_envs_per = 0
            self.eval_max_T = 0

        ctrl = AttrDict(
            quit=mp.RawValue(ctypes.c_bool, False),
            barrier_in=mp.Barrier(n_server + n_worker + 1),
            barrier_out=mp.Barrier(n_server + n_worker + 1),
            do_eval=mp.RawValue(ctypes.c_bool, False),
            itr=mp.RawValue(ctypes.c_long, 0),
            j=mp.RawValue("i", 0),  # Double buffer index.
        )
        traj_infos_queue = mp.Queue()

        sync = AttrDict(
            step_blockers=[mp.Semaphore(0) for _ in range(n_worker)],
            act_waiters=[mp.Semaphore(0) for _ in range(n_worker)],
            stop_eval=mp.RawValue(ctypes.c_bool, False),
            j=self.ctrl.j,  # Copy into sync which passes to Collector.
        )
        step_buffer_pyt, step_buffer_np = build_step_buffer(self.examples,
            sum(n_envs_list))

        if self.eval_n_envs_per > 0:
            eval_n_envs = self.eval_n_envs_per * n_worker
            eval_step_buffer_pyt, eval_step_buffer_np = build_step_buffer(
                self.examples, eval_n_envs)
            self.eval_step_buffer_pyt = eval_step_buffer_pyt
            self.eval_step_buffer_np = eval_step_buffer_np
        else:
            eval_step_buffer_np = None

        common_kwargs = dict(
            EnvCls=self.EnvCls,
            env_kwargs=self.env_kwargs,
            agent=self.agent,
            batch_T=self.batch_spec.T,
            CollectorCls=self.CollectorCls,
            TrajInfoCls=self.TrajInfoCls,
            traj_infos_queue=traj_infos_queue,
            ctrl=self.ctrl,
            max_decorrelation_steps=self.max_decorrelation_steps,
            eval_n_envs=self.eval_n_envs_per,
            eval_CollectorCls=self.eval_CollectorCls or EvalCollector,
            eval_env_kwargs=self.eval_env_kwargs,
            eval_max_T=self.eval_max_T,
            global_B=self.batch_spec.B,
            )
        workers_kwargs = assemble_workers_kwargs(affinity, seed, double_buffer,
            n_envs_list, sync, eval_step_buffer_np)

        workers = [mp.Process(target=sampling_process,
            kwargs=dict(common_kwargs=common_kwargs, worker_kwargs=w_kwargs))
            for w_kwargs in workers_kwargs]
        for w in workers:
            w.start()

        self.workers = workers
        self.sync = sync
        self.mid_batch_reset = self.CollectorCls.mid_batch_reset
        self.ctrl = ctrl
        self.traj_infos_queue = traj_infos_queue

    def obtain_samples(self, itr, j):
        # sync shared memory?  maybe don't need to if optimizer did?
        self.ctrl.itr.value = itr
        self.ctrl.j.value = j  # Tell collectors which buffer to use.
        self.ctrl.barrier_in.wait()
        # Workers step environments and sample actions here.
        self.ctrl.barrier_out.wait()
        traj_infos = list()
        while True:
            try:
                traj_infos.append(self.traj_infos_queue.get(block=False))
            except queue.Empty:
                break
        return traj_infos

    def evaluate_agent(self, itr):
        self.ctrl.do_eval = True
        self.sync.stop_eval.value = False
        self.ctrl.barrier_in.wait()
        traj_infos = list()
        if self.eval_max_trajectories is not None:
            while True:
                time.sleep(EVAL_TRAJ_CHECK)
                while True:
                    try:
                        traj_infos.append(self.traj_infos_queue.get(block=False))
                    except queue.Empty:
                        break
                if len(traj_infos) >= self.eval_max_trajectories:
                    self.sync.stop_eval.value = True
                    logger.log("Evaluation reached max num trajectories "
                        f"({self.eval_max_trajectories}).")
                    break  # Stop possibly before workers reach max_T.
                if self.ctrl.barrier_out.parties - self.ctrl.barrier_out.n_waiting == 1:
                    logger.log("Evaluation reached max num time steps "
                        f"({self.eval_max_T}).")
                    break  # Workers reached max_T.
        self.ctrl.barrier_out.wait()
        while True:
            try:
                traj_infos.append(self.traj_infos_queue.get(block=False))
            except queue.Empty:
                break
        self.ctrl.do_eval.value = False
        return traj_infos

    def shutdown(self):
        self.ctrl.quit.value = True
        self.ctrl.barrier_in.wait()
        for s in self.workers:
            s.join()


def assemble_workers_kwargs(affinity, seed, double_buffer, n_envs_list, sync):
    workers_kwargs = list()
    i_env = 0
    for rank in range(len(affinity["workers_cpus"])):
        n_envs = n_envs_list[rank]
        slice_B = slice(i_env, i_env + n_envs)
        worker_kwargs = dict(
            rank=rank,
            env_ranks=list(range(i_env, i_env + n_envs)),
            seed=seed + rank,
            cpus=affinity["workers_cpus"][rank],
            n_envs=n_envs,
            samples_np=tuple(buf[:, slice_B] for buf in double_buffer),
            sync=sync,  # Actually common kwarg.
        )
        i_env += n_envs
        workers_kwargs.append(worker_kwargs)
    return workers_kwargs