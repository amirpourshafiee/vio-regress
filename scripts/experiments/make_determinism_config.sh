#!/usr/bin/env bash
# Variant C of the S3 variance experiment: the image's euroc_mav config with the two
# repeatability knobs the config exposes (it has no seed for real data; sim_seed_* only
# affect the simulator):
#   num_opencv_threads: 1         (was 4; serialises cv::parallel_for_ in feature tracking,
#                                  whose worker threads do not share cv::setRNGSeed(0))
#   multi_threading_pubs: false   (upstream: "uncomment if you want repeatability",
#                                  ov_msckf/src/ros1_serial_msckf.cpp:67)
#   usage: make_determinism_config.sh <out_dir> [image]
set -euo pipefail
OUT=${1:?out dir}; IMAGE=${2:-vioreg-openvins:v2.7}
mkdir -p "$OUT"
for f in estimator_config.yaml kalibr_imu_chain.yaml kalibr_imucam_chain.yaml; do
  docker run --rm --entrypoint cat "$IMAGE" "/ws/src/open_vins/config/euroc_mav/$f" > "$OUT/$f"
done
sed -i 's/^num_opencv_threads: 4 /num_opencv_threads: 1 /' "$OUT/estimator_config.yaml"
grep -q '^num_opencv_threads: 1 ' "$OUT/estimator_config.yaml"
printf '\n# vioreg S3 variant C: repeatability (see scripts/experiments/make_determinism_config.sh)\nmulti_threading_pubs: false\n' >> "$OUT/estimator_config.yaml"
echo "wrote $OUT"
