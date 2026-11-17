"""S7: window set, frame extraction core, CLI (no Docker, no rosbag: fakes only)."""
import csv
import json

import numpy as np
import pytest

from vioreg import bagframes as bfr
from vioreg import window_cli as cli
from vioreg import windows as W
from vioreg.euroc_io import Trajectory, save_tum


# ------------------------------------------------------------------ helpers
def _circle(t, r=2.0):
    p = np.column_stack([r * np.cos(0.2 * t), r * np.sin(0.2 * t), 0.1 * t])
    q = np.tile([0.0, 0.0, 0.0, 1.0], (len(t), 1))
    return Trajectory(t=t, p=p, q=q)


def _write_gt(path, traj):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = np.column_stack([traj.t * 1e9, traj.p, np.ones(len(traj.t)), np.zeros((len(traj.t), 3))])
    np.savetxt(path, rows, delimiter=",", header="t,x,y,z,qw,qx,qy,qz", comments="#")


def _fake_extract(bag, request, work):
    """Stands in for the Docker call: one 48x64 frame per requested time."""
    out = work / "frames"
    out.mkdir()
    rng = np.random.default_rng(0)
    index = {}
    for key, times in request.items():
        index[key] = []
        for j, t in enumerate(times):
            name = f"{key}_{j:02d}.pgm"
            bfr.write_pgm(out / name, rng.integers(0, 255, (48, 64), dtype=np.uint8))
            index[key].append({"t": t, "stamp": t, "file": name})
    return index


@pytest.fixture
def repo(tmp_path):
    """MH_01_easy with a clean run1 + a spike, plus one faulted copy and an identity row."""
    t = np.arange(100.0, 160.0, 0.05)
    gt = _circle(t)
    _write_gt(tmp_path / "data/euroc/MH_01_easy" / W_GT, gt)
    (tmp_path / "data/euroc/MH_01_easy/MH_01_easy.bag").write_bytes(b"")
    est_p = gt.p + np.random.default_rng(1).normal(0, 0.005, gt.p.shape)
    est_p[(t >= 130) & (t < 133)] += 0.6
    (tmp_path / "results/MH_01_easy").mkdir(parents=True)
    save_tum(Trajectory(t, est_p, gt.q), tmp_path / "results/MH_01_easy/run1.tum")
    rid = "MH_01_easy__blur_length9"
    (tmp_path / f"results/faults/{rid}/MH_01_easy").mkdir(parents=True)
    save_tum(Trajectory(t, est_p, gt.q), tmp_path / f"results/faults/{rid}/MH_01_easy/run1.tum")
    (tmp_path / f"data/faulted/{rid}/MH_01_easy").mkdir(parents=True)
    (tmp_path / f"data/faulted/{rid}/MH_01_easy/MH_01_easy.bag").write_bytes(b"")
    (tmp_path / "labels").mkdir()
    with open(tmp_path / "labels/fault_manifest.csv", "w") as f:
        f.write("run_id,sequence,fault,params,seed,created_utc\n"
                f"{rid},MH_01_easy,blur,\"{{}}\",0,x\n"
                "MH_01_easy__identity_none,MH_01_easy,identity,{},0,x\n")
    return tmp_path


W_GT = cli.GT_REL


def _read_csv(path):
    return csv.DictReader(path.read_text().splitlines())


# ------------------------------------------------------------------ windows.py
def test_window_id_is_stable_opaque_and_run_specific():
    a = W.window_id("MH_01_easy__run1", 30.0)
    assert a == W.window_id("MH_01_easy__run1", 30.0) and len(a) == 10
    assert a != W.window_id("MH_01_easy__run1", 35.0) != W.window_id("V1_01_easy__run1", 30.0)
    assert all(c in "0123456789abcdef" for c in a)  # opaque hex, no sequence name inside


def test_frame_times_are_12_even_and_inside_the_window():
    ft = W.frame_times(30.0, 35.0)
    assert len(ft) == 12 and ft[0] == 30.0 and ft[-1] < 35.0
    assert np.allclose(np.diff(ft), 5.0 / 12)


def test_error_series_returns_timestamps_and_sees_the_spike(repo):
    from vioreg.euroc_io import load_euroc_gt, load_tum
    t, err = W.error_series(load_tum(repo / "results/MH_01_easy/run1.tum"),
                            load_euroc_gt(repo / "data/euroc/MH_01_easy" / W_GT))
    assert len(t) == len(err) > 100 and err[(t >= 130) & (t < 133)].mean() > 5 * err[t < 120].mean()


