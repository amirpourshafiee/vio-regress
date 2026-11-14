#!/usr/bin/env bash
# S3b sensitivity sweep for ONE sequence: how far does ATE move under legitimate changes?
#   usage: [SETS="legit realtime bad"] scripts/experiments/sensitivity.sh <SEQ> [RUNS=3]
#   SEQ: MH_01_easy | V1_01_easy | MH_03_medium.  SETS default: "legit realtime" (S3b);
#   S4 adds MH_03_medium (SETS="legit bad") and the known-bad variants (SETS=bad).
# Every variant changes ONE thing vs the S3 serial baseline (image, data, other settings
# identical). Output: results/sensitivity/<variant>/<SEQ>/run<k>.* and <variant>/<SEQ>.json.
#   serial:   base | bag_start_<s> x2 | num_pts_{180,220} | fast_threshold_{18,22}
#             | gravity_mag_{9.80665,9.80}
#   realtime: rt_rate_{1.0,1.5,2.0}  (rate exists only for rosbag play; serial has none)
#   MH_01 only (S3b 1b): rt_rate_1.0 gets 5 runs; rt_load_rate_1.0 = 3 realtime runs with all
#             4 CPUs busy, to test whether realtime repeatability depends on timing.
#   bad (S4, plausible real regressions): bad_gravity_mag_9.0 | bad_num_pts_50 |
#             bad_imu_noise_discrete (accel+gyro noise density x sqrt(200) in kalibr_imu_chain)
set -uo pipefail
SEQ=${1:?usage: sensitivity.sh SEQ [RUNS]}; RUNS=${2:-3}
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"; cd "$ROOT"
source scripts/experiments/lib.sh
R=results/sensitivity; CFG=$R/configs
SETS=${SETS:-legit realtime}
case $SEQ in
  MH_01_easy)   STARTS="38 42";;     # upstream default 40
  V1_01_easy)   STARTS="1 2";;       # upstream default 0 (cannot go below)
  MH_03_medium) STARTS="15.5 19.5";; # upstream default 17.5
  *) echo "sensitivity sweep is defined for MH_01_easy, V1_01_easy, MH_03_medium" >&2; exit 1;;
esac
has() { [[ " $SETS " == *" $1 "* ]]; }

fetch_checked "$SEQ"; rc=$?; [ $rc -eq 0 ] || exit $rc

declare -A CFGVAR=( [num_pts_180]="num_pts 180" [num_pts_220]="num_pts 220"
  [fast_threshold_18]="fast_threshold 18" [fast_threshold_22]="fast_threshold 22"
  [gravity_mag_9.80665]="gravity_mag 9.80665" [gravity_mag_9.80]="gravity_mag 9.80"
  [bad_gravity_mag_9.0]="gravity_mag 9.0" [bad_num_pts_50]="num_pts 50"
  [bad_imu_noise_discrete]="kalibr_imu_chain.yaml:accelerometer_noise_density 2.828e-2 kalibr_imu_chain.yaml:gyroscope_noise_density 2.3997e-3" )
for v in "${!CFGVAR[@]}"; do
  [ -d "$CFG/$v" ] || bash scripts/experiments/make_config_variant.sh "$CFG/$v" ${CFGVAR[$v]} || exit 1
done

RAN=()
run() {  # variant name, env...
  local V=$1; shift; RAN+=("$V")
  echo "== $SEQ $V ($(date -u +%H:%M:%S)) df: $(df -h / | tail -1 | awk '{print $4" free"}')"
  env RUNS="$RUNS" "$@" bash scripts/run_sequence.sh "$SEQ" "$R/$V"
}
if has legit; then
  run base
  for s in $STARTS; do run "bag_start_$s" BAG_START="$s"; done
  for v in num_pts_180 num_pts_220 fast_threshold_18 fast_threshold_22 gravity_mag_9.80665 gravity_mag_9.80; do
    run "$v" CONFIG_DIR="$CFG/$v"
  done
fi
if has bad; then
  for v in bad_gravity_mag_9.0 bad_num_pts_50 bad_imu_noise_discrete; do run "$v" CONFIG_DIR="$CFG/$v"; done
fi
if has realtime; then
if [ "$SEQ" = MH_01_easy ]; then run rt_rate_1.0 MODE=realtime BAG_RATE=1.0 RUNS=5
else run rt_rate_1.0 MODE=realtime BAG_RATE=1.0; fi
run rt_rate_1.5 MODE=realtime BAG_RATE=1.5
run rt_rate_2.0 MODE=realtime BAG_RATE=2.0
fi
if has realtime && [ "$SEQ" = MH_01_easy ]; then
  echo "load: 4 busy loops on 4 CPUs"; pids=()
  for i in 1 2 3 4; do (while :; do :; done) & pids+=($!); done
  run rt_load_rate_1.0 MODE=realtime BAG_RATE=1.0
  kill "${pids[@]}"; wait "${pids[@]}" 2>/dev/null
fi

for V in "${RAN[@]}"; do   # score only what this invocation ran
  extra=(); [ -n "${CFGVAR[$V]+x}" ] && extra=(--config-dir "$CFG/$V")
  $PY -m vioreg.score --seqs "$SEQ" --out "$R/$V" "${extra[@]}"
done
delete_bag "$SEQ"
echo "done $SEQ ($(date -u +%H:%M:%S))"
