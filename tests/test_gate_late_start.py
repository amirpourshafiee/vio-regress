"""A recorder that starts late loses LEADING poses and nothing else.

Nightly #7 (run 44380368789, Intel Xeon 6975P-C), MH_03_medium, same job, same host, same bag:
run2 = baseline exactly (2296 poses); run1 lacked the first 5 poses (0.25 s) and every other
pose was byte-identical. Nightly #4 lost 14. The gate said QUALITY FAIL (2270 < 2275 - 4).

Rule under test: when the pose count is the ONLY failure and every run's poses are a
bit-identical copy of the baseline trajectory minus at most LATE_START_MAX_POSES leading poses,
the sequence passes with a warning that says how late. Anything else keeps failing: a moved pose,
a gap, poses lost at the end, a later start, no baseline trajectory, or any RPE/coverage failure.
Margins are unchanged.
"""
import json

import numpy as np
import pytest

from vioreg import gate, report
from vioreg.euroc_io import Trajectory, save_tum
from vioreg.variance import distinct_trajectories, pose_equivalent

KEY = {"vio_commit": "a" * 40, "image_digest": "sha256:" + "b" * 64, "config_hash": "c" * 40}
SEQ = "MH_03_medium"
REF = "baselines/MH_03_medium.tum"
N = 200
BASE = {"ate_rmse": 0.0915, "rpe_rmse": 0.0463, "coverage": 0.979, "n": 2275, "trajectory": REF}


def cfg(with_traj=True):
    b = dict(BASE) if with_traj else {k: v for k, v in BASE.items() if k != "trajectory"}
    return {"version": 2, "defaults": {"align": "posyaw"},
            "margins": {"rpe_ratio_max": 1.28, "ate_geomean_ratio_max": 1.5,
                        "coverage_drop_max": 0.01, "pose_count_drop_max": 4},
            "sequences": {SEQ: {"skip_s": 15, "baselines": [{**KEY, **b}]}}}


def traj(lo=0, hi=N, shift_from=None):
    t = 1403637150.0 + np.arange(N)[lo:hi] * 0.05
    p = np.column_stack([np.arange(N)[lo:hi] * 1e-3, np.zeros(hi - lo), np.zeros(hi - lo)])
    if shift_from is not None:
        p = p.copy()
        p[np.arange(lo, hi) >= shift_from, 1] += 1e-6
    q = np.tile([0.0, 0.0, 0.0, 1.0], (hi - lo, 1))
    return Trajectory(t, p, q)


def setup(tmp_path, runs, n, rpe_x=1.0, with_traj=True):
    """runs: list of Trajectory. n: associated pose count the scorer reported."""
    (tmp_path / "baselines").mkdir(exist_ok=True)
    save_tum(traj(), tmp_path / REF)
    d = tmp_path / SEQ
    d.mkdir()
    for k, tr in enumerate(runs, 1):
        save_tum(tr, d / f"run{k}.tum")
    r = {"sequence": SEQ, **KEY, "harness_commit": "d" * 40, "runs": len(runs), "align": "posyaw",
         "ate": {"ok": True, "rmse": BASE["ate_rmse"], "median": None, "max": None,
                 "coverage": BASE["coverage"], "n": n},
         "rpe": {"ok": True, "rmse": BASE["rpe_rmse"] * rpe_x, "delta_s": 1.0},
         "per_run_ate_rmse": [BASE["ate_rmse"]] * len(runs), "wall_s": 40.0}
    (tmp_path / f"{SEQ}.json").write_text(json.dumps(r))
    return cfg(with_traj)


def evaluate(tmp_path, c):
    rep = gate.evaluate_results_dir(tmp_path, c, require=True, root=tmp_path, scope=[SEQ])
    return rep["verdicts"][SEQ], rep["sequences"][SEQ], rep["warnings"]


def test_nightly7_case_passes_with_a_late_start_warning(tmp_path):
    c = setup(tmp_path, [traj(lo=5), traj()], n=2270)       # 5 late, 1 short of the margin
    verdict, fails, warns = evaluate(tmp_path, c)
    assert (verdict, fails) == ("pass", [])
    assert any("run1" in w and "5 poses" in w and "late" in w for w in warns)
    assert not any("run2" in w for w in warns)


def test_nightly4_case_14_late_in_both_runs_passes(tmp_path):
    c = setup(tmp_path, [traj(lo=14), traj(lo=14)], n=2261)
    verdict, fails, warns = evaluate(tmp_path, c)
    assert (verdict, fails) == ("pass", [])
    assert len(warns) == 2 and all("14 poses" in w for w in warns)


