"""Sensitivity analysis: grouping, ranges vs the S3 baseline, config hashes, failures."""
import json

import pytest

from test_score import PROV, est_with_known_error, gt_traj, write_euroc_gt
from vioreg import score, sensitivity
from vioreg.euroc_io import save_tum

SEQ = "MH_01_easy"


@pytest.mark.parametrize("name,group", [
    ("base", "baseline"), ("bag_start_38", "replay_offset"), ("num_pts_180", "config"),
    ("fast_threshold_22", "config"), ("gravity_mag_9.80665", "innocent"),
    ("rt_rate_1.5", "realtime_rate"), ("rt_load_rate_1.0", "realtime_load")])
def test_group_of(name, group):
    assert sensitivity.group_of(name) == group


def test_unknown_variant_is_an_error():
    with pytest.raises(ValueError):
        sensitivity.group_of("mystery")


def test_only_serial_groups_are_legitimate():
    assert sensitivity.LEGITIMATE == {"baseline", "replay_offset", "config", "innocent"}


@pytest.fixture
def root(tmp_path):
    gt = gt_traj()
    data = tmp_path / "data"
    gt_csv = data / SEQ / "mav0" / "state_groundtruth_estimate0" / "data.csv"
    write_euroc_gt(gt, gt_csv)
    r = tmp_path / "sens"
    errs = {"base": [0.020] * 3, "bag_start_42": [0.022] * 3, "num_pts_180": [0.018] * 3,
            "rt_rate_1.0": [0.012] * 3, "gravity_mag_9.80": [0.020, 0.020, None]}
    for v, ds in errs.items():
        d = r / v / SEQ
        d.mkdir(parents=True)
        for k, e in enumerate(ds, start=1):
            if e is None:
                (d / f"run{k}.tum").write_text("")
            else:
                save_tum(est_with_known_error(gt, e), d / f"run{k}.tum")
        prov = {**PROV, "config_hash": ("9" if v.startswith("num_pts") else "d") * 40}
        score.score_sequence(SEQ, r / v, gt_csv, provenance=prov, align="posyaw", skip_s=0.0)
    return r, data


def test_analyse_ranges_against_s3_baseline(root):
    r, data = root
    thr = {"defaults": {"align": "posyaw"}, "sequences": {SEQ: {"skip_s": 0}}}
    out = sensitivity.analyse(r, [SEQ], data, thr, s3_baseline={SEQ: 0.020})
    s = out["sequences"][SEQ]
    v = s["variants"]
    assert v["num_pts_180"]["config_hash"] == "9" * 40 and v["base"]["config_hash"] == "d" * 40
    assert v["bag_start_42"]["ate_median_m"] == pytest.approx(0.022, abs=1e-6)
    assert v["bag_start_42"]["delta_pct"] == pytest.approx(10.0, abs=1e-3)
    assert v["gravity_mag_9.80"]["failed"] == 1
    leg = s["legitimate"]
    assert leg["min_m"] == pytest.approx(0.018, abs=1e-6)
    assert leg["max_m"] == pytest.approx(0.022, abs=1e-6)
    assert leg["min_pct"] == pytest.approx(-10.0, abs=1e-3)
    assert leg["max_pct"] == pytest.approx(10.0, abs=1e-3)
    assert leg["range_pct"] == pytest.approx(20.0, abs=1e-3)
    assert leg["failed_runs"] == 1 and leg["variants"] == 4
    assert "rt_rate_1.0" not in leg["included"]
    rt = s["realtime"]
    assert rt["min_m"] == pytest.approx(0.012, abs=1e-6)
    json.dumps(out, allow_nan=False)


def test_missing_score_json_fails_closed(root):
    r, data = root
    (r / "base" / f"{SEQ}.json").unlink()
    with pytest.raises(sensitivity.SensitivityError, match="score"):
        sensitivity.analyse(r, [SEQ], data, {"defaults": {}, "sequences": {SEQ: {"skip_s": 0}}},
                            s3_baseline={SEQ: 0.020})
