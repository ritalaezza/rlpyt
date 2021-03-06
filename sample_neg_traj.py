from rlpyt.envs.dm_control_env import DMControlEnv
import json
import math
import time
import os
from os.path import join, exists
import itertools
from tqdm import tqdm
import numpy as np
import imageio
import multiprocessing as mp

import sys
use_dr, init_flat = sys.argv[1], sys.argv[2]
use_dr = use_dr == 'True'
init_flat = init_flat == 'True'

mode = 'cloth'
n_samples = 250000
n_neg_samples = 0
max_path_length = 50
n_trajectories = math.ceil(n_samples / (max_path_length + 1 + max_path_length * n_neg_samples))
print(n_trajectories, 'trajectories', 'use_dr', use_dr, 'init_flat', init_flat)

if mode == 'rope':
    env_args = dict(
        domain='rope_dr',
        task='easy',
        max_path_length=max_path_length,
        pixel_wrapper_kwargs=dict(observation_key='pixels', pixels_only=False, # to not take away non pixel obs
                                  render_kwargs=dict(width=64, height=64, camera_id=0)),
        task_kwargs=dict(random_pick=True, use_dr=use_dr, init_flat=init_flat)
    )
elif mode == 'cloth':
    env_args = dict(
         domain='cloth_dr',
         task='easy',
         max_path_length=max_path_length,
         pixel_wrapper_kwargs=dict(observation_key='pixels', pixels_only=False,
                                   render_kwargs=dict(width=64, height=64, camera_id=0)),
         task_kwargs=dict(random_pick=True, use_dr=use_dr, init_flat=init_flat)
    )
elif mode == 'pointmass':
    env_args = dict(
        domain='point_mass_vel',
        task='easy',
        max_path_length=max_path_length,
        pixel_wrapper_kwargs=dict(observation_key='pixels', pixels_only=False,
                                  render_kwargs=dict(width=64, height=64, camera_id=0)),
    )
else:
    raise Exception('Invalid mode', mode)

def worker(worker_id, start, end):
    np.random.seed(worker_id+1)
    # Initialize environment
    env = DMControlEnv(**env_args)
    if worker_id == 0:
        pbar = tqdm(total=end - start)

    for i in range(start, end):
        str_i = str(i)
        run_folder = join(root, 'run{}'.format(str_i.zfill(5)))
        if not exists(run_folder):
            os.makedirs(run_folder)

        o = env.reset()
        imageio.imwrite(join(run_folder, 'img_{}_{}.png'.format('0'.zfill(2), '0'.zfill(3))), o.pixels.astype('uint8'))
        actions = []
        env_states = [env.get_state()]
        for t in itertools.count(start=1):
            saved_state = env.get_state(ignore_step=False)
            str_t = str(t)
            actions_t = []
            for k in range(n_neg_samples):
                str_k = str(k + 1) # start at 1

                a = env.action_space.sample()
                if mode == 'cloth' or mode == 'rope':
                    actions_t.append(np.concatenate((o.location[:2], a))) # need to add before b/c o is replaced
                else:
                    actions_t.append(a)
                o, _, terminal, info = env.step(a)

                imageio.imwrite(join(run_folder, 'img_{}_{}.png'.format(str_t.zfill(2), str_k.zfill(3))), o.pixels.astype('uint8'))

                env.set_state(saved_state, ignore_step=False)
                env.step(np.zeros(env.action_space.sample().shape))
                env._step_count -= 1
                o = env.get_obs()

            a = env.action_space.sample()
            if mode == 'cloth' or mode == 'rope':
                actions_t.insert(0, np.concatenate((o.location[:2], a)))
            else:
                actions_t.insert(0, a)
            o, _, terminal, info = env.step(a)
            env_states.append(env.get_state())

            imageio.imwrite(join(run_folder, 'img_{}_{}.png'.format(str_t.zfill(2), '0'.zfill(3))), o.pixels.astype('uint8'))

            actions.append(np.stack(actions_t, axis=0))
            if terminal or info.traj_done:
                break
        env_states = np.stack(env_states, axis=0)
        actions = np.stack(actions, axis=0)
        np.save(join(run_folder, 'actions.npy'), actions)
        np.save(join(run_folder, 'env_states.npy'), env_states)

        if worker_id == 0:
            pbar.update(1)
    if worker_id == 0:
        pbar.close()

if __name__ == '__main__':
    start = time.time()
    name = f'{mode}_flat_{init_flat}_dr_{use_dr}'
    root = join('data', name)
    if not exists(root):
        os.makedirs(root)

    with open(join(root, 'env_args.json'), 'w') as f:
        json.dump(env_args, f)

    n_chunks = mp.cpu_count()
    partition_size = math.ceil(n_trajectories / n_chunks)
    args_list = []
    for i in range(n_chunks):
        args_list.append((i, i * partition_size, min((i + 1) * partition_size, n_trajectories)))
    print('args', args_list)

    ps = [mp.Process(target=worker, args=args) for args in args_list]
    [p.start() for p in ps]
    [p.join() for p in ps]

    elapsed = time.time() - start
    print('Finished in {:.2f} min'.format(elapsed / 60))
