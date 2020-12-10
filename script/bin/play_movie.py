#!/usr/bin/env python3
import sys
import pickle
import matplotlib.pyplot as plt
from matplotlib.animation import FuncAnimation, writers
from mpl_toolkits.mplot3d import Axes3D

if len(sys.argv) < 2:
    exit("fishpy-play Filename [Save name] [Arrow Length] [Delay]")

with open(sys.argv[1], 'rb') as f:
    movie = pickle.load(f)

if len(sys.argv) == 3:
    save = sys.argv[2]
    length = 2
    delay = 10
elif len(sys.argv) == 4:
    save = False
    length = float(sys.argv[3])
    delay = 10
elif len(sys.argv) == 5:
    save = False
    length = float(sys.argv[3])
    delay = float(sys.argv[4])
else:
    save = False
    length = 2
    delay = 10


fig = plt.figure(figsize=(5, 5), tight_layout=True)
ax = fig.add_subplot(111, projection='3d')
ax.set_xlabel('X')
ax.set_ylabel('Y')
ax.set_zlabel('Z')

scatter = ax.scatter([], [], [], 'o', color='lightblue', edgecolor='teal')
quiver = ax.quiver3D([], [], [], [], [], [], color='teal')

def update(frame_num):
    global quiver
    global scatter
    frame = movie[frame_num]
    if len(frame) > 0:
        v = movie.velocity(frame_num)
        quiver.remove()
        scatter.remove()
        quiver = ax.quiver3D(*frame.T, *v.T, color='teal', length=length)
        scatter = ax.scatter(*frame.T, color='lightblue', edgecolor='teal', s=10)
    dummy = ax.plot([], [])[0]
    return [dummy]

ani = FuncAnimation(fig, update, frames=range(len(movie)), interval=delay)

if save:
    Writer = writers['ffmpeg']
    writer = Writer(fps=15, metadata=dict(artist='Me'), bitrate=1800)
    ani.save(save, writer=writer)

plt.show()
