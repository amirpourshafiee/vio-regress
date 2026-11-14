# vioreg starter

Tested starter modules from the build guide: metrics, gate, faults, windows, judge, kappa.
Not included on purpose: runner scripts, score.py, report.py, make_thresholds.py (sessions S1 to S4).
Dockerfile and workflows are UNTESTED skeletons.

    python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
    pytest -m "not sequence and not slow"
    HARNESS_RESULTS=tests/golden_results HARNESS_REQUIRE_RESULTS=1 pytest -m sequence

tests/golden_results holds hand-made JSONs that exercise gate logic only. They are not VIO results.
Every sequence listed in configs/thresholds.yaml needs a golden file, or the PR tier fails by design.
Thresholds in configs/thresholds.yaml are placeholders until you run the baseline experiment.

## Result JSON contract (`results/<seq>.json`, written by `python -m vioreg.score`)

The key set is frozen (enforced by `tests/test_score.py`):

    sequence, backend, vio_commit, image_digest, harness_commit, config_hash, runs,
    align, skip_s, ate{ok, rmse, median, max, coverage, n}, rpe{ok, rmse, delta_s},
    per_run_ate_rmse, wall_s

- `vio_commit`: the OpenVINS commit baked into the image (label `vioreg.ov_ref`).
- `image_digest`: the Docker image ID (`sha256:...`).
- `harness_commit`: git HEAD of this repo, with `-dirty` appended if `src/`, `configs/`
  or `scripts/` has uncommitted changes (untracked files included).
- `config_hash`: sha1 over `estimator_config.yaml` plus the kalibr IMU and IMU-camera
  yamls it references (`relative_config_imu`, `relative_config_imucam`), with the file
  contents concatenated in sorted file-name order. The configs are baked into the image at
  `/ws/src/open_vins/config/<config>/`; a run that mounts an override directory must be
  scored with `--config-dir` pointing at it. `scripts/run_sequence.sh` writes a
  `run<k>.config_hash` sidecar per run, and any mismatch fails the result.
  (Before S3, this hashed only `estimator_config.yaml`.)
- `ate` and `rpe` come from the median run by ATE RMSE (the upper median for an even run
  count). Any failed run makes `ok=false`, and the exit code is non-zero.
