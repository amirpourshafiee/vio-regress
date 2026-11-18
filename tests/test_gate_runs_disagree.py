"""Runs that disagree under one key get their own verdict (first scheduled nightly, run
43743071045): MH_03_medium's 2 runs gave 2 distinct trajectories, 2261 associated poses
against a baseline of 2275 - 4, RPE ratio 1.002, ATE ratio 1.000. The gate said QUALITY FAIL
on pose count; the real finding was that the estimator was not repeatable on that host.

Margins are unchanged: the sequence still FAILS. Only the label and the reason change.
"""
import json

import numpy as np

from vioreg import gate
from vioreg.euroc_io import Trajectory, save_tum

KEY = {"vio_commit": "a" * 40, "image_digest": "sha256:" + "b" * 64, "config_hash": "c" * 40}
SEQ = "MH_03_medium"
BASE = {"ate_rmse": 0.0915, "rpe_rmse": 0.0463, "coverage": 0.979, "n": 2275}


def cfg():
    return {"version": 2, "defaults": {"align": "posyaw"},
            "margins": {"rpe_ratio_max": 1.28, "ate_geomean_ratio_max": 1.5,
                        "coverage_drop_max": 0.01, "pose_count_drop_max": 4},
            "sequences": {SEQ: {"skip_s": 15, "baselines": [{**KEY, **BASE}]}}}


def write_result(root, n, rpe_x=1.0):
    r = {"sequence": SEQ, **KEY, "harness_commit": "d" * 40, "runs": 2, "align": "posyaw",
         "ate": {"ok": True, "rmse": BASE["ate_rmse"], "median": None, "max": None,
                 "coverage": BASE["coverage"], "n": n},
         "rpe": {"ok": True, "rmse": BASE["rpe_rmse"] * rpe_x, "delta_s": 1.0},
         "per_run_ate_rmse": [BASE["ate_rmse"]] * 2, "wall_s": 40.0}
    (root / f"{SEQ}.json").write_text(json.dumps(r))


def write_runs(root, same: bool):
    d = root / SEQ
    d.mkdir()
    t = np.arange(50) * 0.05
    p = np.zeros((50, 3))
    q = np.tile([0.0, 0.0, 0.0, 1.0], (50, 1))
    save_tum(Trajectory(t, p, q), d / "run1.tum")
    p2 = p.copy()
    if not same:
        p2[30:, 0] += 1e-6  # one tiny float difference is enough: not the same trajectory
    save_tum(Trajectory(t, p2, q), d / "run2.tum")


def evaluate(tmp_path):
    rep = gate.evaluate_results_dir(tmp_path, cfg(), require=True, root=tmp_path, scope=[SEQ])
    return rep["verdicts"][SEQ], rep["sequences"][SEQ]


def test_nov15_case_is_labelled_nondeterministic_and_still_fails(tmp_path):
    write_result(tmp_path, n=2261)          # 14 short, limit 4
    write_runs(tmp_path, same=False)
    verdict, fails = evaluate(tmp_path)
    assert verdict == "nondeterministic"
    assert any(f.startswith("associated pose count 2261") for f in fails)
    assert any(f.startswith("runs disagree: 2 distinct trajectories from 2 runs") for f in fails)


def test_disagreeing_runs_fail_even_when_metrics_pass(tmp_path):
    write_result(tmp_path, n=2275)
    write_runs(tmp_path, same=False)
    verdict, fails = evaluate(tmp_path)
    assert verdict == "nondeterministic" and fails  # fails closed, never a silent pass


def test_rpe_failure_keeps_the_quality_label_even_if_runs_disagree(tmp_path):
    write_result(tmp_path, n=2261, rpe_x=1.5)
    write_runs(tmp_path, same=False)
    verdict, _ = evaluate(tmp_path)
    assert verdict == "quality-fail"


def test_identical_runs_short_on_poses_stay_quality_fail(tmp_path):
    write_result(tmp_path, n=2261)
    write_runs(tmp_path, same=True)
    verdict, fails = evaluate(tmp_path)
    assert verdict == "quality-fail"
    assert not any(f.startswith("runs disagree") for f in fails)


def test_identical_runs_within_margins_pass(tmp_path):
    write_result(tmp_path, n=2275)
    write_runs(tmp_path, same=True)
    verdict, fails = evaluate(tmp_path)
    assert (verdict, fails) == ("pass", [])
