#!/usr/bin/env bash
# Copy the image's euroc_mav config and change exactly ONE estimator_config.yaml key.
#   usage: make_config_variant.sh <out_dir> <key> <value> [image]
# Fails if the key is absent, appears more than once, or the edit did not change exactly
# one line. Never guesses a parameter name.
set -euo pipefail
OUT=${1:?out dir}; KEY=${2:?key}; VAL=${3:?value}; IMAGE=${4:-vioreg-openvins:v2.7}
mkdir -p "$OUT"
for f in estimator_config.yaml kalibr_imu_chain.yaml kalibr_imucam_chain.yaml; do
  docker run --rm --entrypoint cat "$IMAGE" "/ws/src/open_vins/config/euroc_mav/$f" > "$OUT/$f"
done
E="$OUT/estimator_config.yaml"
n=$(grep -cE "^${KEY}:" "$E" || true)
[ "$n" = 1 ] || { echo "key $KEY found $n times in estimator_config.yaml" >&2; exit 1; }
cp "$E" "$E.orig"
sed -i -E "s/^(${KEY}:[[:space:]]*)[^[:space:]#]+/\1${VAL}/" "$E"
changed=$(diff "$E.orig" "$E" | grep -c '^>' || true); rm "$E.orig"
[ "$changed" = 1 ] || { echo "edit changed $changed lines, expected 1" >&2; exit 1; }
grep -qE "^${KEY}:[[:space:]]*${VAL}([[:space:]]|$)" "$E" || { echo "value not applied" >&2; exit 1; }
echo "$OUT: $(grep -E "^${KEY}:" "$E")"
