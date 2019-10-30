import numpy as np
import pickle
import fish_track as ft
import os
import sys


filename = sys.argv[1]
linker_name = sys.argv[2]

frame_start = int(sys.argv[3])
frame_end = int(sys.argv[4])
linker_range = float(sys.argv[5])
dx_max = int(sys.argv[6])
dt_max = int(sys.argv[7])
blur = int(sys.argv[8])
threshold = int(sys.argv[9])
save_folder = sys.argv[10]

frame_number = frame_end - frame_start

if f'vanilla_trajs.pkl' not in os.listdir(save_folder):
    frames = []
    with open(filename, 'rb') as f:
        for _ in range(frame_start):
            pickle.load(f)
        for _ in range(0, frame_number):
            frames.append(pickle.load(f))

    if linker_name.lower() == 'trackpy':
        linker = ft.TrackpyLinker(linker_range, 0)
    elif linker_name.lower() == 'active':
        linker = ft.ActiveLinker(linker_range)
    else:
        print("Invalid linker: ", linker_name)

    vanilla_trajs = linker.link(frames)
    vanilla_trajs = [t for t in vanilla_trajs if len(t['time']) > 1]

    with open(f'{save_folder}/vanilla_trajs.pkl', 'wb') as f:
              pickle.dump(vanilla_trajs, f)
else:
    with open(f'{save_folder}/vanilla_trajs.pkl', 'rb') as f:
        vanilla_trajs = pickle.load(f)

if len(vanilla_trajs) > 1:
    trajs = ft.relink(vanilla_trajs, 1, 1, blur=blur)
    for dt in range(1, dt_max + 1):
        for dx in range(1, dx_max + 1):
            trajs = ft.relink(trajs, dx, dt, blur=None)

    trajs = [t for t in trajs if len(t['time']) > threshold]
else:
    trajs = vanilla_trajs

with open(f'{save_folder}/trajectories.pkl', 'wb') as f:
          pickle.dump(trajs, f)