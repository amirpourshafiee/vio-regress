"""make_thresholds: baseline.json + margins -> thresholds.yaml (v2) that the gate accepts."""
import json

import pytest
import yaml

from vioreg import gate, thresholds

KEY = {"vio_commit": "a" * 40, "image_digest": "sha256:" + "b" * 64, "harness_commit": "d" * 40,
       "config_hash": "c" * 40}
MARGINS = {"rpe_ratio_max": 1.10, "ate_geomean_ratio_max": 1.30, "coverage_drop_max": 0.01}
OLD = {"defaults": {"align": "posyaw", "min_coverage": 0.95},
       "sequences": {"MH_01_easy": {"max_ate_m": 0.15, "skip_s": 40},
                     "V1_01_easy": {"max_ate_m": 0.15, "skip_s": 0}}}


def baseline(**over):
    def seq(ate, rpe, cov, n):
        return {**KEY, "align": "posyaw", "skip_s": 0.0, "runs": 3,
                "per_run_ate_rmse": [ate] * 3, "per_run_rpe_rmse": [rpe] * 3,
                "per_run_ok": [True] * 3, "per_run_coverage": [cov] * 3, "per_run_n": [n] * 3}
    b = {"variant": "A_serial", "sequences": {"MH_01_easy": seq(0.0909, 0.0264, 0.973, 2746),
                                              "V1_01_easy": seq(0.0634, 0.0444, 0.968, 2768)}}
    for s, patch in over.items():
        b["sequences"][s].update(patch)
    return b


def test_build_v2_entries_from_baseline():
    cfg = thresholds.build(baseline(), MARGINS, OLD)
    assert cfg["version"] == 2 and cfg["margins"] == MARGINS
    assert cfg["defaults"] == {"align": "posyaw"}
    e = cfg["sequences"]["MH_01_easy"]
    assert e["skip_s"] == 40  # carried over from the existing thresholds
    (b,) = e["baselines"]
    assert {k: b[k] for k in ("vio_commit", "image_digest", "config_hash")} == \
        {k: KEY[k] for k in ("vio_commit", "image_digest", "config_hash")}
    assert b["ate_rmse"] == pytest.approx(0.0909) and b["rpe_rmse"] == pytest.approx(0.0264)
    assert b["coverage"] == pytest.approx(0.973) and b["n"] == 2746


def test_uses_median_ate_rpe_and_worst_coverage_and_count():
    b = baseline(MH_01_easy={"per_run_ate_rmse": [0.09, 0.10, 0.11],
                             "per_run_coverage": [0.97, 0.96, 0.98], "per_run_n": [2746, 2740, 2750]})
    e = thresholds.build(b, MARGINS, OLD)["sequences"]["MH_01_easy"]["baselines"][0]
    assert e["ate_rmse"] == pytest.approx(0.10) and e["coverage"] == pytest.approx(0.96)
    assert e["n"] == 2740


def test_refuses_baseline_with_a_failed_run():
    b = baseline(V1_01_easy={"per_run_ok": [True, False, True],
                             "per_run_ate_rmse": [0.06, None, 0.06]})
    with pytest.raises(thresholds.ThresholdsError, match="failed run"):
        thresholds.build(b, MARGINS, OLD)


@pytest.mark.parametrize("field", ["vio_commit", "image_digest", "config_hash"])
def test_refuses_baseline_without_provenance(field):
    with pytest.raises(thresholds.ThresholdsError, match=field):
        thresholds.build(baseline(MH_01_easy={field: None}), MARGINS, OLD)


@pytest.mark.parametrize("bad", [{"rpe_ratio_max": 0.9}, {"ate_geomean_ratio_max": 1.0},
                                 {"coverage_drop_max": -0.01}, {"rpe_ratio_max": None}])
def test_rejects_margins_that_would_fail_the_baseline_itself(bad):
    with pytest.raises(thresholds.ThresholdsError):
        thresholds.build(baseline(), {**MARGINS, **bad}, OLD)


def test_unknown_skip_s_fails_closed():
    old = {"defaults": {}, "sequences": {"MH_01_easy": {"skip_s": 40}}}  # no V1_01
    with pytest.raises(thresholds.ThresholdsError, match="V1_01_easy"):
        thresholds.build(baseline(), MARGINS, old)


def test_write_and_gate_round_trip(tmp_path):
    """The written file loads, and the baseline's own results pass the gate it defines."""
    out = tmp_path / "thresholds.yaml"
    thresholds.write(thresholds.build(baseline(), MARGINS, OLD), out, provenance_note="test")
    text = out.read_text()
    assert text.startswith("#") and "make_thresholds" in text
    cfg = yaml.safe_load(text)
    res = {}
    for s, (a, r, c, n) in {"MH_01_easy": (0.0909, 0.0264, 0.973, 2746),
                            "V1_01_easy": (0.0634, 0.0444, 0.968, 2768)}.items():
        res[s] = {"sequence": s, "backend": "openvins", **KEY, "runs": 3, "align": "posyaw",
                  "skip_s": 0.0, "ate": {"ok": True, "rmse": a, "median": a, "max": a,
                                         "coverage": c, "n": n},
                  "rpe": {"ok": True, "rmse": r, "delta_s": 1.0},
                  "per_run_ate_rmse": [a] * 3, "wall_s": 40.0}
        (tmp_path / f"{s}.json").write_text(json.dumps(res[s]))
    rep = gate.evaluate_results_dir(tmp_path, cfg, require=True, root=tmp_path)
    assert rep["passed"], rep


def test_copies_representative_trajectory(tmp_path):
    traj = tmp_path / "A_serial"
    for s in ("MH_01_easy", "V1_01_easy"):
        (traj / s).mkdir(parents=True)
        for k in (1, 2, 3):
            (traj / s / f"run{k}.tum").write_text(f"1.0 0 0 0 0 0 0 1\n2.0 {k} 0 0 0 0 0 1\n")
    cfg = thresholds.build(baseline(), MARGINS, OLD, trajectories=traj, out_root=tmp_path / "repo")
    e = cfg["sequences"]["MH_01_easy"]["baselines"][0]
    assert e["trajectory"] == "baselines/MH_01_easy.tum"
    assert (tmp_path / "repo" / "baselines" / "MH_01_easy.tum").read_text().startswith("1.0")
