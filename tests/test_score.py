"""Scorer tests: golden values, fail-closed behaviour, and the frozen JSON contract."""
import json
import re

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from vioreg import score
from vioreg.euroc_io import Trajectory, save_tum

SEQ = "MH_01_easy"
PROV = {"backend": "openvins", "vio_commit": "a" * 40, "image_digest": "sha256:" + "b" * 64,
        "harness_commit": "c" * 40, "config_hash": "d" * 40}

TOP_KEYS = {"sequence", "backend", "vio_commit", "image_digest", "harness_commit",
            "config_hash", "runs", "align", "skip_s", "ate", "rpe", "per_run_ate_rmse",
            "wall_s"}
ATE_KEYS = {"ok", "rmse", "median", "max", "coverage", "n"}
RPE_KEYS = {"ok", "rmse", "delta_s"}


def gt_traj(n=2000, hz=200.0, t0=1403636580.0):
    t = t0 + np.arange(n) / hz
    p = np.column_stack([3 * np.sin(0.3 * (t - t0)), 2 * np.cos(0.2 * (t - t0)),
                         0.5 * np.sin(t - t0)])
    q = Rotation.from_euler("z", 0.1 * (t - t0)[:, None]).as_quat()  # xyzw
    return Trajectory(t, p, q)


def write_euroc_gt(tr: Trajectory, path):
    """EuRoC layout: ns timestamps, position, quaternion w x y z."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ns = np.round(tr.t * 1e9).astype(np.int64)
    with open(path, "w") as f:
        f.write("#timestamp,p_x,p_y,p_z,q_w,q_x,q_y,q_z\n")
        for k in range(len(ns)):
            x, y, z, w = tr.q[k]
            f.write(f"{ns[k]},{tr.p[k,0]:.9f},{tr.p[k,1]:.9f},{tr.p[k,2]:.9f},"
                    f"{w:.9f},{x:.9f},{y:.9f},{z:.9f}\n")


def est_with_known_error(gt: Trajectory, d: float, yaw=0.8, shift=(4.0, -2.0, 0.0)):
    """Estimate on every 7th GT sample (35 ms), +/-d alternating in z, then moved to
    another yaw+translation frame. posyaw alignment undoes the frame exactly and the
    zero-mean z pattern survives, so every per-pose ATE error is exactly d.
    35 ms is chosen so a 1 s RPE window never ties on a sample: it always spans 29
    steps (odd), so the relative z error is always 2d and RPE is exactly 2d."""
    idx = np.arange(0, len(gt), 7)
    t, p, q = gt.t[idx], gt.p[idx].copy(), gt.q[idx]
    if len(t) % 2:
        t, p, q = t[:-1], p[:-1], q[:-1]
    p[:, 2] += d * np.where(np.arange(len(t)) % 2 == 0, 1.0, -1.0)
    R = Rotation.from_euler("z", yaw)
    return Trajectory(t, R.apply(p) + np.array(shift), (R * Rotation.from_quat(q)).as_quat())


@pytest.fixture
def layout(tmp_path):
    gt = gt_traj()
    gt_csv = tmp_path / "data" / SEQ / "mav0" / "state_groundtruth_estimate0" / "data.csv"
    write_euroc_gt(gt, gt_csv)
    res = tmp_path / "results"
    (res / SEQ).mkdir(parents=True)
    return gt, gt_csv, res


def run_score(res, gt_csv, skip_s=0.0, prov=PROV):
    return score.score_sequence(SEQ, res, gt_csv, provenance=prov, align="posyaw",
                                skip_s=skip_s)


# ---------------------------------------------------------------- golden
def test_golden_single_run_known_error(layout):
    gt, gt_csv, res = layout
    save_tum(est_with_known_error(gt, 0.05), res / SEQ / "run1.tum")
    out, ok = run_score(res, gt_csv)
    assert ok
    assert out["ate"]["ok"] and out["rpe"]["ok"]
    assert out["ate"]["rmse"] == pytest.approx(0.05, abs=1e-6)
    assert out["ate"]["median"] == pytest.approx(0.05, abs=1e-6)
    assert out["ate"]["max"] == pytest.approx(0.05, abs=1e-6)
    n = len(est_with_known_error(gt, 0.05))
    assert out["ate"]["n"] == n
    assert out["ate"]["coverage"] == pytest.approx(1.0, abs=0.01)
    assert out["rpe"]["rmse"] == pytest.approx(0.10, abs=1e-6)
    assert out["rpe"]["delta_s"] == 1.0
    assert out["runs"] == 1 and out["per_run_ate_rmse"] == [pytest.approx(0.05, abs=1e-6)]
    err = np.load(res / SEQ / "err.npy")
    assert err.shape == (n,) and np.allclose(err, 0.05, atol=1e-6)
    on_disk = json.loads((res / f"{SEQ}.json").read_text())
    assert on_disk == json.loads(json.dumps(out))


def test_golden_multi_run_reports_median_run(layout):
    gt, gt_csv, res = layout
    for k, d in enumerate([0.01, 0.03, 0.02], start=1):
        save_tum(est_with_known_error(gt, d), res / SEQ / f"run{k}.tum")
    out, ok = run_score(res, gt_csv)
    assert ok and out["runs"] == 3
    assert out["per_run_ate_rmse"] == pytest.approx([0.01, 0.03, 0.02], abs=1e-6)  # run order
    assert out["ate"]["rmse"] == pytest.approx(0.02, abs=1e-6)  # the median run
    assert np.allclose(np.load(res / SEQ / "err.npy"), 0.02, atol=1e-6)


def test_even_run_count_takes_the_worse_middle_run(layout):
    gt, gt_csv, res = layout
    for k, d in enumerate([0.01, 0.04, 0.02, 0.03], start=1):
        save_tum(est_with_known_error(gt, d), res / SEQ / f"run{k}.tum")
    out, ok = run_score(res, gt_csv)
    assert ok and out["ate"]["rmse"] == pytest.approx(0.03, abs=1e-6)


def test_runs_are_ordered_numerically_not_lexically(layout):
    gt, gt_csv, res = layout
    for k in (1, 2, 10):
        save_tum(est_with_known_error(gt, 0.01 * k), res / SEQ / f"run{k}.tum")
    out, _ = run_score(res, gt_csv)
    assert out["per_run_ate_rmse"] == pytest.approx([0.01, 0.02, 0.10], abs=1e-6)


def test_skip_s_drops_the_start(layout):
    gt, gt_csv, res = layout
    save_tum(est_with_known_error(gt, 0.05), res / SEQ / "run1.tum")
    out, ok = run_score(res, gt_csv, skip_s=4.0)
    assert ok and out["skip_s"] == 4.0
    assert out["ate"]["n"] < len(est_with_known_error(gt, 0.05)) and out["ate"]["rmse"] == pytest.approx(0.05, abs=1e-6)


def test_wall_s_is_median_of_sidecars(layout):
    gt, gt_csv, res = layout
    for k, w in enumerate([50.0, 70.0, 60.0], start=1):
        save_tum(est_with_known_error(gt, 0.02), res / SEQ / f"run{k}.tum")
        (res / SEQ / f"run{k}.wall_s").write_text(f"{w}\n")
    out, _ = run_score(res, gt_csv)
    assert out["wall_s"] == 60.0


# ---------------------------------------------------------------- fail closed
def assert_failed_result(res, out, ok):
    assert not ok
    assert out["ate"]["ok"] is False and out["rpe"]["ok"] is False
    on_disk = json.loads((res / f"{SEQ}.json").read_text())  # written, not skipped
    assert on_disk["ate"]["ok"] is False


def test_missing_run_file_fails_closed(layout):
    _, gt_csv, res = layout
    out, ok = run_score(res, gt_csv)
    assert_failed_result(res, out, ok)
    assert out["runs"] == 0 and out["per_run_ate_rmse"] == []


def test_missing_results_dir_fails_closed(layout, tmp_path):
    _, gt_csv, _ = layout
    res = tmp_path / "nowhere"
    out, ok = run_score(res, gt_csv)
    assert_failed_result(res, out, ok)


@pytest.mark.parametrize("content", ["", "# header only\n"])
def test_empty_run_file_fails_closed(layout, content):
    _, gt_csv, res = layout
    (res / SEQ / "run1.tum").write_text(content)
    out, ok = run_score(res, gt_csv)
    assert_failed_result(res, out, ok)
    assert out["runs"] == 1 and out["per_run_ate_rmse"] == [None]


def test_one_bad_run_fails_the_sequence_even_if_median_is_fine(layout):
    gt, gt_csv, res = layout
    save_tum(est_with_known_error(gt, 0.02), res / SEQ / "run1.tum")
    save_tum(est_with_known_error(gt, 0.02), res / SEQ / "run2.tum")
    (res / SEQ / "run3.tum").write_text("")
    out, ok = run_score(res, gt_csv)
    assert_failed_result(res, out, ok)
    assert out["per_run_ate_rmse"][:2] == pytest.approx([0.02, 0.02], abs=1e-6)
    assert out["per_run_ate_rmse"][2] is None


def test_ate_not_ok_fails_closed(layout):
    gt, gt_csv, res = layout
    est = est_with_known_error(gt, 0.02)
    # Move the run outside the GT time range: nothing associates, ate() returns ok=False.
    save_tum(Trajectory(est.t + 1000.0, est.p, est.q), res / SEQ / "run1.tum")
    out, ok = run_score(res, gt_csv)
    assert_failed_result(res, out, ok)


def test_missing_groundtruth_fails_closed(layout, tmp_path):
    gt, _, res = layout
    save_tum(est_with_known_error(gt, 0.02), res / SEQ / "run1.tum")
    out, ok = run_score(res, tmp_path / "no_gt.csv")
    assert_failed_result(res, out, ok)


@pytest.mark.parametrize("field", ["vio_commit", "image_digest", "harness_commit", "config_hash"])
def test_missing_provenance_fails_closed(layout, field):
    gt, gt_csv, res = layout
    save_tum(est_with_known_error(gt, 0.02), res / SEQ / "run1.tum")
    out, ok = run_score(res, gt_csv, prov={**PROV, field: None})
    assert_failed_result(res, out, ok)
    assert out[field] is None


def test_cli_exit_code_nonzero_on_failure(layout, tmp_path, monkeypatch):
    gt, gt_csv, res = layout
    monkeypatch.setattr(score, "collect_provenance", lambda **kw: dict(PROV))
    thr = tmp_path / "thr.yaml"
    thr.write_text("defaults: {align: posyaw}\nsequences:\n  MH_01_easy: {skip_s: 0}\n")
    args = ["--seqs", SEQ, "--out", str(res), "--data", str(tmp_path / "data"),
            "--thresholds", str(thr)]
    assert score.main(args) == 1  # no run files yet
    save_tum(est_with_known_error(gt, 0.02), res / SEQ / "run1.tum")
    assert score.main(args) == 0
    assert score.main(["--seqs", "MH_99_bogus", *args[2:]]) == 1  # unknown sequence


def _git(repo, *args):
    import subprocess
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_harness_commit_marks_untracked_src_as_dirty(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("x = 1\n")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    monkeypatch.setattr(score, "_run", lambda cmd, **kw: (
        __import__("subprocess").run(cmd, check=True, capture_output=True).stdout
        if cmd[0] == "git" else (_ for _ in ()).throw(OSError("no docker in tests"))))
    clean = score.collect_provenance(image="x", config_name="euroc_mav", repo=repo)
    assert re.fullmatch(r"[0-9a-f]{40}", clean["harness_commit"])
    assert clean["image_digest"] is None and clean["vio_commit"] is None  # docker failed
    (repo / "src" / "new.py").write_text("y = 2\n")  # untracked, uncommitted code
    assert score.collect_provenance(image="x", config_name="euroc_mav",
                                    repo=repo)["harness_commit"].endswith("-dirty")


# ---------------------------------------------------------------- schema
def check_schema(d):
    assert set(d) == TOP_KEYS
    assert set(d["ate"]) == ATE_KEYS
    assert set(d["rpe"]) == RPE_KEYS
    assert isinstance(d["runs"], int) and isinstance(d["per_run_ate_rmse"], list)
    assert isinstance(d["ate"]["ok"], bool) and isinstance(d["rpe"]["ok"], bool)


def test_schema_exact_keys_on_success(layout):
    gt, gt_csv, res = layout
    save_tum(est_with_known_error(gt, 0.02), res / SEQ / "run1.tum")
    run_score(res, gt_csv)
    d = json.loads((res / f"{SEQ}.json").read_text())
    check_schema(d)
    assert d["sequence"] == SEQ and d["align"] == "posyaw" and isinstance(d["skip_s"], float)
    assert d["backend"] == "openvins"
    for k in ("rmse", "median", "max", "coverage"):
        assert isinstance(d["ate"][k], float)
    assert isinstance(d["ate"]["n"], int)


def test_schema_exact_keys_on_failure(layout):
    _, gt_csv, res = layout
    run_score(res, gt_csv)
    check_schema(json.loads((res / f"{SEQ}.json").read_text()))


def test_no_numpy_arrays_or_nan_leak_into_json(layout):
    gt, gt_csv, res = layout
    save_tum(est_with_known_error(gt, 0.02), res / SEQ / "run1.tum")
    out, _ = run_score(res, gt_csv)
    json.dumps(out, allow_nan=False)  # raises on ndarray / numpy scalar / NaN
    text = (res / f"{SEQ}.json").read_text()
    assert "per_pose" not in text and "NaN" not in text


# ---------------------------------------------------------------- config sidecars
def test_run_config_hash_sidecar_must_match_recorded_hash(layout):
    """run_sequence.sh writes run<k>.config_hash with the hash of the config the run
    actually used. A run made with a different config (e.g. a mounted override) must not
    be scored under the image's hash."""
    gt, gt_csv, res = layout
    save_tum(est_with_known_error(gt, 0.02), res / SEQ / "run1.tum")
    save_tum(est_with_known_error(gt, 0.02), res / SEQ / "run2.tum")
    (res / SEQ / "run1.config_hash").write_text(PROV["config_hash"] + "\n")
    (res / SEQ / "run2.config_hash").write_text(PROV["config_hash"] + "\n")
    _, ok = run_score(res, gt_csv)
    assert ok
    (res / SEQ / "run2.config_hash").write_text("e" * 40 + "\n")
    out, ok = run_score(res, gt_csv)
    assert_failed_result(res, out, ok)
