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


def test_next_run_index(tmp_path):
    assert runner.next_run_index(tmp_path / "missing") == 1
    for name in ("run1.tum", "run2.tum", "run10.tum", "run11.ov.txt", "run12.wall_s", "x.tum"):
        (tmp_path / name).write_text("")
    assert runner.next_run_index(tmp_path) == 11


def cfg(tmp_path, **kw):
    base = dict(seq="MH_01_easy", out_root=tmp_path / "results", data_root=tmp_path / "data",
                image="img:tag", config_hash="f" * 40, uid=1000, gid=1000)
    base.update(kw)
    return runner.RunConfig(**base)


def test_serial_command(tmp_path):
    c = cfg(tmp_path)
    cmd = runner.docker_cmd(c, 3)
    assert cmd[:3] == ["docker", "run", "--rm"]
    assert "--cpus" not in cmd
    assert f"{tmp_path / 'data' / 'MH_01_easy'}:/data:ro" in cmd
    i = cmd.index("img:tag")
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
    i = cmd.index("img:tag")
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
