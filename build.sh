export prefix=/usr/local
export PY=python3

cd fish_3d
make
cd ../fish_corr
make
cd ../fish_track
make
cd ..