def test_limit_is_inclusive_and_one_more_fails(tmp_path):
    lim = gate.LATE_START_MAX_POSES
    c = setup(tmp_path, [traj(lo=lim), traj(lo=lim)], n=2275 - lim)
    assert evaluate(tmp_path, c)[0] == "pass"


def test_later_start_than_the_limit_still_fails(tmp_path):
    lim = gate.LATE_START_MAX_POSES
    c = setup(tmp_path, [traj(lo=lim + 1), traj(lo=lim + 1)], n=2275 - lim - 1)
    verdict, fails, _ = evaluate(tmp_path, c)
    assert verdict == "quality-fail" and fails


def test_a_moved_pose_after_the_late_start_still_fails(tmp_path):
    c = setup(tmp_path, [traj(lo=5, shift_from=100), traj()], n=2270)
    verdict, fails, _ = evaluate(tmp_path, c)
    assert verdict != "pass" and fails


def test_poses_lost_at_the_end_are_not_a_late_start(tmp_path):
    c = setup(tmp_path, [traj(hi=N - 6), traj()], n=2269)
    verdict, fails, _ = evaluate(tmp_path, c)
    assert verdict != "pass" and fails


def test_a_gap_in_the_middle_still_fails(tmp_path):
    full = traj()
    keep = np.r_[0:80, 90:N]
    gap = Trajectory(full.t[keep], full.p[keep], full.q[keep])
    c = setup(tmp_path, [gap, traj()], n=2265)
    assert evaluate(tmp_path, c)[0] != "pass"


def test_no_baseline_trajectory_means_no_waiver(tmp_path):
    c = setup(tmp_path, [traj(lo=5), traj()], n=2270, with_traj=False)
    verdict, fails, _ = evaluate(tmp_path, c)
    assert verdict != "pass" and any(f.startswith("associated pose count") for f in fails)


def test_rpe_failure_is_never_waived(tmp_path):
    c = setup(tmp_path, [traj(lo=5), traj()], n=2270, rpe_x=1.5)
    verdict, fails, _ = evaluate(tmp_path, c)
    assert verdict == "quality-fail" and any(f.startswith("RPE ratio") for f in fails)


def test_pose_count_margin_itself_is_unchanged():
    assert gate.margins(cfg())["pose_count_drop_max"] == 4


def test_exact_runs_give_no_warning(tmp_path):
    c = setup(tmp_path, [traj(), traj()], n=2275)
    verdict, fails, warns = evaluate(tmp_path, c)
    assert (verdict, fails, warns) == ("pass", [], [])


def test_pose_equivalent_leading_allowance_is_opt_in(tmp_path):
    save_tum(traj(), tmp_path / "a.tum")
    save_tum(traj(lo=5), tmp_path / "b.tum")
    assert not pose_equivalent(tmp_path / "a.tum", tmp_path / "b.tum")          # default: 2
    assert pose_equivalent(tmp_path / "a.tum", tmp_path / "b.tum", max_leading=10)
    save_tum(traj(hi=N - 5), tmp_path / "c.tum")                                # end stays strict
    assert not pose_equivalent(tmp_path / "a.tum", tmp_path / "c.tum", max_leading=10)
    assert distinct_trajectories([tmp_path / "a.tum", tmp_path / "b.tum"]) == 2
    assert distinct_trajectories([tmp_path / "a.tum", tmp_path / "b.tum"], max_leading=10) == 1


@pytest.mark.parametrize("lo,kind,k", [(0, "same", 0), (2, "same", 0), (5, "late", 5),
                                       (40, "late", 40), (41, "differs", None)])
def test_run_vs_baseline_kinds(tmp_path, lo, kind, k):
    save_tum(traj(), tmp_path / "ref.tum")
    save_tum(traj(lo=lo), tmp_path / "run.tum")
    assert gate.run_vs_baseline(tmp_path / "run.tum", tmp_path / "ref.tum") == (kind, k)


def test_report_says_late_start_not_identical(tmp_path):
    c = setup(tmp_path, [traj(lo=5), traj()], n=2270)
    text = "\n".join(report._sequence_section(tmp_path, SEQ, c, tmp_path, [], "pass"))
    assert "late" in text and "5 poses" in text
    assert "(a) poses: reproduced exactly (2/2" not in text