def test_run_windows_natural_has_controls_injected_does_not():
    t = np.arange(0, 60, 0.05)
    err = np.full_like(t, 0.05)
    err[(t >= 31) & (t < 34)] = 0.9
    nat = W.run_windows("a", "S", t, err, injected=False, top_k=2, control_k=2)
    inj = W.run_windows("b", "S", t, err, injected=True, top_k=2, control_k=2)
    assert sorted(r["source"] for r in nat) == ["control", "control", "natural", "natural"]
    assert {r["source"] for r in inj} == {"injected"} and len(inj) == 2


def test_public_run_id_hides_fault_but_is_stable_and_distinct():
    a = W.public_run_id("MH_01_easy__blur_length9", "MH_01_easy", True)
    assert a == W.public_run_id("MH_01_easy__blur_length9", "MH_01_easy", True)
    assert a.startswith("MH_01_easy__inj-") and "blur" not in a
    assert a != W.public_run_id("MH_01_easy__blur_length15", "MH_01_easy", True)
    assert W.public_run_id("MH_01_easy__run1", "MH_01_easy", False) == "MH_01_easy__run1"


def test_contact_sheet_layout_and_determinism():
    frames = [np.full((48, 64), 20 * i, np.uint8) for i in range(12)]
    a, b = W.contact_sheet(frames), W.contact_sheet(frames)
    assert a.size == (4 * 188, 3 * 141) and a.tobytes() == b.tobytes()
    assert a.getpixel((10, 10)) == 0 and a.getpixel((188 + 10, 10)) == 20  # tile order is time order
    with pytest.raises(ValueError):
        W.contact_sheet([])
    with pytest.raises(ValueError):
        W.contact_sheet([np.zeros((4, 8), np.uint8), np.zeros((8, 8), np.uint8)])


def test_check_set_warns_on_size_controls_and_duplicates():
    rows = [{"window_id": str(i), "source": "natural"} for i in range(10)]
    msgs = " | ".join(W.check_set(rows))
    assert "only 10 windows" in msgs and "controls are 0%" in msgs
    assert "duplicate" in " ".join(W.check_set(rows + rows[:1]))
    ok = [{"window_id": str(i), "source": "control" if i < 40 else "natural"} for i in range(160)]
    assert W.check_set(ok) == []


def test_csvs_never_carry_fault_and_ui_file_hides_everything_but_the_sheet(tmp_path):
    rows = [{"window_id": "abc", "sequence": "MH_01_easy", "run_id": "r", "source": "injected",
             "t0": 1.0, "t1": 6.0, "peak_err_m": 0.5, "sheet": "sheets/abc.png"}]
    W.write_csvs(rows, tmp_path / "w.csv", tmp_path / "ui.csv")
    head = (tmp_path / "w.csv").read_text().splitlines()[0]
    assert "fault" not in head and "params" not in head
    ui = (tmp_path / "ui.csv").read_text()
    assert ui.splitlines()[0] == "window_id,sheet"
    assert all(s not in ui for s in ("MH_01", "injected", "0.5", "blur"))
    with pytest.raises(ValueError):  # an unknown column (e.g. fault) is refused, not dropped
        W.write_csvs([dict(rows[0], fault="blur")], tmp_path / "w2.csv", tmp_path / "ui2.csv")


# ------------------------------------------------------------------ bagframes.py
def _stream(stamps):
    return [(s, np.full((2, 2), i, np.uint8)) for i, s in enumerate(stamps)]


def test_select_nearest_picks_closest_ties_earlier_and_handles_edges():
    msgs = _stream([1.0, 2.0, 3.0])
    got = bfr.select_nearest(iter(msgs), [2.4, 0.2, 2.5, 9.0, 3.0])  # unsorted, tie, edges, exact
    assert got[0][0] == 2.0 and got[1][0] == 1.0 and got[2][0] == 2.0   # 2.5 ties -> earlier
    assert got[3][0] == 3.0 and got[4][0] == 3.0


def test_select_nearest_streams_and_stops_early():
    seen = []

    def gen():
        for s, p in _stream([1.0, 2.0, 3.0, 4.0, 5.0]):
            seen.append(s)
            yield s, p
    bfr.select_nearest(gen(), [1.2])
    assert seen == [1.0, 2.0]  # did not read the whole bag


