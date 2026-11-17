"""Build the S7 window set: labels/windows.csv, labels/windows_ui.csv, labels/sheets/*.png.

    python -m vioreg.window_cli                       # all natural runs found + faulted copies
    python -m vioreg.window_cli --seqs MH_01_easy --top-k 8 --control-k 5
    python -m vioreg.window_cli --dry-run             # list the windows, extract no frames
    python -m vioreg.window_cli --sheets-only --seqs MH_02_easy   # per sequence: cut missing
                                                      # sheets while its bags exist, no CSVs
    python -m vioreg.window_cli --reuse-sheets        # final step: no bags, no Docker; every
                                                      # window must already have a valid sheet

Long runs (11 sequences do not fit on disk at once): run --sheets-only per sequence, delete its
bags, then --reuse-sheets once. Window ids are deterministic (run id + window start), and a
sheet depends only on the bag and the window times, so a sheet cut earlier is the sheet the
final step would cut. --reuse-sheets fails (exit 1, no CSVs, stale CSVs removed) if any window
lacks a valid sheet, e.g. a re-run moved the worst windows; sheets whose id is not in the set
are warned about and never enter it.

Inputs (all under the repo, all gitignored): data/euroc/<seq>/<seq>.bag + ground truth,
results/<seq>/run<k>.tum (natural; only folders named like a EuRoC sequence), and
results/faults/<run_id>/<seq>/run<k>.tum with data/faulted/<run_id>/<seq>/<seq>.bag (injected;
the results path is the one fault_cli prints; run ids come from labels/fault_manifest.csv,
identity rows are skipped, a row without results or a duplicate row is reported). By default only run1 of each natural sequence is used: serial
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
from .fault_cli import SEQ_RE, parse_spec
from .euroc_io import load_euroc_gt, load_tum

ROOT = Path(__file__).resolve().parents[2]
GT_REL = Path("mav0/state_groundtruth_estimate0/data.csv")
MAX_SHEET_BYTES = 1_000_000  # CLAUDE.md: nothing over 1 MB in git; sheets stay well under
FAULT_RESULTS = Path("results") / "faults"  # where fault_cli tells you to write fault runs
SHEET_W = 4 * 188  # contact_sheet(cols=4, tile_w=188); height follows the camera aspect


def _run_key(p: Path) -> int:
    return int(p.stem[3:])


def natural_runs(data_root: Path, results: Path, seqs=None, all_runs: bool = False) -> list[dict]:
    out = []
    for sd in sorted(p for p in Path(results).iterdir() if p.is_dir() and SEQ_RE.fullmatch(p.name)):
        if seqs and sd.name not in seqs:
            continue
        tums = sorted(sd.glob("run*.tum"), key=_run_key)
        for tum in (tums if all_runs else tums[:1]):
            out.append({"run_id": f"{sd.name}__{tum.stem}", "sequence": sd.name, "injected": False,
                        "tum": tum, "bag": data_root / sd.name / f"{sd.name}.bag",
                        "gt": data_root / sd.name / GT_REL})
    return out


def _norm(params: dict) -> dict:
    return {k: float(v) for k, v in params.items()}


def parse_faults(specs) -> list[tuple]:
    """['blur=25', ...] -> [(spec, fault, params)]; off the fault_cli allow-list raises."""
    out = []
    for spec in specs:
        fault, params = parse_spec(spec)
        out.append((spec, fault, _norm(params)))
    return out


def injected_runs(repo: Path, data_root: Path, manifest: Path, seqs=None,
                  faults=None) -> list[dict]:
    """One entry per non-identity manifest row; with `faults` (fault_cli specs such as
    'blur=25'), only rows matching one of them, and a spec with no row for a sequence that
    has rows is reported. A row whose results are missing, or a repeat
    of a run_id already seen, is returned with a 'problem' so plan() reports it: a silent
    skip here once meant zero injected windows (results were under another folder)."""
    out, seen = [], set()
    want = parse_faults(faults) if faults else None
    matched: dict[str, set] = {}
    if not Path(manifest).exists():
        return out
    with open(manifest, newline="") as f:
        for r in csv.DictReader(f):
            if r["fault"] == "identity" or (seqs and r["sequence"] not in seqs):
                continue
            rid, seq = r["run_id"], r["sequence"]
            if want is not None:
                params = _norm(json.loads(r["params"] or "{}"))
                hit = [sp for sp, fa, pa in want if fa == r["fault"] and pa == params]
                matched.setdefault(seq, set()).update(hit)
                if not hit:
                    continue
            res = FAULT_RESULTS / rid / seq
            e = {"run_id": rid, "sequence": seq, "injected": True, "tum": None,
                 "bag": repo / "data" / "faulted" / rid / seq / f"{seq}.bag",
                 "gt": data_root / seq / GT_REL}
            if rid in seen:
                out.append(dict(e, problem="duplicate manifest row (the first row is used)"))
                continue
            seen.add(rid)
            tums = sorted((repo / res).glob("run*.tum"), key=_run_key)
            if not tums:
                out.append(dict(e, problem=f"no results: {res}/run*.tum not found"))
                continue
            out.append(dict(e, tum=tums[0]))
    for seq in sorted(matched):
        for sp, _fa, _pa in want:
            if sp not in matched[seq]:
                out.append({"run_id": f"{seq} fault {sp}", "sequence": seq, "injected": True,
                            "problem": "no manifest row for this fault on this sequence "
                                       "(fault_cli not run?)"})
    return out


def plan(runs, win_s, top_k, inj_top_k, control_k, align="posyaw", skip_s=0.0):
    """-> (rows, skipped). rows = metadata rows; skipped = [(run_id, reason)]."""
    rows, skipped = [], []
    for r in runs:
        if r.get("problem"):
            skipped.append((r["run_id"], r["problem"]))
            continue
        try:
            est, gt = load_tum(r["tum"]), load_euroc_gt(r["gt"])
            t, err = W.error_series(est, gt, align, skip_s)
        except Exception as e:  # noqa: BLE001 - reported, never silent
            skipped.append((r["run_id"], str(e)))
            continue
        for w in W.run_windows(r["run_id"], r["sequence"], t, err, injected=r["injected"],
                               win_s=win_s, top_k=inj_top_k if r["injected"] else top_k,
                               control_k=control_k):
            rows.append(dict(w, _bag=r["bag"], _run=r["run_id"]))
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


def sheet_rel(window_id: str) -> str:
    return f"sheets/{window_id}.png"


def sheet_ok(path: Path) -> bool:
    """A complete contact sheet: decodes fully as PNG, grayscale, SHEET_W wide."""
    from PIL import Image
    try:
        with Image.open(path) as im:
            ok = im.format == "PNG" and im.mode == "L" and im.width == SHEET_W and im.height > 0
            im.load()  # a truncated file fails here
        return ok
    except Exception:  # noqa: BLE001 - any unreadable file is simply not a valid sheet
        return False


def build_sheets(rows, sheets_dir: Path, extract=docker_extract, resume: bool = False) -> list:
    """Fill row['sheet'] (relative, forward slashes) for every row; one extract call per bag.
    resume=True keeps every valid existing sheet and extracts only the rest. A sheet is
    written to a temp name and renamed, so an interrupted run leaves no partial PNG.
    Returns [(run_id, reason)] for runs whose bag is missing (their rows are removed)."""
    sheets_dir.mkdir(parents=True, exist_ok=True)
    by_bag: dict[Path, list[dict]] = {}
    for r in rows:
        if resume and sheet_ok(sheets_dir / f"{r['window_id']}.png"):
            r["sheet"] = sheet_rel(r["window_id"])
            continue
        by_bag.setdefault(r["_bag"], []).append(r)
    skipped = []
    from PIL import Image
    for bag, rs in by_bag.items():
        if not Path(bag).exists():
            for rid in sorted({r["_run"] for r in rs}):
                skipped.append((rid, f"bag missing: {bag}"))
            for r in rs:
                rows.remove(r)
            continue
        work = Path(tempfile.mkdtemp(prefix="vioreg_frames_"))
        try:
            req = {r["window_id"]: [float(t) for t in W.frame_times(r["t0"], r["t1"])] for r in rs}
            idx = extract(bag, req, work)
            for r in rs:
                frames = [np.asarray(Image.open(work / "frames" / e["file"]))
                          for e in idx[r["window_id"]]]
                out = sheets_dir / f"{r['window_id']}.png"
                tmp = out.with_name(out.name + ".tmp")
                try:
                    W.contact_sheet(frames).save(tmp, format="PNG", optimize=True)
                    if tmp.stat().st_size > MAX_SHEET_BYTES:
                        raise RuntimeError(f"{out.name} is over {MAX_SHEET_BYTES} bytes")
                    os.replace(tmp, out)
                finally:
                    tmp.unlink(missing_ok=True)
                r["sheet"] = sheet_rel(r["window_id"])
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return skipped


def attach_existing_sheets(rows, sheets_dir: Path) -> list[str]:
    """--reuse-sheets: set row['sheet'] from existing valid sheets; return ids that have none."""
    missing = []
    for r in rows:
        if sheet_ok(sheets_dir / f"{r['window_id']}.png"):
            r["sheet"] = sheet_rel(r["window_id"])
        else:
            missing.append(r["window_id"])
    return missing


def orphan_sheets(rows, sheets_dir: Path) -> list[str]:
    """Sheet files whose id is not in this set (left by an earlier plan): never used."""
    ids = {r["window_id"] for r in rows}
    return sorted(p.stem for p in Path(sheets_dir).glob("*.png") if p.stem not in ids)


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
    p.add_argument("--fault", action="append", default=None, metavar="SPEC",
                   help="only faulted runs matching this fault_cli spec (repeatable), e.g. "
                        "--fault blur=25 --fault gamma=2.2 --fault time-offset=100")
    p.add_argument("--dry-run", action="store_true")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--sheets-only", action="store_true",
                      help="cut missing sheets (bags needed), keep valid ones, write no CSVs")
    mode.add_argument("--reuse-sheets", action="store_true",
                      help="final step: use existing sheets only (no bags, no Docker); "
                           "fail if any window lacks a valid sheet")
    return p


def main(argv=None, extract=docker_extract) -> int:
    ap = _parser()
    a = ap.parse_args(argv)
    try:
        if a.fault:
            parse_faults(a.fault)
    except Exception as e:  # noqa: BLE001 - Refused or out-of-range values
        ap.error(f"--fault: {e}")
    repo = a.repo
    data = a.data or repo / "data" / "euroc"
    results = a.results or repo / "results"
    runs = natural_runs(data, results, a.seqs, a.all_runs)
    if not a.no_injected:
        runs += injected_runs(repo, data, repo / "labels" / "fault_manifest.csv", a.seqs,
                              faults=a.fault)
    rows, skipped = plan(runs, a.win_s, a.top_k, a.inj_top_k, a.control_k, skip_s=a.skip_s)
    labels = repo / "labels"
    sheets = labels / "sheets"
    csvs = (labels / "windows.csv", labels / "windows_ui.csv")
    if not (a.dry_run or a.reuse_sheets) and rows:
        skipped += build_sheets(rows, sheets, extract, resume=a.sheets_only)
    for rid, why in skipped:
        print(f"window_cli: SKIPPED {rid}: {why}", file=sys.stderr)
    by = {s: sum(r["source"] == s for r in rows) for s in W.SOURCES}
    n_runs = len({r["_run"] for r in rows})
    print(f"window_cli: {len(rows)} windows from {n_runs} runs "
          f"({by['natural']} natural, {by['injected']} injected, {by['control']} control)")
    if a.reuse_sheets and not rows:
        for f in csvs:  # the final step failed: no CSV from an earlier set may survive it
            f.unlink(missing_ok=True)
    if a.dry_run or not rows:
        return 0 if rows else 1
    if a.sheets_only:
        print(f"window_cli: sheets only: {len(rows)} sheets present in {sheets}; CSVs not written")
        return 0
    if a.reuse_sheets:
        missing = attach_existing_sheets(rows, sheets)
        orphans = orphan_sheets(rows, sheets)
        if orphans:
            print(f"window_cli: WARNING {len(orphans)} sheet(s) not in the set, ignored "
                  f"(stale from an earlier plan?): {' '.join(orphans)}", file=sys.stderr)
        if missing:
            for f in csvs:  # never leave a CSV from an earlier, different set behind
                f.unlink(missing_ok=True)
            print(f"window_cli: FAIL: {len(missing)} of {len(rows)} windows have no valid sheet "
                  f"(run --sheets-only for their sequence with its bags present): "
                  f"{' '.join(missing)}", file=sys.stderr)
            return 1
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    W.write_csvs(clean, *csvs)
    for w in W.check_set(clean):
        print(f"window_cli: WARNING {w}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
