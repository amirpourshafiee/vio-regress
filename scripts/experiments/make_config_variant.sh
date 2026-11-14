#!/usr/bin/env bash
# Copy the image's euroc_mav config and change one or more keys.
#   usage: make_config_variant.sh <out_dir> <key> <value> [<key> <value> ...] [-- image]
# A key is an estimator_config.yaml key (e.g. num_pts) or <file>:<key> for another config
# file (e.g. kalibr_imu_chain.yaml:gyroscope_noise_density; indented keys are matched).
# Fails if a key is absent or appears more than once in its file, or if an edit did not
# change exactly one line. Never guesses a parameter name.
set -euo pipefail
OUT=${1:?out dir}; shift
IMAGE=vioreg-openvins:v2.7
args=("$@")
if [ "${#args[@]}" -ge 2 ] && [ "${args[-2]}" = "--" ]; then IMAGE=${args[-1]}; args=("${args[@]:0:${#args[@]}-2}"); fi
[ $(( ${#args[@]} % 2 )) -eq 0 ] && [ ${#args[@]} -ge 2 ] || { echo "need <key> <value> pairs" >&2; exit 1; }
mkdir -p "$OUT"
for f in estimator_config.yaml kalibr_imu_chain.yaml kalibr_imucam_chain.yaml; do
  docker run --rm --entrypoint cat "$IMAGE" "/ws/src/open_vins/config/euroc_mav/$f" > "$OUT/$f"
done
for ((i=0; i<${#args[@]}; i+=2)); do
  spec=${args[i]}; VAL=${args[i+1]}
  if [[ $spec == *:* ]]; then FILE=${spec%%:*}; KEY=${spec#*:}; else FILE=estimator_config.yaml; KEY=$spec; fi
  E="$OUT/$FILE"; [ -f "$E" ] || { echo "no config file $FILE" >&2; exit 1; }
  n=$(grep -cE "^[[:space:]]*${KEY}:" "$E" || true)
  [ "$n" = 1 ] || { echo "key $KEY found $n times in $FILE" >&2; exit 1; }
  cp "$E" "$E.orig"
  sed -i -E "s/^([[:space:]]*${KEY}:[[:space:]]*)[^[:space:]#]+/\1${VAL}/" "$E"
  changed=$(diff "$E.orig" "$E" | grep -c '^>' || true); rm "$E.orig"
  [ "$changed" = 1 ] || { echo "edit of $KEY changed $changed lines, expected 1" >&2; exit 1; }
  grep -qE "^[[:space:]]*${KEY}:[[:space:]]*${VAL}([[:space:]]|$)" "$E" || { echo "$KEY value not applied" >&2; exit 1; }
  echo "$OUT/$FILE: $(grep -E "^[[:space:]]*${KEY}:" "$E" | sed 's/^ *//')"
done
