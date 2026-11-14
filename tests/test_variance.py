"""Variance analysis: per-run metrics, spread statistics, bit-identity, baseline.json."""
import json

import numpy as np
import pytest

from test_score import PROV, est_with_known_error, gt_traj, write_euroc_gt
from vioreg import score, variance
from vioreg.euroc_io import save_tum

SEQ = "MH_01_easy"


def test_spread_statistics():
    s = variance.spread([0.10, 0.12, 0.11, 0.13, 0.10])
    assert s["n"] == 5
    assert s["median"] == pytest.approx(0.11)
    assert s["min"] == 0.10 and s["max"] == 0.13 and s["range"] == pytest.approx(0.03)
    assert s["std"] == pytest.approx(np.std([0.10, 0.12, 0.11, 0.13, 0.10], ddof=1))
    assert s["mad"] == pytest.approx(1.4826 * 0.01)


def test_spread_ignores_failed_runs_but_counts_them():
    s = variance.spread([0.1, None, 0.1])
    assert s["n"] == 2 and s["failed"] == 1 and s["range"] == 0.0


def test_spread_of_nothing_is_explicit():
    s = variance.spread([None, None])
    assert s["n"] == 0 and s["failed"] == 2 and s["median"] is None


@pytest.fixture
def variant_root(tmp_path):
    gt = gt_traj()
    data = tmp_path / "data"
    write_euroc_gt(gt, data / SEQ / "mav0" / "state_groundtruth_estimate0" / "data.csv")
    root = tmp_path / "A_serial"
    d = root / SEQ
    d.mkdir(parents=True)
    # runs 1 and 2 are byte-identical, run 3 differs, run 4 failed (empty)
    save_tum(est_with_known_error(gt, 0.02), d / "run1.tum")
    save_tum(est_with_known_error(gt, 0.02), d / "run2.tum")
    save_tum(est_with_known_error(gt, 0.03), d / "run3.tum")
    (d / "run4.tum").write_text("")
    for k, w in ((1, 50.0), (2, 52.0), (3, 51.0), (4, 9.0)):
        (d / f"run{k}.wall_s").write_text(f"{w}\n")
    score.score_sequence(SEQ, root, data / SEQ / "mav0/state_groundtruth_estimate0/data.csv",
                         provenance=PROV, align="posyaw", skip_s=0.0)
    return root, data


def test_collect_runs_scores_each_run_and_fingerprints_trajectories(variant_root):
    root, data = variant_root
    runs = variance.collect_runs(root, SEQ, data, align="posyaw", skip_s=0.0)
    assert [r["k"] for r in runs] == [1, 2, 3, 4]
    assert [r["ok"] for r in runs] == [True, True, True, False]
    assert runs[0]["ate_rmse"] == pytest.approx(0.02, abs=1e-6)
    assert runs[2]["ate_rmse"] == pytest.approx(0.03, abs=1e-6)
    assert runs[0]["rpe_rmse"] == pytest.approx(0.04, abs=1e-6)  # 2d, see test_score
    assert runs[3]["ate_rmse"] is None and runs[3]["reason"]
    assert runs[0]["traj_sha1"] == runs[1]["traj_sha1"] != runs[2]["traj_sha1"]
    assert runs[3]["traj_sha1"] is None
    assert [r["wall_s"] for r in runs] == [50.0, 52.0, 51.0, 9.0]


def test_summarise_counts_distinct_trajectories_and_failures(variant_root):
    root, data = variant_root
    s = variance.summarise(variance.collect_runs(root, SEQ, data, "posyaw", 0.0))
    assert s["runs"] == 4 and s["failed"] == 1 and s["distinct_trajectories"] == 2
    assert s["ate_rmse"]["range"] == pytest.approx(0.01, abs=1e-6)


def test_baseline_json_shape(variant_root, tmp_path):
    root, data = variant_root
    thr = {"defaults": {"align": "posyaw"}, "sequences": {SEQ: {"skip_s": 0}}}
    b = variance.build_baseline(root, [SEQ], data, thr)
    json.dumps(b, allow_nan=False)
    s = b["sequences"][SEQ]
    for k in ("vio_commit", "image_digest", "harness_commit", "config_hash"):
        assert s[k] == PROV[k]
    assert s["per_run_ate_rmse"] == pytest.approx([0.02, 0.02, 0.03, None], abs=1e-6)
    assert len(s["per_run_rpe_rmse"]) == 4 and s["per_run_rpe_rmse"][3] is None
    assert s["per_run_ok"] == [True, True, True, False]
    assert s["align"] == "posyaw" and s["skip_s"] == 0.0


def test_baseline_fails_closed_without_score_json(variant_root):
    root, data = variant_root
    (root / f"{SEQ}.json").unlink()
    with pytest.raises(variance.VarianceError, match="score"):
        variance.build_baseline(root, [SEQ], data,
                                {"defaults": {}, "sequences": {SEQ: {"skip_s": 0}}})