def test_extract_writes_pgm_and_index(tmp_path):
    idx = bfr.extract("unused.bag", {"k|0": [1.1, 2.9]}, str(tmp_path), stream=_stream([1.0, 2.0, 3.0]))
    assert [e["stamp"] for e in idx["k|0"]] == [1.0, 3.0]
    from PIL import Image
    assert np.asarray(Image.open(tmp_path / "k_0_00.pgm")).shape == (2, 2)
    assert json.loads((tmp_path / "index.json").read_text())["k|0"][1]["file"] == "k_0_01.pgm"
    with pytest.raises(SystemExit):
        bfr.extract("x", {"k": [1.0]}, str(tmp_path / "e"), stream=[])


# ------------------------------------------------------------------ window_cli.py
def test_docker_cmd_is_offline_readonly_and_unprivileged(tmp_path):
    cmd = cli.docker_cmd("img@sha256:" + "a" * 64, tmp_path / "x" / "b.bag", tmp_path / "w", 1000, 1000)
    s = " ".join(cmd)
    assert "--network none" in s and f"{tmp_path / 'x'}:/in:ro" in s and "--user 1000:1000" in s
    assert "vioreg.bagframes" in s and "/in/b.bag" in s


def test_discovery_uses_run1_only_and_skips_identity(repo):
    nat = cli.natural_runs(repo / "data/euroc", repo / "results")
    assert [r["run_id"] for r in nat] == ["MH_01_easy__run1"]
    inj = cli.injected_runs(repo, repo / "data/euroc", repo / "labels/fault_manifest.csv")
    assert [r["run_id"] for r in inj] == ["MH_01_easy__blur_length9"]


def test_end_to_end_with_fake_frames(repo):
    rc = cli.main(["--repo", str(repo), "--top-k", "3", "--control-k", "2", "--inj-top-k", "2"],
                  extract=_fake_extract)
    assert rc == 0
    meta = list(_read_csv(repo / "labels/windows.csv"))
    ui = list(_read_csv(repo / "labels/windows_ui.csv"))
    assert len(meta) == len(ui) == 3 + 2 + 2
    assert {r["source"] for r in meta} == {"natural", "control", "injected"}
    text = (repo / "labels/windows.csv").read_text()
    assert "blur" not in text and "length9" not in text and "__inj-" in text  # fault not leaked
    for r in meta:
        assert (repo / "labels" / r["sheet"]).exists() and (repo / "labels" / r["sheet"]).stat().st_size < 1_000_000
    spike = max((r for r in meta if r["source"] == "natural"), key=lambda r: float(r["peak_err_m"]))
    assert abs(float(spike["t0"]) - 130.0) < 0.1  # grid starts at the first pose kept after the trim
    assert not list((repo / "labels").glob("**/*.pgm"))  # scratch frames were removed


def test_unscorable_run_is_reported_and_skipped_not_dropped_silently(repo, capsys):
    """A missing bag is checked when frames are cut (a dry run or --reuse-sheets needs none)."""
    (repo / "data/euroc/MH_01_easy/MH_01_easy.bag").unlink()
    rc = cli.main(["--repo", str(repo), "--no-injected"], extract=_fake_extract)
    err = capsys.readouterr().err
    assert rc == 1 and "SKIPPED MH_01_easy__run1" in err and "bag missing" in err
    assert not (repo / "labels/windows.csv").exists()


@pytest.mark.filterwarnings("ignore:loadtxt. input contained no data")
def test_run_without_scorable_poses_is_reported(repo, capsys):
    """e.g. imu-noise x5: the run wrote no poses. Reported, and the set is just smaller."""
    tum = repo / "results/faults/MH_01_easy__blur_length9/MH_01_easy/run1.tum"
    tum.write_text("")
    assert _cli(repo, "--dry-run") == 0
    err = capsys.readouterr().err
    assert "SKIPPED MH_01_easy__blur_length9" in err


# ------------------------------------------------------------------ S7 run fixes
def _cli(repo, *extra, extract=_fake_extract):
    return cli.main(["--repo", str(repo), "--top-k", "3", "--control-k", "2", "--inj-top-k", "2",
                     *extra], extract=extract)


def _no_extract(bag, request, work):
    raise AssertionError("extraction must not run in this mode")


