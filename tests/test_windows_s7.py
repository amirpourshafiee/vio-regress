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
    (tmp_path / f"results/faulted/{rid}/MH_01_easy").mkdir(parents=True)
    save_tum(Trajectory(t, est_p, gt.q), tmp_path / f"results/faulted/{rid}/MH_01_easy/run1.tum")
    (tmp_path / f"data/faulted/{rid}/MH_01_easy").mkdir(parents=True)
    (tmp_path / f"data/faulted/{rid}/MH_01_easy/MH_01_easy.bag").write_bytes(b"")
    (tmp_path / "labels").mkdir()
    with open(tmp_path / "labels/fault_manifest.csv", "w") as f:
        f.write("run_id,sequence,fault,params,seed,created_utc\n"
                f"{rid},MH_01_easy,blur,\"{{}}\",0,x\n"
                "MH_01_easy__identity_none,MH_01_easy,identity,{},0,x\n")
    return tmp_path


W_GT = cli.GT_REL


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
    meta = list(csv.DictReader(open(repo / "labels/windows.csv")))
    ui = list(csv.DictReader(open(repo / "labels/windows_ui.csv")))
    assert len(meta) == len(ui) == 3 + 2 + 2
    assert {r["source"] for r in meta} == {"natural", "control", "injected"}
    text = (repo / "labels/windows.csv").read_text()
    assert "blur" not in text and "length9" not in text and "__inj-" in text  # fault not leaked
    for r in meta:
        assert (repo / "labels" / r["sheet"]).exists() and (repo / "labels" / r["sheet"]).stat().st_size < 1_000_000
    spike = max((r for r in meta if r["source"] == "natural"), key=lambda r: float(r["peak_err_m"]))
    assert float(spike["t0"]) == 130.0
    assert not list((repo / "labels").glob("**/*.pgm"))  # scratch frames were removed


def test_unscorable_run_is_reported_and_skipped_not_dropped_silently(repo, capsys):
    (repo / "data/euroc/MH_01_easy/MH_01_easy.bag").unlink()
    rc = cli.main(["--repo", str(repo), "--dry-run", "--no-injected"], extract=_fake_extract)
    err = capsys.readouterr().err
    assert rc == 1 and "SKIPPED MH_01_easy__run1" in err and "bag missing" in err
