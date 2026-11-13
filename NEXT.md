Done (S1): pinned OpenVINS v2.7 image; fetch_euroc.sh; MH_01_easy run1.tum, ATE posyaw 0.0909 m, coverage 0.973.
Done (S2): score.py writes the frozen-contract results/<seq>.json (median run, fail closed); real MH_01_easy scores OK and passes the gate. vioreg/evo/ov_eval ATE agree to 1e-9 at matched settings (docs/crosscheck.md).
Next: scripts/run_sequence.sh (S1 docker command + run<k>.wall_s sidecar), then 5+ repeat runs per sequence for thresholds via make_thresholds.py.
Next: strip -g3 debug info from the image (devel/ 802 MB); disk is ~7 GB free.
Changed (S3): config_hash now = sha1 of estimator_config.yaml + referenced kalibr imu/imucam yamls, sorted by name (key set unchanged). Open: RC 429 risk in CI? poetry.lock?