def _counting(log):
    def ex(bag, request, work):
        log.append((bag, sorted(request)))
        return _fake_extract(bag, request, work)
    return ex


def _sheet_ids(repo):
    return {p.stem for p in (repo / "labels/sheets").glob("*.png")}


# fix 3: only EuRoC sequence folders are natural runs
def test_natural_runs_ignore_folders_that_are_not_sequences(repo):
    t = np.arange(100.0, 160.0, 0.05)
    for d in ("s1", "variance/MH_01_easy", "faults", "sensitivity", "MH_01_easy_old", "mh_01_easy"):
        (repo / "results" / d).mkdir(parents=True, exist_ok=True)
        save_tum(_circle(t), repo / "results" / d / "run1.tum")
    nat = cli.natural_runs(repo / "data/euroc", repo / "results")
    assert [r["run_id"] for r in nat] == ["MH_01_easy__run1"]


# fix 2: results/faults, and a manifest row without results is reported
def test_injected_runs_read_results_faults(repo):
    inj = cli.injected_runs(repo, repo / "data/euroc", repo / "labels/fault_manifest.csv")
    assert [(r["run_id"], r["tum"].parent.parent.parent.name) for r in inj] == \
        [("MH_01_easy__blur_length9", "faults")]


def test_manifest_row_without_results_is_reported_not_silently_skipped(repo, capsys):
    with open(repo / "labels/fault_manifest.csv", "a") as f:
        f.write('MH_01_easy__gamma_gamma2.2,MH_01_easy,gamma,"{}",0,x\n')
    rc = _cli(repo, "--dry-run")
    err = capsys.readouterr().err
    assert rc == 0
    assert "SKIPPED MH_01_easy__gamma_gamma2.2" in err and "no results" in err
    assert "results/faults/MH_01_easy__gamma_gamma2.2/MH_01_easy" in err


def test_results_left_under_the_old_faulted_path_are_reported_missing(repo, capsys):
    import shutil
    src = repo / "results/faults/MH_01_easy__blur_length9"
    shutil.move(src, repo / "results/faulted/MH_01_easy__blur_length9")
    _cli(repo, "--dry-run")
    err = capsys.readouterr().err
    assert "SKIPPED MH_01_easy__blur_length9" in err and "no results" in err


def test_duplicate_manifest_rows_are_used_once_and_reported(repo, capsys):
    with open(repo / "labels/fault_manifest.csv", "a") as f:
        f.write('MH_01_easy__blur_length9,MH_01_easy,blur,"{}",0,y\n')
    inj = cli.injected_runs(repo, repo / "data/euroc", repo / "labels/fault_manifest.csv")
    assert [r["run_id"] for r in inj if not r.get("problem")] == ["MH_01_easy__blur_length9"]
    _cli(repo, "--dry-run")
    assert "duplicate manifest row" in capsys.readouterr().err


# addition A: --sheets-only writes sheets, never the CSVs, and resumes
def test_sheets_only_writes_sheets_and_no_csvs(repo, capsys):
    assert _cli(repo, "--sheets-only") == 0
    assert len(_sheet_ids(repo)) == 3 + 2 + 2
    assert not (repo / "labels/windows.csv").exists() and not (repo / "labels/windows_ui.csv").exists()


def test_sheets_only_with_seqs_does_not_touch_existing_csvs(repo):
    assert _cli(repo) == 0
    before = (repo / "labels/windows.csv").read_bytes(), (repo / "labels/windows_ui.csv").read_bytes()
    assert _cli(repo, "--sheets-only", "--seqs", "MH_01_easy") == 0
    assert ((repo / "labels/windows.csv").read_bytes(),
            (repo / "labels/windows_ui.csv").read_bytes()) == before


def test_sheets_only_resumes_and_extracts_only_missing_sheets(repo):
    assert _cli(repo, "--sheets-only") == 0
    victim = sorted(_sheet_ids(repo))[0]
    (repo / f"labels/sheets/{victim}.png").unlink()
    log = []
    assert _cli(repo, "--sheets-only", extract=_counting(log)) == 0
    assert [ids for _, ids in log] == [[victim]], "only the missing window is extracted"


def test_sheets_only_redoes_a_truncated_sheet(repo):
    assert _cli(repo, "--sheets-only") == 0
    victim = sorted(_sheet_ids(repo))[0]
    p = repo / f"labels/sheets/{victim}.png"
    p.write_bytes(p.read_bytes()[:100])  # interrupted write
    log = []
    assert _cli(repo, "--sheets-only", extract=_counting(log)) == 0
    assert [ids for _, ids in log] == [[victim]]


