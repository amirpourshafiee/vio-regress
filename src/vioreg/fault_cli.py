"""Create ONE faulted copy of a EuRoC sequence (S6). Never touches the original data.

    python -m vioreg.fault_cli time-offset --offset-ms 5 --seq MH_01_easy
    python -m vioreg.fault_cli imu-noise --sigma-w 0.01 --sigma-a 0.2 --seed 0 --seq MH_01_easy
    python -m vioreg.fault_cli gamma --gamma 2.2 --seq MH_01_easy
    python -m vioreg.fault_cli blur --length 9 --seq MH_01_easy
    python -m vioreg.fault_cli drop-frames --keep-every 2 --seq MH_01_easy
    python -m vioreg.fault_cli identity --seq MH_01_easy          # control: no change
    python -m vioreg.fault_cli spec blur=9 --seq MH_01_easy --print-data-root   # nightly input

Writes data/faulted/<seq>__<fault>_<params>/<seq>/<seq>.bag. The inner <seq>/ makes the
directory a drop-in DATA root for scripts/run_sequence.sh. It also writes fault.json next to
it and appends one row to labels/fault_manifest.csv (run_id, sequence, fault, params, seed,
created_utc). The manifest is the ONLY record of the fault: result JSONs stay frozen, and
the manifest is hidden from the labeling UI.

Safety: the output root must be <repo>/data/faulted and must not resolve inside the original
data root (e.g. via a symlink). An existing copy is never overwritten. The original bag is
mounted read-only into the container, and its sha256 is compared before and after; on any
failure the partial copy is removed and no manifest row is written.

The rewrite itself runs vioreg.bagfault inside the pinned image (docker/IMAGE_REF, digest
verified), which has rosbag. Exit codes: 0 ok, 1 the rewrite or a check failed, 2 refused.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import bagfault as bf
from . import image_ref as ir

ROOT = Path(__file__).resolve().parents[2]
SEQ_RE = re.compile(r"^(MH|V1|V2)_0[1-5]_[a-z]+$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.+=-]+$")
MANIFEST_FIELDS = ["run_id", "sequence", "fault", "params", "seed", "created_utc"]

# The nightly `fault` input: a strict allow-list, written so the SAME text works as a bash
# ERE ([[ =~ ]]) and a Python regex. Numbers: up to 4 digits, optional 1-4 decimals.
_N = r"[0-9]{1,4}(\.[0-9]{1,4})?"
SPEC_PATTERN = (rf"^(imu-noise={_N},{_N}|gamma={_N}|blur=[0-9]{{1,4}}|"
                rf"drop-frames=[0-9]{{1,4}}|time-offset=-?{_N})$")
SPEC_PARAMS = {"imu-noise": ("sigma_w", "sigma_a"), "gamma": ("gamma",), "blur": ("length",),
               "drop-frames": ("keep_every",), "time-offset": ("offset_ms",)}


class Refused(RuntimeError):
    pass


def parse_spec(spec: str) -> tuple:
    """'blur=9' -> ('blur', {'length': 9}); anything off the allow-list raises Refused, and
    out-of-range values raise BagFaultError (same checks as the subcommands)."""
    if not isinstance(spec, str) or not re.fullmatch(SPEC_PATTERN, spec):
        raise Refused(f"fault spec {spec!r} is not on the allow-list ({SPEC_PATTERN})")
    fault, values = spec.split("=", 1)
    names = SPEC_PARAMS[fault]
    nums = values.split(",")
    if len(nums) != len(names):
        raise Refused(f"{fault} takes {len(names)} value(s)")
    params = {k: float(v) for k, v in zip(names, nums)}
    return fault, bf.validate(fault, params)


def _num(v: float) -> str:
    return f"{v:g}"


def run_id(seq: str, fault: str, params: dict, seed: int) -> str:
    parts = [f"{k}{_num(v)}" for k, v in params.items()]
    if fault in bf.RANDOM_FAULTS:
        parts.append(f"seed{seed}")
    rid = f"{seq}__{fault}_{'-'.join(parts) or 'none'}"
    if not RUN_ID_RE.fullmatch(rid):
        raise Refused(f"bad run id {rid!r}")
    return rid


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def output_dir(repo_root: Path, out_root: Path, data_root: Path, rid: str) -> Path:
    allowed = (repo_root / "data" / "faulted").resolve()
    real_root = Path(out_root).resolve()
    if real_root != allowed:
        raise Refused(f"output root must be {allowed}, not {real_root}")
    original = Path(data_root).resolve()
    if _inside(real_root, original) or _inside(original, real_root):
        raise Refused(f"output root {real_root} overlaps the original data {original}")
    out = (real_root / rid).resolve()
    if not _inside(out, allowed) or _inside(out, original):
        raise Refused(f"{out} is outside {allowed} or inside the original data")
    if out.exists():
        raise Refused(f"{out} already exists; refusing to overwrite a faulted copy")
    return out


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def docker_cmd(ref, src_bag: Path, out_bag: Path, fault, params, seed, repo_root=ROOT,
               uid=None, gid=None) -> list:
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    return ["docker", "run", "--rm", "--network", "none", "--user", f"{uid}:{gid}",
            "-e", "HOME=/tmp", "-e", "ROS_HOME=/tmp/.ros", "-e", "PYTHONPATH=/harness",
            "-v", f"{src_bag.parent}:/in:ro", "-v", f"{out_bag.parent}:/out",
            "-v", f"{Path(repo_root) / 'src'}:/harness:ro",
            ref, "python3", "-m", "vioreg.bagfault",
            "--in", f"/in/{src_bag.name}", "--out", f"/out/{out_bag.name}",
            "--fault", fault, "--params", json.dumps(params, sort_keys=True),
            "--seed", str(seed), "--summary", "/out/bagfault_summary.json"]


def docker_apply(src_bag: Path, out_bag: Path, fault, params, seed) -> dict:
    ref = ir.pinned_ref()
    ir.verify_image(ref)
    subprocess.run(docker_cmd(ref, src_bag, out_bag, fault, params, seed), check=True)
    summary = out_bag.parent / "bagfault_summary.json"
    return json.loads(summary.read_text())


def append_manifest(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS, lineterminator="\n")
        if new:
            w.writeheader()
        w.writerow(row)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="fault", required=True)
    flags = {"sigma_w": "--sigma-w", "sigma_a": "--sigma-a", "gamma": "--gamma",
             "length": "--length", "keep_every": "--keep-every", "offset_ms": "--offset-ms"}
    for fault, spec in bf.SPEC.items():
        p = sub.add_parser(fault)
        for k, (typ, _ok, desc) in spec.items():
            p.add_argument(flags[k], dest=k, type=typ, required=True, help=desc)
        p.add_argument("--seq", required=True)
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("--data", default=None, help="original EuRoC root (default data/euroc)")
        p.add_argument("--out-root", default=None, help="must be data/faulted (the default)")
    p = sub.add_parser("spec", help="one fault as NAME=VALUE[,VALUE] (the nightly input form)")
    p.add_argument("spec")
    p.add_argument("--seq", required=True)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--data", default=None)
    p.add_argument("--out-root", default=None)
    for q in sub.choices.values():
        q.add_argument("--print-data-root", action="store_true",
                       help="print the DATA root for run_sequence.sh as the last line")
    return ap


def main(argv=None, repo_root: Path = ROOT, apply=docker_apply, now=None) -> int:
    a = _parser().parse_args(argv)
    if not SEQ_RE.fullmatch(a.seq):
        raise SystemExit(f"fault_cli: bad sequence name {a.seq!r}")
    repo_root = Path(repo_root)
    data_root = Path(a.data) if a.data else repo_root / "data" / "euroc"
    out_root = Path(a.out_root) if a.out_root else repo_root / "data" / "faulted"
    try:
        if a.fault == "spec":
            fault, params = parse_spec(a.spec)
        else:
            fault = a.fault
            params = bf.validate(fault, {k: getattr(a, k) for k in bf.SPEC[fault]})
        rid = run_id(a.seq, fault, params, a.seed)
        src = (data_root / a.seq / f"{a.seq}.bag").resolve()
        if not src.is_file():
            raise Refused(f"original bag {src} missing (scripts/fetch_euroc.sh)")
        out_dir = output_dir(repo_root, out_root, data_root, rid)
    except (bf.BagFaultError, Refused) as e:
        print(f"fault_cli: REFUSED: {e}", file=sys.stderr)
        return 2

    before = sha256(src)
    out_bag = out_dir / a.seq / f"{a.seq}.bag"
    out_bag.parent.mkdir(parents=True)
    try:
        summary = apply(src, out_bag, fault, params, a.seed)
        after = sha256(src)
        if after != before:
            raise RuntimeError(f"ORIGINAL BAG CHANGED: sha256 {before} -> {after}")
        if not out_bag.is_file() or out_bag.stat().st_size == 0:
            raise RuntimeError("no faulted bag written")
    except Exception as e:  # noqa: BLE001 - any failure: remove the partial copy, no row
        shutil.rmtree(out_dir, ignore_errors=True)
        print(f"fault_cli: FAIL: {e}", file=sys.stderr)
        return 1

    created = (now or (lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))()
    meta = {"run_id": rid, "sequence": a.seq, "fault": fault, "params": params,
            "seed": a.seed, "created_utc": created, "original": str(src),
            "original_sha256": before, "bag": str(out_bag), "summary": summary}
    (out_dir / "fault.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    append_manifest(repo_root / "labels" / "fault_manifest.csv",
                    {"run_id": rid, "sequence": a.seq, "fault": fault,
                     "params": json.dumps(params, sort_keys=True), "seed": a.seed,
                     "created_utc": created})
    print(f"fault_cli: {rid} -> {out_bag}")
    print(f"  run:   RUNS=2 DATA={out_dir} bash scripts/run_sequence.sh {a.seq} results/faults/{rid}")
    if a.print_data_root:
        print(out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
