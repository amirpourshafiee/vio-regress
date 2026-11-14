"""vioreg.report: the nightly job summary. Never the verdict (the gate is); it must keep
"needs re-baseline" visibly apart from a quality failure and report pose reproduction and
the RPE ratio separately, even when the key differs (then labelled informational)."""
import json

import pytest

from test_gate import KEY, BASE, cfg, result, write_results
from vioreg import report


def tum(path, n=6, shift=0.0, start=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{100 + 0.05 * i:.9f} {i * 0.1 + shift:.9f} 0 0 0 0 0 1\n"
                            for i in range(start, n)))


@pytest.fixture
def setup(tmp_path):
    c = cfg()
    for s in c["sequences"]:
        tum(tmp_path / "baselines" / f"{s}.tum")
        c["sequences"][s]["baselines"][0]["trajectory"] = f"baselines/{s}.tum"
    return tmp_path, c


def host(path, model="AMD EPYC 7763 64-Core Processor", flags=("avx", "avx2", "fma")):
    path.write_text(json.dumps({"cpu_model": model, "logical_cpus": 4,
                                "simd_flags": sorted(flags)}))
    return path


def test_same_key_identical_poses_reports_pass_and_reproduced(setup):
    root, c = setup
    res = root / "res"
    write_results(res, {"MH_01_easy": result("MH_01_easy")})
    for k in (1, 2):
        tum(res / "MH_01_easy" / f"run{k}.tum")
    md = report.render(res, c, root=root, seqs=["MH_01_easy"])
    assert "MH_01_easy" in md and "PASS" in md
    assert "key: match" in md
    assert "(a) poses: reproduced exactly" in md and "2/2 runs" in md
    assert "runs agree with each other: yes (1 distinct trajectory)" in md
    assert "(b) RPE ratio 1.000" in md and "margin 1.10" in md
    assert "informational" not in md.split("MH_01_easy", 1)[1].split("ATE")[0]


def test_key_mismatch_is_needs_rebaseline_not_quality_and_still_compares(setup):
    root, c = setup
    res = root / "res"
    r = result("MH_01_easy", rpe_x=1.05, image_digest="sha256:" + "9" * 64)
    write_results(res, {"MH_01_easy": r})
    tum(res / "MH_01_easy" / "run1.tum", shift=1e-6)
    md = report.render(res, c, root=root, seqs=["MH_01_easy"])
    assert "NEEDS RE-BASELINE" in md and "QUALITY FAIL" not in md
    assert "image_digest differs" in md and "sha256:99999999" in md
    assert "(a) poses: differ from the baseline" in md and "informational" in md
    assert "(b) RPE ratio 1.050" in md


def test_quality_failure_is_labelled_as_such(setup):
    root, c = setup
    res = root / "res"
    write_results(res, {"MH_01_easy": result("MH_01_easy", rpe_x=2.0)})
    md = report.render(res, c, root=root, seqs=["MH_01_easy"])
    assert "QUALITY FAIL" in md and "RPE ratio 2.000 > 1.100" in md
    assert "NEEDS RE-BASELINE" not in md


def test_missing_sequence_is_reported_missing(setup):
    root, c = setup
    (root / "res").mkdir()
    md = report.render(root / "res", c, root=root, seqs=["MH_01_easy"])
    assert "MISSING" in md and "missing result" in md


def test_runs_that_disagree_are_flagged(setup):
    root, c = setup
    res = root / "res"
    write_results(res, {"MH_01_easy": result("MH_01_easy")})
    tum(res / "MH_01_easy" / "run1.tum")
    tum(res / "MH_01_easy" / "run2.tum", shift=1e-3)
    md = report.render(res, c, root=root, seqs=["MH_01_easy"])
    assert "runs agree with each other: NO (2 distinct trajectories)" in md
    assert "1/2 runs" in md


def test_partial_scope_says_why_ate_aggregate_is_skipped(setup):
    root, c = setup
    res = root / "res"
    write_results(res, {"MH_01_easy": result("MH_01_easy")})
    md = report.render(res, c, root=root, seqs=["MH_01_easy"])
    assert "covers 1 of 3" in md


def test_host_table_shows_simd_differences(setup, tmp_path):
    root, c = setup
    ci = host(tmp_path / "ci.json", model="Intel Xeon Platinum 8370C",
              flags=("avx", "avx2", "fma", "avx512f"))
    ref = host(tmp_path / "ref.json")
    md = report.render(root / "nores", c, root=root, seqs=[], host=ci, reference_host=ref)
    assert "Intel Xeon Platinum 8370C" in md and "AMD EPYC 7763" in md
    assert "avx512f" in md and "only on CI" in md
    assert "different CPU" in md


def test_render_never_raises_on_garbage(setup):
    root, c = setup
    res = root / "res"
    res.mkdir()
    (res / "MH_01_easy.json").write_text("{nope")
    md = report.render(res, c, root=root, seqs=["MH_01_easy"], host=root / "absent.json")
    assert "MISSING" in md and "host fingerprint unavailable" in md


def test_cli_writes_markdown_and_exits_zero(setup, capsys):
    root, c = setup
    import yaml
    thr = root / "thr.yaml"
    thr.write_text(yaml.safe_dump(c))
    (root / "res").mkdir()
    assert report.main([str(root / "res"), "--seqs", "MH_01_easy", "--thresholds", str(thr),
                        "--root", str(root)]) == 0
    assert "MISSING" in capsys.readouterr().out


def test_fetch_failure_says_not_a_quality_result(setup):
    import json as _j
    from test_gate import all_results, failed_result, write_fetch_status
    root, c = setup
    res = root / "res"
    rs = all_results()
    rs["MH_03_medium"] = failed_result("MH_03_medium")
    write_results(res, rs)
    write_fetch_status(res, "MH_03_medium", ok=False)
    md = report.render(res, c, root=root)
    assert "### MH_03_medium: **DATA FETCH FAILED**" in md
    assert "data fetch failed, not a quality result" in md
    assert "DATA FETCH FAILED 1" in md and "QUALITY FAIL" not in md


def test_injected_fault_banner_marks_a_demo_run(setup):
    root, c = setup
    res = root / "res"
    write_results(res, {"MH_01_easy": result("MH_01_easy", rpe_x=2.0)})
    md = report.render(res, c, root=root, seqs=["MH_01_easy"], injected_fault="blur=9")
    head = md.split("### MH_01_easy", 1)[0]
    assert "FAULT INJECTED: `blur=9`" in head and "expected" in head
    assert "QUALITY FAIL" in md
    plain = report.render(res, c, root=root, seqs=["MH_01_easy"])
    assert "FAULT INJECTED" not in plain