# fix 1: --reuse-sheets assembles the set from existing sheets only
def test_reuse_sheets_assembles_without_bags_or_extraction(repo):
    assert _cli(repo, "--sheets-only") == 0
    (repo / "data/euroc/MH_01_easy/MH_01_easy.bag").unlink()
    (repo / "data/faulted/MH_01_easy__blur_length9/MH_01_easy/MH_01_easy.bag").unlink()
    assert _cli(repo, "--reuse-sheets", extract=_no_extract) == 0
    meta = list(_read_csv(repo / "labels/windows.csv"))
    assert len(meta) == 7 and {r["window_id"] for r in meta} == _sheet_ids(repo)
    assert all(r["sheet"] == f"sheets/{r['window_id']}.png" for r in meta)


def test_reuse_sheets_fails_loudly_on_a_missing_sheet_and_writes_no_csvs(repo, capsys):
    assert _cli(repo, "--sheets-only") == 0
    victim = sorted(_sheet_ids(repo))[0]
    (repo / f"labels/sheets/{victim}.png").unlink()
    assert _cli(repo, "--reuse-sheets", extract=_no_extract) == 1
    err = capsys.readouterr().err
    assert "FAIL" in err and victim in err and "1 of 7 windows have no valid sheet" in err
    assert not (repo / "labels/windows.csv").exists()


def test_reuse_sheets_fails_on_a_corrupt_sheet(repo, capsys):
    assert _cli(repo, "--sheets-only") == 0
    victim = sorted(_sheet_ids(repo))[0]
    (repo / f"labels/sheets/{victim}.png").write_bytes(b"not a png")
    assert _cli(repo, "--reuse-sheets", extract=_no_extract) == 1
    assert victim in capsys.readouterr().err


def test_reuse_sheets_keeps_failing_closed_when_failure_follows_a_good_set(repo):
    assert _cli(repo, "--sheets-only") == 0
    assert _cli(repo, "--reuse-sheets") == 0
    victim = sorted(_sheet_ids(repo))[0]
    (repo / f"labels/sheets/{victim}.png").unlink()
    assert _cli(repo, "--reuse-sheets", extract=_no_extract) == 1
    assert not (repo / "labels/windows.csv").exists(), "a stale CSV from a good run is removed"
    assert not (repo / "labels/windows_ui.csv").exists()


def test_orphan_sheet_warns_and_never_enters_the_set(repo, capsys):
    assert _cli(repo, "--sheets-only") == 0
    import shutil
    any_sheet = next((repo / "labels/sheets").glob("*.png"))
    shutil.copy(any_sheet, repo / "labels/sheets/deadbeef00.png")
    assert _cli(repo, "--reuse-sheets", extract=_no_extract) == 0
    err = capsys.readouterr().err
    assert "WARNING" in err and "deadbeef00" in err and "not in the set" in err
    assert "deadbeef00" not in (repo / "labels/windows.csv").read_text()
    assert "deadbeef00" not in (repo / "labels/windows_ui.csv").read_text()


def test_stale_ids_after_a_rerun_do_not_pass(repo, capsys):
    """A re-run that moves the worst windows gives new ids: the old sheets are orphans
    (warned, kept out) and the new windows have no sheet, so the final step fails."""
    assert _cli(repo, "--sheets-only") == 0
    old = _sheet_ids(repo)
    from vioreg.euroc_io import load_tum
    tum = repo / "results/MH_01_easy/run1.tum"
    tr = load_tum(tum)
    p = tr.p.copy()
    p[(tr.t >= 130) & (tr.t < 133)] -= 0.6          # old spike gone
    p[(tr.t >= 145) & (tr.t < 148)] += 0.9          # new spike elsewhere
    save_tum(Trajectory(tr.t, p, tr.q), tum)
    assert _cli(repo, "--reuse-sheets", extract=_no_extract) == 1
    err = capsys.readouterr().err
    assert "no valid sheet" in err and "not in the set" in err
    assert not (repo / "labels/windows.csv").exists()
    assert old == _sheet_ids(repo), "reuse mode never deletes sheets"


def test_sheets_only_and_reuse_sheets_are_exclusive(repo):
    with pytest.raises(SystemExit):
        _cli(repo, "--sheets-only", "--reuse-sheets")


