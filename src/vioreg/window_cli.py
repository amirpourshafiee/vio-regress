"""Build the S7 window set: labels/windows.csv, labels/windows_ui.csv, labels/sheets/*.png.

    python -m vioreg.window_cli                       # all natural runs found + faulted copies
    python -m vioreg.window_cli --seqs MH_01_easy --top-k 8 --control-k 5
    python -m vioreg.window_cli --dry-run             # list the windows, extract no frames

Inputs (all under the repo, all gitignored): data/euroc/<seq>/<seq>.bag + ground truth,
results/<seq>/run<k>.tum (natural), results/faulted/<run_id>/<seq>/run<k>.tum with
data/faulted/<run_id>/<seq>/<seq>.bag (injected; run ids come from labels/fault_manifest.csv,
identity rows are skipped). By default only run1 of each natural sequence is used: serial
repeats of one sequence are near-duplicates, and counting them would inflate n without adding
information (windows of one sequence are already correlated; the kappa bootstrap resamples by
sequence). --all-runs lifts that.

Frames are cut from the bag inside the pinned image (digest verified, no network, bag mounted
read-only), written as PGM to a scratch dir, composed into one 4x3 PNG per window here, and the
scratch dir is deleted. Sheets under labels/sheets/ are gitignored: they are regenerable and
about 150 KB each. A run that cannot be scored or has no bag is reported and skipped, never
silently dropped. The fault never enters windows.csv (see vioreg.windows).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from . import image_ref as ir
from . import windows as W
from .euroc_io import load_euroc_gt, load_tum

ROOT = Path(__file__).resolve().parents[2]
GT_REL = Path("mav0/state_groundtruth_estimate0/data.csv")
MAX_SHEET_BYTES = 1_000_000  # CLAUDE.md: nothing over 1 MB in git; sheets stay well under


def natural_runs(data_root: Path, results: Path, seqs=None, all_runs: bool = False) -> list[dict]:
    out = []
    for sd in sorted(p for p in Path(results).iterdir() if p.is_dir() and p.name != "faulted"):
        if seqs and sd.name not in seqs:
            continue
        tums = sorted(sd.glob("run*.tum"), key=lambda p: int(p.stem[3:]))
        for tum in (tums if all_runs else tums[:1]):
            out.append({"run_id": f"{sd.name}__{tum.stem}", "sequence": sd.name, "injected": False,
                        "tum": tum, "bag": data_root / sd.name / f"{sd.name}.bag",
                        "gt": data_root / sd.name / GT_REL})
    return out


def injected_runs(repo: Path, data_root: Path, manifest: Path, seqs=None) -> list[dict]:
    out = []
    if not Path(manifest).exists():
        return out
    with open(manifest, newline="") as f:
        for r in csv.DictReader(f):
            if r["fault"] == "identity" or (seqs and r["sequence"] not in seqs):
                continue
            rid, seq = r["run_id"], r["sequence"]
            tums = sorted((repo / "results" / "faulted" / rid / seq).glob("run*.tum"))
            if not tums:
                continue
            out.append({"run_id": rid, "sequence": seq, "injected": True, "tum": tums[0],
                        "bag": repo / "data" / "faulted" / rid / seq / f"{seq}.bag",
                        "gt": data_root / seq / GT_REL})
    return out


def plan(runs, win_s, top_k, inj_top_k, control_k, align="posyaw", skip_s=0.0):
    """-> (rows, skipped). rows = metadata rows; skipped = [(run_id, reason)]."""
    rows, skipped = [], []
    for r in runs:
        try:
            est, gt = load_tum(r["tum"]), load_euroc_gt(r["gt"])
            t, err = W.error_series(est, gt, align, skip_s)
            if not r["bag"].exists():
                raise FileNotFoundError(f"bag missing: {r['bag']}")
        except Exception as e:  # noqa: BLE001 - reported, never silent
            skipped.append((r["run_id"], str(e)))
            continue
        for w in W.run_windows(r["run_id"], r["sequence"], t, err, injected=r["injected"],
                               win_s=win_s, top_k=inj_top_k if r["injected"] else top_k,
                               control_k=control_k):
            rows.append(dict(w, _bag=r["bag"]))
    return rows, skipped


def docker_cmd(ref, bag: Path, work: Path, uid=None, gid=None, repo=ROOT) -> list:
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    return ["docker", "run", "--rm", "--network", "none", "--user", f"{uid}:{gid}",
            "-e", "HOME=/tmp", "-e", "ROS_HOME=/tmp/.ros", "-e", "PYTHONPATH=/harness",
            "-v", f"{bag.parent}:/in:ro", "-v", f"{work}:/out",
            "-v", f"{Path(repo) / 'src'}:/harness:ro",
            ref, "python3", "-m", "vioreg.bagframes", "--bag", f"/in/{bag.name}",
            "--request", "/out/request.json", "--out", "/out/frames"]


def docker_extract(bag: Path, request: dict, work: Path) -> dict:
    ref = ir.pinned_ref()
    ir.verify_image(ref)
    (work / "request.json").write_text(json.dumps(request))
    subprocess.run(docker_cmd(ref, bag, work), check=True)
    return json.loads((work / "frames" / "index.json").read_text())


def build_sheets(rows, sheets_dir: Path, extract=docker_extract) -> None:
    """Fill row['sheet'] (relative, forward slashes) for every row; one extract call per bag."""
    sheets_dir.mkdir(parents=True, exist_ok=True)
    by_bag: dict[Path, list[dict]] = {}
    for r in rows:
        by_bag.setdefault(r["_bag"], []).append(r)
    from PIL import Image
    for bag, rs in by_bag.items():
        work = Path(tempfile.mkdtemp(prefix="vioreg_frames_"))
        try:
            req = {r["window_id"]: [float(t) for t in W.frame_times(r["t0"], r["t1"])] for r in rs}
            idx = extract(bag, req, work)
            for r in rs:
                frames = [np.asarray(Image.open(work / "frames" / e["file"]))
                          for e in idx[r["window_id"]]]
                out = sheets_dir / f"{r['window_id']}.png"
                W.contact_sheet(frames).save(out, optimize=True)
                if out.stat().st_size > MAX_SHEET_BYTES:
                    raise RuntimeError(f"{out.name} is over {MAX_SHEET_BYTES} bytes")
                r["sheet"] = f"sheets/{out.name}"
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--repo", type=Path, default=ROOT)
    p.add_argument("--data", type=Path, default=None, help="default <repo>/data/euroc")
    p.add_argument("--results", type=Path, default=None, help="default <repo>/results")
    p.add_argument("--seqs", nargs="*", default=None)
    p.add_argument("--all-runs", action="store_true")
    p.add_argument("--no-injected", action="store_true")
    p.add_argument("--win-s", type=float, default=5.0)
    p.add_argument("--top-k", type=int, default=8, help="worst windows per natural run")
    p.add_argument("--control-k", type=int, default=5, help="best windows per natural run")
    p.add_argument("--inj-top-k", type=int, default=3, help="worst windows per faulted run")
    p.add_argument("--skip-s", type=float, default=0.0)
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv=None, extract=docker_extract) -> int:
    a = _parser().parse_args(argv)
    repo = a.repo
    data = a.data or repo / "data" / "euroc"
    results = a.results or repo / "results"
    runs = natural_runs(data, results, a.seqs, a.all_runs)
    if not a.no_injected:
        runs += injected_runs(repo, data, repo / "labels" / "fault_manifest.csv", a.seqs)
    rows, skipped = plan(runs, a.win_s, a.top_k, a.inj_top_k, a.control_k, skip_s=a.skip_s)
    for rid, why in skipped:
        print(f"window_cli: SKIPPED {rid}: {why}", file=sys.stderr)
    by = {s: sum(r["source"] == s for r in rows) for s in W.SOURCES}
    print(f"window_cli: {len(rows)} windows from {len(runs) - len(skipped)} runs "
          f"({by['natural']} natural, {by['injected']} injected, {by['control']} control)")
    if a.dry_run or not rows:
        return 0 if rows else 1
    build_sheets(rows, repo / "labels" / "sheets", extract)
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    W.write_csvs(clean, repo / "labels" / "windows.csv", repo / "labels" / "windows_ui.csv")
    for w in W.check_set(clean):
        print(f"window_cli: WARNING {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
