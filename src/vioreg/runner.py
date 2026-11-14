"""Run OpenVINS on one EuRoC sequence in the pinned Docker image.

    scripts/run_sequence.sh <SEQ> <OUT_ROOT>        (env: RUNS, MODE, CPUS, CONFIG_DIR, IMAGE, DATA)

The image is the pinned ref in docker/IMAGE_REF (<repo>@sha256:...; $IMAGE or --image
override it, same form required). Before any run, `docker image inspect` must confirm that
the local image carries exactly that registry digest; otherwise exit 1 and nothing runs.

Each run k (numbered after the highest existing run<k>.tum) writes into <OUT_ROOT>/<SEQ>/:
  run<k>.ov.txt       raw OpenVINS output (ov_eval pose_to_file, 20 columns)
  run<k>.tum          strict 8-column TUM, quaternions renormalised, xyzw
  run<k>.wall_s       wall-clock seconds of the whole `docker run` (container start to exit),
                      measured with time.monotonic() identically for every run and mode
  run<k>.config_hash  hash of the config the run actually used (image or CONFIG_DIR)
  run<k>.image_digest registry digest of the image the run used (verified before the runs)
  run<k>.log          container stdout/stderr
  run<k>.cmd          the exact docker command

No output reuse (S3b): run numbers skip past every run<k>.* file, a run refuses to start
if its run<k>.ov.txt already exists, and the trajectory must be modified after the run
started. (ov_eval's Recorder also deletes an existing output file when it opens.)

Fail closed: if the container exits non-zero, or produces no or unparsable output, the
run still claims its slot with an EMPTY run<k>.tum, so score.py counts it as a failed run
instead of silently scoring one run fewer.

Modes:
  serial    ros1_serial_msckf reads the bag directly (serial.launch). use_multi_threading_subs
            is forced false in that node, so image processing is synchronous with the IMU feed.
  realtime  run_subscribe_msckf + `rosbag play -r 1` (subscribe.launch dobag:=true); the
            estimator consumes topics in real time, so scheduling affects which data it sees.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config_hash as ch
from . import image_ref as ir
from .euroc_io import Trajectory, load_tum, save_tum

# From the bag_start comment in ov_msckf/launch/serial.launch at the pinned commit:
#   v1-2: 0, mh1: 40, mh2: 35, mh3: 17.5, mh4-5: 15  (skips the Machine Hall pick-up phase)
_BAG_START = {"MH_01": 40.0, "MH_02": 35.0, "MH_03": 17.5, "MH_04": 15.0, "MH_05": 15.0,
              "V1_01": 0.0, "V1_02": 0.0, "V1_03": 0.0, "V2_01": 0.0, "V2_02": 0.0,
              "V2_03": 0.0}
CONFIG_NAME = "euroc_mav"
RUN_ANY = re.compile(r"^run(\d+)\.")  # any per-run artifact: .tum .ov.txt .wall_s .log ...


def bag_start(seq: str) -> float:
    key = seq[:5]
    if key not in _BAG_START or not re.fullmatch(r"(MH|V1|V2)_0\d_[a-z]+", seq):
        raise ValueError(f"unknown EuRoC sequence {seq!r}")
    return _BAG_START[key]


def next_run_index(seq_dir: Path) -> int:
    if not Path(seq_dir).is_dir():
        return 1
    # Every run<k>.* artifact counts, not just .tum: a leftover run<k>.ov.txt whose .tum was
    # deleted must never become the "output" of a new run k (S3b reuse guard).
    ks = [int(m.group(1)) for p in Path(seq_dir).iterdir() if (m := RUN_ANY.match(p.name))]
    return max(ks, default=0) + 1


@dataclass
class RunConfig:
    seq: str
    out_root: Path
    data_root: Path
    image: str
    image_digest: str
    config_hash: str
    uid: int
    gid: int
    mode: str = "serial"
    cpus: str | None = None
    config_dir: Path | None = None
    bag_start_s: float | None = None  # None = upstream per-sequence default
    bag_rate: float | None = None     # realtime only; None = 1x


class RunnerError(RuntimeError):
    pass


@dataclass
class RunResult:
    k: int
    ok: bool
    wall_s: float
    reason: str = ""


def docker_cmd(c: RunConfig, k: int) -> list[str]:
    if c.mode not in ("serial", "realtime"):
        raise ValueError(f"mode must be serial or realtime, not {c.mode!r}")
    start = bag_start(c.seq) if c.bag_start_s is None else float(c.bag_start_s)
    if start < 0:
        raise ValueError(f"bag_start must be >= 0, not {start}")
    if c.bag_rate is not None and c.mode != "realtime":
        raise ValueError("bag_rate exists only in realtime mode (serial reads the bag directly)")
    seq_out = Path(c.out_root).resolve() / c.seq
    cmd = ["docker", "run", "--rm", "--user", f"{c.uid}:{c.gid}",
           "-e", "HOME=/tmp", "-e", "ROS_HOME=/tmp/.ros"]
    if c.cpus:
        cmd += ["--cpus", str(c.cpus)]
    cmd += ["-v", f"{Path(c.data_root).resolve() / c.seq}:/data:ro", "-v", f"{seq_out}:/out"]
    if c.config_dir:
        cmd += ["-v", f"{Path(c.config_dir).resolve()}:/ws/src/open_vins/config/{CONFIG_NAME}:ro"]
    launch = "serial.launch" if c.mode == "serial" else "subscribe.launch"
    cmd += [c.image, "roslaunch", "ov_msckf", launch,
            f"config:={CONFIG_NAME}", f"dataset:={c.seq}", f"bag:=/data/{c.seq}.bag",
            f"bag_start:={start}", "verbosity:=WARNING",
            "dosave:=true", f"path_est:=/out/run{k}.ov.txt"]
    if c.mode == "realtime":
        cmd += ["dobag:=true", f"bag_rate:={c.bag_rate if c.bag_rate is not None else 1}"]
    return cmd


def _execute(cmd: list[str], log_path: Path) -> int:
    with open(log_path, "wb") as log:
        return subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode


def convert_ov_to_tum(ov_path: Path, tum_path: Path) -> int:
    """20-column ov_eval output -> 8-column TUM. Returns pose count; raises on bad data."""
    raw = load_tum(ov_path)  # first 8 columns; enforces strictly increasing t
    q = raw.q / np.linalg.norm(raw.q, axis=1, keepdims=True)
    save_tum(Trajectory(raw.t, raw.p, q), tum_path)
    return len(raw)


def run_once(c: RunConfig, execute=_execute) -> RunResult:
    seq_dir = Path(c.out_root) / c.seq
    seq_dir.mkdir(parents=True, exist_ok=True)
    k = next_run_index(seq_dir)
    tum, ov = seq_dir / f"run{k}.tum", seq_dir / f"run{k}.ov.txt"
    if ov.exists():  # cannot happen via next_run_index; refuse rather than risk reuse
        raise RunnerError(f"{ov} already exists; refusing to run into it")
    cmd = docker_cmd(c, k)
    tum.write_text("")  # claim the slot first: a crash below leaves a failed run, not a gap
    (seq_dir / f"run{k}.config_hash").write_text(c.config_hash + "\n")
    (seq_dir / f"run{k}.image_digest").write_text(c.image_digest + "\n")
    (seq_dir / f"run{k}.cmd").write_text(" ".join(cmd) + "\n")
    started = time.time() - 1.0  # 1 s slack for filesystem timestamp granularity
    t0 = time.monotonic()
    rc = execute(cmd, seq_dir / f"run{k}.log")
    wall = time.monotonic() - t0
    (seq_dir / f"run{k}.wall_s").write_text(f"{wall:.3f}\n")
    reason = ""
    if rc != 0:
        reason = f"container exited {rc}"
    elif not ov.exists() or ov.stat().st_size == 0:
        reason = "no trajectory written"
    elif ov.stat().st_mtime < started:
        reason = "trajectory file is older than the run (not written by it)"
    else:
        try:
            if convert_ov_to_tum(ov, tum) < 2:
                reason = "fewer than 2 poses"
        except Exception as e:  # noqa: BLE001 - unparsable or non-monotonic output
            reason = f"unusable output: {e}"
    if reason:
        tum.write_text("")
    return RunResult(k=k, ok=not reason, wall_s=wall, reason=reason)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run OpenVINS on one EuRoC sequence.")
    ap.add_argument("seq")
    ap.add_argument("out_root", type=Path)
    ap.add_argument("--runs", type=int, default=int(os.environ.get("RUNS", "1")))
    ap.add_argument("--mode", default=os.environ.get("MODE", "serial"))
    ap.add_argument("--cpus", default=os.environ.get("CPUS") or None)
    ap.add_argument("--config-dir", type=Path,
                    default=Path(os.environ["CONFIG_DIR"]) if os.environ.get("CONFIG_DIR") else None)
    ap.add_argument("--image", default=None,
                    help="digest-pinned ref (default: $IMAGE, else docker/IMAGE_REF)")
    ap.add_argument("--data", type=Path, default=Path(os.environ.get("DATA", "data/euroc")))
    ap.add_argument("--bag-start", type=float,
                    default=float(os.environ["BAG_START"]) if os.environ.get("BAG_START") else None)
    ap.add_argument("--bag-rate", type=float,
                    default=float(os.environ["BAG_RATE"]) if os.environ.get("BAG_RATE") else None)
    a = ap.parse_args(argv)

    try:
        a.image = a.image or ir.pinned_ref()
        digest = ir.verify_image(a.image)
    except ir.ImageRefError as e:
        print(f"run_sequence: FAIL: image: {e}", file=sys.stderr)
        return 1
    bag = a.data / a.seq / f"{a.seq}.bag"
    if not bag.is_file():
        print(f"run_sequence: FAIL: {bag} missing (scripts/fetch_euroc.sh)", file=sys.stderr)
        return 1
    try:
        read = (ch.dir_reader(a.config_dir) if a.config_dir
                else ch.image_reader(a.image, CONFIG_NAME))
        chash = ch.config_hash(read)
    except ch.ConfigHashError as e:
        print(f"run_sequence: FAIL: config hash: {e}", file=sys.stderr)
        return 1
    c = RunConfig(seq=a.seq, out_root=a.out_root, data_root=a.data, image=a.image,
                  image_digest=digest, config_hash=chash, uid=os.getuid(), gid=os.getgid(), mode=a.mode,
                  cpus=a.cpus, config_dir=a.config_dir, bag_start_s=a.bag_start,
                  bag_rate=a.bag_rate)
    print(f"run_sequence: {a.seq} x{a.runs} mode={a.mode} cpus={a.cpus or 'unlimited'} "
          f"image={digest[:19]} config={a.config_dir or 'image'} ({chash[:12]}) "
          f"bag_start={a.bag_start if a.bag_start is not None else bag_start(a.seq)} "
          f"bag_rate={a.bag_rate or '-'}", flush=True)
    fails = 0
    for _ in range(a.runs):
        r = run_once(c)
        fails += not r.ok
        print(f"  run{r.k}: {'ok  ' if r.ok else 'FAIL'} wall={r.wall_s:.1f}s {r.reason}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
