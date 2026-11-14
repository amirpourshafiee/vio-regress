"""Runner tests: command construction, run numbering, sidecars, fail-closed run slots.
No Docker: the executor is injected."""
import numpy as np
import pytest

from vioreg import runner
from vioreg.euroc_io import load_tum

OV_HEADER = "# timestamp(s) tx ty tz qx qy qz qw Pr11 Pr12 Pr13 Pr22 Pr23 Pr33 Pt11 Pt12 Pt13 Pt22 Pt23 Pt33\n"


def ov_rows(n=5, t0=1403636624.66262):
    cov = " ".join(["0.001"] * 12)
    # quaternion deliberately not unit length (as printed at 6 decimals)
    return "".join(f"{t0 + 0.05 * i:.5f} {i:.6f} 0.000000 0.000000 0.000000 0.000000 0.000000 1.000002 {cov}\n"
                   for i in range(n))


@pytest.mark.parametrize("seq,start", [("MH_01_easy", 40.0), ("MH_02_easy", 35.0),
                                       ("MH_03_medium", 17.5), ("MH_05_difficult", 15.0),
                                       ("V1_01_easy", 0.0), ("V2_02_medium", 0.0)])
def test_bag_start_follows_upstream_serial_launch(seq, start):
    assert runner.bag_start(seq) == start


def test_unknown_sequence_is_an_error():
    with pytest.raises(ValueError):
        runner.bag_start("MH_99_bogus")


def test_next_run_index_skips_past_every_run_artifact(tmp_path):
    """S3b: numbering looks at EVERY run<k>.* file, not just .tum, so a leftover
    run<k>.ov.txt (its .tum deleted) can never be picked up as a new run's output."""
    assert runner.next_run_index(tmp_path / "missing") == 1
    for name in ("run1.tum", "run2.tum", "run10.tum", "run11.ov.txt", "run12.wall_s", "x.tum",
                 "runner.txt"):
        (tmp_path / name).write_text("")
    assert runner.next_run_index(tmp_path) == 13


def cfg(tmp_path, **kw):
    base = dict(seq="MH_01_easy", out_root=tmp_path / "results", data_root=tmp_path / "data",
                image="img@sha256:" + "c" * 64, image_digest="sha256:" + "c" * 64,
                config_hash="f" * 40, uid=1000, gid=1000)
    base.update(kw)
    return runner.RunConfig(**base)


def test_serial_command(tmp_path):
    c = cfg(tmp_path)
    cmd = runner.docker_cmd(c, 3)
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "--cpus" not in cmd
    assert f"{tmp_path / 'data' / 'MH_01_easy'}:/data:ro" in cmd
    i = cmd.index("img@sha256:" + "c" * 64)  # docker runs the digest-pinned ref
    assert cmd[i + 1:i + 3] == ["roslaunch", "ov_msckf"] and cmd[i + 3] == "serial.launch"
    args = cmd[i + 4:]
    for a in ("config:=euroc_mav", "bag:=/data/MH_01_easy.bag", "bag_start:=40.0",
              "dosave:=true", "path_est:=/out/run3.ov.txt"):
        assert a in args


def test_cpus_and_config_mount(tmp_path):
    c = cfg(tmp_path, cpus="2", config_dir=tmp_path / "cfg")
    cmd = runner.docker_cmd(c, 1)
    assert cmd[cmd.index("--cpus") + 1] == "2"
    assert f"{tmp_path / 'cfg'}:/ws/src/open_vins/config/euroc_mav:ro" in cmd


def test_realtime_command_uses_subscribe_with_bag_playback(tmp_path):
    cmd = runner.docker_cmd(cfg(tmp_path, mode="realtime"), 1)
    i = cmd.index("img@sha256:" + "c" * 64)  # docker runs the digest-pinned ref
    assert cmd[i + 3] == "subscribe.launch"
    assert "dobag:=true" in cmd and "bag_rate:=1" in cmd and "bag_start:=40.0" in cmd


def test_bad_mode_rejected(tmp_path):
    with pytest.raises(ValueError):
        runner.docker_cmd(cfg(tmp_path, mode="fast"), 1)


def fake_exec(rows, rc=0, wall=0.0):
    def execute(cmd, log_path):
        out = [a for a in cmd if a.startswith("path_est:=")][0].split(":=")[1]
        host = log_path.parent / out.rsplit("/", 1)[1]
        if rows is not None:
            host.write_text(OV_HEADER + rows)
        log_path.write_text("fake log\n")
        return rc
    return execute


def test_run_once_writes_tum_and_sidecars(tmp_path):
    c = cfg(tmp_path)
    r = runner.run_once(c, execute=fake_exec(ov_rows()))
    d = tmp_path / "results" / "MH_01_easy"
    assert r.k == 1 and r.ok
    tr = load_tum(d / "run1.tum")
    assert len(tr) == 5 and np.allclose(np.linalg.norm(tr.q, axis=1), 1.0)
    assert (d / "run1.tum").read_text().splitlines()[0].count(" ") == 7  # 8 columns
    assert float((d / "run1.wall_s").read_text()) >= 0.0
    assert (d / "run1.config_hash").read_text().strip() == "f" * 40
    assert (d / "run1.image_digest").read_text().strip() == "sha256:" + "c" * 64
    assert (d / "run1.log").exists() and (d / "run1.ov.txt").exists()
    assert runner.run_once(c, execute=fake_exec(ov_rows())).k == 2