def test_sheets_are_written_atomically(repo, monkeypatch):
    """An extraction that dies mid-write leaves no PNG under the final name."""
    calls = {"n": 0}
    orig = W.contact_sheet

    def flaky(frames):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("killed")
        return orig(frames)
    monkeypatch.setattr(W, "contact_sheet", flaky)
    with pytest.raises(RuntimeError):
        _cli(repo, "--sheets-only")
    assert len(_sheet_ids(repo)) == 1 and not list((repo / "labels/sheets").glob("*.tmp*"))


def test_reuse_sheets_with_no_windows_fails_and_removes_old_csvs(repo):
    import shutil
    assert _cli(repo, "--sheets-only") == 0 and _cli(repo, "--reuse-sheets") == 0
    shutil.rmtree(repo / "results")
    (repo / "results").mkdir()
    assert _cli(repo, "--reuse-sheets", extract=_no_extract) == 1
    assert not (repo / "labels/windows.csv").exists()
    assert not (repo / "labels/windows_ui.csv").exists()


# --fault: restrict injected runs to the agreed fault set (S6 left 14 faults per sequence)
def _add_fault_run(repo, rid, fault, params):
    t = np.arange(100.0, 160.0, 0.05)
    d = repo / f"results/faults/{rid}/MH_01_easy"
    d.mkdir(parents=True)
    save_tum(_circle(t), d / "run1.tum")
    (repo / f"data/faulted/{rid}/MH_01_easy").mkdir(parents=True)
    (repo / f"data/faulted/{rid}/MH_01_easy/MH_01_easy.bag").write_bytes(b"")
    with open(repo / "labels/fault_manifest.csv", "a") as f:
        f.write(f'{rid},MH_01_easy,{fault},"{json.dumps(params, sort_keys=True).replace(chr(34), chr(34) * 2)}",0,x\n')


def _with_s6_extras(repo):
    _add_fault_run(repo, "MH_01_easy__blur_length25", "blur", {"length": 25})
    _add_fault_run(repo, "MH_01_easy__time-offset_offset_ms100", "time-offset", {"offset_ms": 100.0})
    _add_fault_run(repo, "MH_01_easy__time-offset_offset_ms40", "time-offset", {"offset_ms": 40.0})
    return repo


def test_fault_filter_keeps_only_matching_manifest_rows(repo):
    _with_s6_extras(repo)
    inj = cli.injected_runs(repo, repo / "data/euroc", repo / "labels/fault_manifest.csv",
                            faults=["blur=25", "time-offset=100"])
    assert sorted(r["run_id"] for r in inj if not r.get("problem")) == \
        ["MH_01_easy__blur_length25", "MH_01_easy__time-offset_offset_ms100"]


def test_no_fault_filter_keeps_every_row_as_before(repo):
    _with_s6_extras(repo)
    inj = cli.injected_runs(repo, repo / "data/euroc", repo / "labels/fault_manifest.csv")
    assert len([r for r in inj if not r.get("problem")]) == 4


def test_a_fault_spec_with_no_manifest_row_is_reported(repo, capsys):
    _with_s6_extras(repo)
    assert _cli(repo, "--dry-run", "--fault", "blur=25", "--fault", "gamma=2.2") == 0
    err = capsys.readouterr().err
    assert "SKIPPED MH_01_easy fault gamma=2.2" in err and "no manifest row" in err


def test_fault_filter_reaches_the_final_step(repo):
    """Filtered runs without bags must not make --reuse-sheets fail."""
    _with_s6_extras(repo)
    args = ("--fault", "blur=25", "--fault", "time-offset=100")
    assert _cli(repo, "--sheets-only", *args) == 0
    (repo / "data/faulted/MH_01_easy__time-offset_offset_ms40/MH_01_easy/MH_01_easy.bag").unlink()
    assert _cli(repo, "--reuse-sheets", *args, extract=_no_extract) == 0
    meta = list(_read_csv(repo / "labels/windows.csv"))
    assert sum(r["source"] == "injected" for r in meta) == 2 * 2


def test_bad_fault_spec_is_refused(repo):
    with pytest.raises(SystemExit):
        _cli(repo, "--dry-run", "--fault", "blur=25;rm")


# decision 2 (2026-11-17): injected windows never overlap natural/control windows in time
def _overlap(a, b):
    return a["t0"] < b["t1"] and b["t0"] < a["t1"]


