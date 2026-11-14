Done (S1-S2): pinned OpenVINS v2.7 image, fetch_euroc.sh, score.py (frozen contract, fail closed), 3-way ATE cross-check (docs/crosscheck.md).
Done (S3): config_hash = sha1(estimator_config + referenced kalibr imu/imucam yamls, sorted; configs baked into image); run_sequence.sh; 100-run variance study (docs/variance/): serial replay is bit-identical run to run, 0/100 failed; realtime differs by ~31 mm on MH_01.
Next (S4): re-run the baseline on the CI runner to test cross-host determinism, then set thresholds from docs/variance/baseline.json (MAD=0, so margin needs its own justification); pin MODE=serial; 2 runs/seq to detect lost determinism.
Next: restore the nightly schedule once thresholds are set (ask before editing .github/workflows); strip -g3 from the image (802 MB).
Open: thresholds.yaml skip_s=15 vs upstream bag_start=17.5 on MH_03 (coverage OK at 0.979)? RC 429 risk in CI (cache data/euroc)? poetry.lock?