@pytest.mark.parametrize("rows,rc", [(None, 0), ("", 0), (ov_rows(), 1)])
def test_failed_run_still_claims_its_slot_with_empty_tum(tmp_path, rows, rc):
    """A crash must leave an empty run<k>.tum so score.py counts a failed run,
    never silently one run fewer."""
    c = cfg(tmp_path)
    r = runner.run_once(c, execute=fake_exec(rows, rc))
    d = tmp_path / "results" / "MH_01_easy"
    assert not r.ok
    assert (d / "run1.tum").exists() and (d / "run1.tum").read_text() == ""
    assert (d / "run1.wall_s").exists() and (d / "run1.config_hash").exists()


def test_non_monotonic_output_fails_the_run(tmp_path):
    rows = ov_rows(3)
    lines = rows.splitlines(keepends=True)
    r = runner.run_once(cfg(tmp_path), execute=fake_exec(lines[1] + lines[0] + lines[2]))
    assert not r.ok


# ---------------------------------------------------------------- S3b: no output reuse
def test_stale_output_of_a_deleted_run_is_never_reused(tmp_path):
    """run1.tum deleted, run1.ov.txt left behind, and the next container exits 0 without
    writing anything: the run must FAIL, not silently score the stale file."""
    d = tmp_path / "results" / "MH_01_easy"
    d.mkdir(parents=True)
    (d / "run1.ov.txt").write_text(OV_HEADER + ov_rows())
    r = runner.run_once(cfg(tmp_path), execute=fake_exec(None))
    assert r.k == 2 and not r.ok and "no trajectory" in r.reason
    assert (d / "run2.tum").read_text() == ""


def test_refuses_to_run_into_an_existing_output_file(tmp_path, monkeypatch):
    d = tmp_path / "results" / "MH_01_easy"
    d.mkdir(parents=True)
    (d / "run1.ov.txt").write_text(OV_HEADER + ov_rows())
    monkeypatch.setattr(runner, "next_run_index", lambda _d: 1)
    with pytest.raises(runner.RunnerError, match="already exists"):
        runner.run_once(cfg(tmp_path), execute=fake_exec(ov_rows()))


def test_output_older_than_the_run_is_rejected(tmp_path):
    """Defence in depth: whatever the container did, the trajectory it leaves must have
    been written during this run."""
    import os

    def execute(cmd, log_path):
        fake_exec(ov_rows())(cmd, log_path)
        out = log_path.parent / f"run{runner.next_run_index(log_path.parent) - 1}.ov.txt"
        os.utime(out, (1_000_000_000, 1_000_000_000))  # 2001
        return 0

    r = runner.run_once(cfg(tmp_path), execute=execute)
    assert not r.ok and "older than the run" in r.reason


def test_cmd_sidecar_records_exact_command(tmp_path):
    r = runner.run_once(cfg(tmp_path, bag_start_s=42.0), execute=fake_exec(ov_rows()))
    cmd = (tmp_path / "results" / "MH_01_easy" / f"run{r.k}.cmd").read_text()
    assert "bag_start:=42.0" in cmd and "serial.launch" in cmd


def test_bag_start_and_rate_overrides(tmp_path):
    c = cfg(tmp_path, bag_start_s=38.0)
    assert "bag_start:=38.0" in runner.docker_cmd(c, 1)
    rt = runner.docker_cmd(cfg(tmp_path, mode="realtime", bag_rate=2.0), 1)
    assert "bag_rate:=2.0" in rt and "bag_start:=40.0" in rt
    with pytest.raises(ValueError):
        runner.docker_cmd(cfg(tmp_path, bag_rate=2.0), 1)  # rate exists only for realtime
    with pytest.raises(ValueError):
        runner.docker_cmd(cfg(tmp_path, bag_start_s=-1.0), 1)


# ---------------------------------------------------------------- image pin (S5b)
def _bag(tmp_path):
    bag = tmp_path / "data" / "MH_01_easy" / "MH_01_easy.bag"
    bag.parent.mkdir(parents=True)
    bag.write_bytes(b"x")
    return ["MH_01_easy", str(tmp_path / "results"), "--data", str(tmp_path / "data"), "--runs", "1"]


def test_main_fails_before_any_run_if_image_digest_is_not_the_pinned_one(tmp_path, monkeypatch):
    from vioreg import image_ref as ir

    def bad(ref, inspect=None):
        raise ir.ImageRefError("pulled image digest is not the pinned one")
    monkeypatch.setattr(runner.ir, "verify_image", bad)
    monkeypatch.setattr(runner, "run_once", lambda c: pytest.fail("ran despite a bad image"))
    assert runner.main(_bag(tmp_path)) == 1
    assert not (tmp_path / "results").exists()


def test_main_fails_on_a_non_digest_image_ref(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "run_once", lambda c: pytest.fail("ran despite a tag ref"))
    assert runner.main([*_bag(tmp_path), "--image", "vioreg-openvins:v2.7"]) == 1


def test_main_defaults_to_pinned_ref_and_passes_the_verified_digest(tmp_path, monkeypatch):
    from vioreg import image_ref as ir
    monkeypatch.delenv("IMAGE", raising=False)
    pinned = ir.pinned_ref(env={})
    seen = {}
    monkeypatch.setattr(runner.ir, "verify_image",
                        lambda ref, inspect=None: seen.setdefault("ref", ref) and "sha256:" + "a" * 64)
    monkeypatch.setattr(runner.ch, "image_reader", lambda image, name: (lambda f: b""))
    monkeypatch.setattr(runner.ch, "config_hash", lambda read: "f" * 40)
    monkeypatch.setattr(runner, "run_once",
                        lambda c: seen.update(cfg=c) or runner.RunResult(k=1, ok=True, wall_s=0.0))
    assert runner.main(_bag(tmp_path)) == 0
    assert seen["ref"] == pinned and seen["cfg"].image == pinned
    assert seen["cfg"].image_digest == "sha256:" + "a" * 64