def test_run_windows_avoid_takes_next_worst_non_overlapping():
    t = np.arange(0, 60, 0.05)
    err = np.full_like(t, 0.05)
    err[(t >= 30) & (t < 35)] = 0.9   # worst
    err[(t >= 10) & (t < 15)] = 0.5   # second
    err[(t >= 50) & (t < 55)] = 0.3   # third
    rows = W.run_windows("b", "S", t, err, injected=True, top_k=2, control_k=0,
                         avoid=[(31.0, 36.0)])
    assert [r["t0"] for r in rows] == [10.0, 50.0]


def test_run_windows_avoid_touching_edges_is_not_overlap():
    t = np.arange(0, 60, 0.05)
    err = np.full_like(t, 0.05)
    err[(t >= 30) & (t < 35)] = 0.9
    rows = W.run_windows("b", "S", t, err, injected=True, top_k=1, control_k=0,
                         avoid=[(25.0, 30.0), (35.0, 40.0)])
    assert [r["t0"] for r in rows] == [30.0]


def test_run_windows_avoid_returns_fewer_when_nothing_is_left():
    t = np.arange(0, 20, 0.05)          # 4 windows
    err = np.linspace(0, 1, len(t))
    rows = W.run_windows("b", "S", t, err, injected=True, top_k=3, control_k=0,
                         avoid=[(0.0, 10.0), (12.0, 13.0)])
    assert [r["t0"] for r in rows] == [15.0]  # 0-5, 5-10, 10-15 overlap; only 15-20 is free


def test_plan_injected_windows_never_overlap_natural_or_control(repo):
    """Fixture: the blur run has the SAME error as the natural run, so without the rule its
    worst windows would be exactly the natural ones."""
    runs = (cli.natural_runs(repo / "data/euroc", repo / "results")
            + cli.injected_runs(repo, repo / "data/euroc", repo / "labels/fault_manifest.csv"))
    rows, skipped = cli.plan(runs, 5.0, 3, 2, 2)
    assert skipped == []
    nat = [r for r in rows if r["source"] in ("natural", "control")]
    inj = [r for r in rows if r["source"] == "injected"]
    assert len(inj) == 2
    assert not any(_overlap(i, n) for i in inj for n in nat)


def test_plan_reports_a_short_injected_run(repo, capsys):
    # 12 windows of 5 s; natural 5 + control 5 leave 2 free; ask for 4 injected
    rc = _cli(repo, "--dry-run", "--top-k", "5", "--control-k", "5", "--inj-top-k", "4",
              "--trim-start-s", "0")
    out = capsys.readouterr()
    assert rc == 0
    assert "MH_01_easy__blur_length9: only 2 of 4 injected windows" in out.err
    assert "overlap" in out.err


# decision 1 (revised 2026-11-17): trim the start-up transient after each run's first pose
def test_trim_start_default_is_15_s_from_the_runs_first_pose():
    assert W.TRIM_START_S == 15.0
    assert cli._parser().parse_args([]).trim_start_s == W.TRIM_START_S


def test_trim_start_is_not_the_gate_skip_s():
    """The gate's skip_s counts from ground-truth start and removes no poses on the gated
    sequences; the trim counts from the run's first pose. They are different quantities."""
    import inspect
    assert "first pose" in inspect.getdoc(W.trim_start)


def test_no_window_starts_inside_the_trimmed_start(repo):
    runs = (cli.natural_runs(repo / "data/euroc", repo / "results")
            + cli.injected_runs(repo, repo / "data/euroc", repo / "labels/fault_manifest.csv"))
    rows, _ = cli.plan(runs, 5.0, 3, 2, 2)
    assert min(r["t0"] for r in rows) >= 100.0 + 15.0  # fixture runs start at t=100
    rows0, _ = cli.plan(runs, 5.0, 3, 2, 2, trim_start_s=0.0)
    assert min(r["t0"] for r in rows0) == 100.0


def test_trim_start_cuts_the_error_series():
    t = np.arange(0.0, 30.0, 0.05)
    err = np.arange(len(t), dtype=float)
    t2, e2 = W.trim_start(t, err, 15.0)
    assert t2[0] >= 15.0 and len(t2) == len(e2) and np.all(e2 == err[t >= 15.0])
    t3, _ = W.trim_start(t, err, 0.0)
    assert len(t3) == len(t)
