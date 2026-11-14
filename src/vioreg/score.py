"""Score VIO runs: results/<seq>/run*.tum + EuRoC ground truth -> results/<seq>.json.

    python -m vioreg.score --seqs MH_01_easy --out results

JSON contract (frozen; exact key sets are enforced by tests/test_score.py):
  sequence, backend, vio_commit, image_digest, harness_commit, config_hash, runs,
  align, skip_s, ate{ok,rmse,median,max,coverage,n}, rpe{ok,rmse,delta_s},
  per_run_ate_rmse, wall_s

Aggregation across runs (VIO is not bit-deterministic, so a sequence may have N runs):
  * per_run_ate_rmse lists every run's ATE RMSE in numeric run order (run1, run2, ...,
    run10); a run that could not be scored contributes null.
  * The top-level ate AND rpe blocks are copied from ONE representative run: the median
    run by ATE RMSE. For an even N the upper median (the worse of the two middle runs)
    is used, so ties break toward failing. Taking both blocks from the same run keeps
    them mutually consistent (they describe a run that actually happened, not a blend
    of medians of different runs). err.npy holds that run's per-pose ATE error.
  * Fail closed: if ANY run is missing, empty, unreadable or has ok=false, or there are
    zero runs, or ground truth is unreadable, or any of the four provenance fields is
    missing, then ate.ok = rpe.ok = false, the other ate/rpe fields are null (the key
    set never changes), the reason goes to stderr, and the exit code is 1. A median
    must never hide a crashed run.
  * wall_s is the median per-run VIO wall-clock time, read from run<k>.wall_s sidecars
    written by the runner. It is null unless every run has one; it is a performance
    figure, not a correctness one, so its absence does not fail the result.

Provenance:
  vio_commit     label vioreg.ov_ref of the Docker image (== /ws/OV_COMMIT, built in)
  image_digest   the REGISTRY digest of the pinned image (vioreg/image_ref.py): --image must be
                 <repo>@sha256:<64 hex> (default $IMAGE, else docker/IMAGE_REF), and
                 `docker image inspect` RepoDigests must list it. Never the local image ID,
                 which changes per rebuild and per Docker storage mode. Unconfirmed -> None
                 -> the result fails closed. Every run<k>.image_digest sidecar written by
                 the runner must equal it, like config_hash.
  harness_commit `git rev-parse HEAD`, with "-dirty" appended if src/, configs/ or
                 scripts/ have any uncommitted change, untracked files included
  config_hash    sha1 over estimator_config.yaml + the kalibr IMU and IMU-camera yamls it
                 references, contents concatenated in sorted file-name order (see
                 vioreg/config_hash.py). Read from the config baked into the image, or from
                 --config-dir when the runs mounted an override. Every run<k>.config_hash
                 sidecar written by scripts/run_sequence.sh must equal it, or the result
                 fails closed: a run made with another config is never scored under this hash.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import numpy as np

from . import config_hash as ch
from . import image_ref as ir
from .euroc_io import load_euroc_gt, load_tum
from .metrics import ate, rpe

RPE_DELTA_S = 1.0
BACKEND = "openvins"
PROVENANCE_KEYS = ("vio_commit", "image_digest", "harness_commit", "config_hash")
GT_REL = Path("mav0/state_groundtruth_estimate0/data.csv")
HARNESS_PATHS = ("src", "configs", "scripts")
RUN_RE = re.compile(r"^run(\d+)\.tum$")
ROOT = Path(__file__).resolve().parents[2]


class ScoreError(RuntimeError):
    pass


# ---------------------------------------------------------------- provenance
def _run(cmd: list[str], **kw) -> bytes:
    return subprocess.run(cmd, check=True, capture_output=True, timeout=120, **kw).stdout


def collect_provenance(image: str, config_name: str, repo: Path = ROOT,
                       config_dir: Path | None = None) -> dict:
    """Each field is None if it cannot be determined; the scorer then fails closed."""
    prov: dict = {"backend": BACKEND, **{k: None for k in PROVENANCE_KEYS}}
    try:
        info = ir.docker_inspect(image)
        prov["image_digest"] = ir.registry_digest(image, info.get("RepoDigests"))
        if prov["image_digest"] is None:
            print(f"score: {image} is not a digest-pinned ref confirmed by docker "
                  f"(RepoDigests {info.get('RepoDigests')}); image_digest unknown",
                  file=sys.stderr)
        ref = (info.get("Config", {}).get("Labels") or {}).get("vioreg.ov_ref")
        prov["vio_commit"] = ref if ref and re.fullmatch(r"[0-9a-f]{40}", ref) else None
    except Exception as e:  # noqa: BLE001 - any failure means "unknown", never a guess
        print(f"score: cannot inspect image {image}: {e}", file=sys.stderr)
    try:
        read = (ch.dir_reader(config_dir) if config_dir is not None
                else ch.image_reader(image, config_name, run=_run))
        prov["config_hash"] = ch.config_hash(read)
    except Exception as e:  # noqa: BLE001
        src = config_dir if config_dir is not None else f"{image}:{config_name}"
        print(f"score: cannot hash config from {src}: {e}", file=sys.stderr)
    try:
        sha = _run(["git", "-C", str(repo), "rev-parse", "HEAD"]).decode().strip()
        # Untracked files count too: a new uncommitted module in src/ would otherwise
        # let harness_commit name a commit that does not contain the code that ran.
        dirty = _run(["git", "-C", str(repo), "status", "--porcelain",
                      "--untracked-files=all", "--", *HARNESS_PATHS]).strip()
        prov["harness_commit"] = sha + ("-dirty" if dirty else "")
    except Exception as e:  # noqa: BLE001
        print(f"score: cannot read harness commit: {e}", file=sys.stderr)
    return prov


# ---------------------------------------------------------------- scoring
def find_runs(seq_dir: Path) -> list[tuple[int, Path]]:
    if not seq_dir.is_dir():
        return []
    runs = [(int(m.group(1)), p) for p in seq_dir.iterdir()
            if (m := RUN_RE.match(p.name)) and p.is_file()]
    return sorted(runs)


def _finite(*xs) -> bool:
    return all(isinstance(x, (int, float)) and math.isfinite(x) for x in xs)


def _score_run(path: Path, gt, align: str, skip_s: float) -> dict:
    try:
        est = load_tum(path)
    except Exception as e:  # empty, header-only, wrong column count, non-monotonic t
        raise ScoreError(f"{path.name}: unreadable trajectory ({e})") from e
    if len(est) < 2:
        raise ScoreError(f"{path.name}: {len(est)} poses")
    a = ate(est, gt, align=align, skip_s=skip_s)
    if not a.get("ok"):
        raise ScoreError(f"{path.name}: ATE not ok ({a.get('reason')})")
    r = rpe(est, gt, delta_s=RPE_DELTA_S)
    if not r.get("ok"):
        raise ScoreError(f"{path.name}: RPE not ok ({r.get('reason')})")
    if not _finite(a["rmse"], a["median"], a["max"], a["coverage"], r["rmse"]):
        raise ScoreError(f"{path.name}: non-finite metric")
    return {"ate": a, "rpe": r}


def _read_wall(seq_dir: Path, idx: list[int]) -> float | None:
    vals = []
    for k in idx:
        f = seq_dir / f"run{k}.wall_s"
        try:
            vals.append(float(f.read_text().strip()))
        except (OSError, ValueError):
            return None
    return float(np.median(vals)) if vals and _finite(*vals) else None


def score_sequence(seq: str, results: Path, gt_csv: Path, provenance: dict,
                   align: str, skip_s: float) -> tuple[dict, bool]:
    """Score one sequence, write results/<seq>.json (always) and err.npy (on success).

    Returns (result_dict, ok)."""
    results, seq_dir = Path(results), Path(results) / seq
    reasons: list[str] = []
    runs = find_runs(seq_dir)
    if not runs:
        reasons.append(f"no run*.tum files in {seq_dir}")

    gt = None
    try:
        gt = load_euroc_gt(gt_csv)
    except Exception as e:  # noqa: BLE001
        reasons.append(f"ground truth unreadable: {gt_csv} ({e})")

    scored: list[dict | None] = []
    for _, path in runs:
        if gt is None:
            scored.append(None)
            continue
        try:
            scored.append(_score_run(path, gt, align, skip_s))
        except ScoreError as e:
            reasons.append(str(e))
            scored.append(None)

    for k in PROVENANCE_KEYS:
        if not provenance.get(k):
            reasons.append(f"provenance field {k} missing")
    for k, _ in runs:
        side = seq_dir / f"run{k}.image_digest"
        if side.exists() and side.read_text().strip() != provenance.get("image_digest"):
            reasons.append(f"run{k} used image {side.read_text().strip()[:19]}, "
                           f"not the recorded {str(provenance.get('image_digest'))[:19]}")
        side = seq_dir / f"run{k}.config_hash"
        if side.exists() and side.read_text().strip() != provenance.get("config_hash"):
            reasons.append(f"run{k} used config {side.read_text().strip()[:12]}, "
                           f"not the recorded {str(provenance.get('config_hash'))[:12]}")

    per_run = [float(s["ate"]["rmse"]) if s else None for s in scored]
    ok = not reasons
    ate_out = {"ok": False, "rmse": None, "median": None, "max": None,
               "coverage": None, "n": None}
    rpe_out = {"ok": False, "rmse": None, "delta_s": RPE_DELTA_S}
    err_path = seq_dir / "err.npy"
    if ok:
        order = sorted(range(len(scored)), key=lambda i: scored[i]["ate"]["rmse"])
        rep = scored[order[len(order) // 2]]  # upper median for even N
        a, r = rep["ate"], rep["rpe"]
        ate_out = {"ok": True, "rmse": float(a["rmse"]), "median": float(a["median"]),
                   "max": float(a["max"]), "coverage": float(a["coverage"]), "n": int(a["n"])}
        rpe_out = {"ok": True, "rmse": float(r["rmse"]), "delta_s": RPE_DELTA_S}
        np.save(err_path, np.asarray(a["per_pose"], dtype=np.float64))
    elif err_path.exists():
        err_path.unlink()  # never leave a stale per-pose file next to a failed result

    out = {
        "sequence": seq,
        "backend": provenance.get("backend", BACKEND),
        **{k: provenance.get(k) for k in PROVENANCE_KEYS},
        "runs": len(runs),
        "align": align,
        "skip_s": float(skip_s),
        "ate": ate_out,
        "rpe": rpe_out,
        "per_run_ate_rmse": per_run,
        "wall_s": _read_wall(seq_dir, [k for k, _ in runs]) if runs else None,
    }
    results.mkdir(parents=True, exist_ok=True)
    (results / f"{seq}.json").write_text(json.dumps(out, indent=2, allow_nan=False) + "\n")
    for msg in reasons:
        print(f"score: {seq}: FAIL: {msg}", file=sys.stderr)
    return out, ok


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    import yaml

    ap = argparse.ArgumentParser(description="Score VIO runs against EuRoC ground truth.")
    ap.add_argument("--seqs", nargs="+", required=True)
    ap.add_argument("--out", default="results", help="results root (holds <seq>/run*.tum)")
    ap.add_argument("--data", default="data/euroc", help="EuRoC root from fetch_euroc.sh")
    ap.add_argument("--thresholds", default=str(ROOT / "configs" / "thresholds.yaml"))
    ap.add_argument("--image", default=None,
                    help="digest-pinned ref (default: $IMAGE, else docker/IMAGE_REF)")
    ap.add_argument("--config-name", default="euroc_mav")
    ap.add_argument("--config-dir", type=Path, default=None,
                    help="hash this local config dir (the one the runs mounted) "
                         "instead of the config baked into the image")
    a = ap.parse_args(argv)
    try:
        a.image = a.image or ir.pinned_ref()
    except ir.ImageRefError as e:
        print(f"score: {e}", file=sys.stderr)  # provenance then records image_digest None
        a.image = a.image or ""

    cfg = yaml.safe_load(Path(a.thresholds).read_text())
    prov = collect_provenance(image=a.image, config_name=a.config_name,
                              config_dir=a.config_dir)
    rc = 0
    for seq in a.seqs:
        if seq not in cfg.get("sequences", {}):
            print(f"score: {seq}: FAIL: not in {a.thresholds}", file=sys.stderr)
            rc = 1
            continue
        lim = {**cfg.get("defaults", {}), **cfg["sequences"][seq]}
        out, ok = score_sequence(seq, Path(a.out), Path(a.data) / seq / GT_REL, prov,
                                 align=lim.get("align", "posyaw"),
                                 skip_s=float(lim.get("skip_s", 0.0)))
        a_ = out["ate"]
        print(f"{seq}: {'OK  ' if ok else 'FAIL'} runs={out['runs']} "
              f"ate_rmse={a_['rmse']} coverage={a_['coverage']} rpe_rmse={out['rpe']['rmse']}")
        rc |= 0 if ok else 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
