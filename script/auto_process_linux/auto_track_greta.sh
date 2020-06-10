#!/bin/bash
# request resources:
#PBS -N ft-3d-
#PBS -l nodes=1:ppn=1
#PBS -l walltime=72:00:00

if [ $PBS_O_WORKDIR ]; then
    cd $PBS_O_WORKDIR
fi

# retrieving parameters
source configure.sh

# create folders for 3D tracking
if [ ! -d "track_greta" ]; then
    mkdir track_greta
fi

cp $script_folder/track_greta/* track_greta

# fill the configuration file
cd track_greta

video_folder_escaped=$(echo $video_folder | sed -e 's~/[]./[]~\&~g')
calib_folder_escaped=$(echo $calib_folder | sed -e 's~/[]./[]~\&~g')
order_json_escaped=$(echo $order_json | sed -e 's~/[]./[]~\&~g')
sed -i'' "s~ORDERJSON~\.\./$order_json_escaped~" configure.ini
sed -i'' "s~CALIBRATIONFOLDER~\.\./$calib_folder_escaped~" configure.ini
sed -i'' "s~VIDEOFILECAM1~\.\./$video_folder_escaped/cam-1.mp4~" configure.ini
sed -i'' "s~VIDEOFILECAM2~\.\./$video_folder_escaped/cam-2.mp4~" configure.ini
sed -i'' "s~VIDEOFILECAM3~\.\./$video_folder_escaped/cam-3.mp4~" configure.ini

sed -i'' "s~CALIBRATIONFORMAT~$calib_format~" configure.ini
sed -i'' "s~GRIDSIZE~$grid_size~" configure.ini
sed -i'' "s~CORNERNUMBER~$corner_number~" configure.ini
sed -i'' "s~TRACK3DWANTPLOT~$track_3d_want_plot~" configure.ini
sed -i'' "s~INTERNALCAM1~\.\./$cam_1_internal~" configure.ini
sed -i'' "s~INTERNALCAM2~\.\./$cam_2_internal~" configure.ini
sed -i'' "s~INTERNALCAM3~\.\./$cam_3_internal~" configure.ini
sed -i'' "s~ORIENTATIONNUMBER~$track_2d_orientation_number~g" configure.ini

sed -i'' "s~GRETAFRAMESTART~$greta_frame_start~g" configure.ini
sed -i'' "s~GRETAFRAMEEND~$greta_frame_end~g" configure.ini
sed -i'' "s~GRETAWATERDEPTH~$greta_water_depth~g" configure.ini
sed -i'' "s~GRETATOL2D~$greta_tol_2d~g" configure.ini
sed -i'' "s~GRETASEARCHRANGE~$greta_search_range~g" configure.ini
sed -i'' "s~GRETATAU1~$greta_tau_1~g" configure.ini
sed -i'' "s~GRETATAU2~$greta_tau_2~g" configure.ini
sed -i'' "s~GRETAOVERLAPNUM~$greta_overlap_num~g" configure.ini
sed -i'' "s~GRETAOVERLAPRTOL~$greta_overlap_rtol~g" configure.ini
sed -i'' "s~GRETARELINKDX~$greta_relink_dx~g" configure.ini
sed -i'' "s~GRETARELINKDT~$greta_relink_dt~g" configure.ini

# Calibration, only calibrate if there is no calibrated camera file
if [ ! -e "cameras.pkl" ]; then
    echo "calibrating cameras"
    python3 calibration.py
fi

cd -
