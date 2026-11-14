"""Run-to-run variance analysis (session S3).

    python -m vioreg.variance --root results/variance --seqs ... --baseline-variant A_serial \
        --out-dir docs/variance

Per run: ATE and RPE are recomputed with the same score._score_run the scorer uses (so the
numbers are identical to score.py's), and the trajectory file is fingerprinted (sha1) so
"bit-identical runs" is measured, not inferred. A failed run (empty / unreadable / ok=false)
is kept with null metrics and its reason.

Outputs:
  baseline.json  per sequence: per-run ATE RMSE, per-run RPE RMSE, per-run ok, align,
                 skip_s, and the four provenance fields from results/<variant>/<seq>.json
                 (the baseline variant only; thresholds are NOT derived here)
  variance.json  per variant x sequence: spread statistics, failures, distinct trajectories
  spread.png     per-sequence strip plot of ATE RMSE per run, one column per variant
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from . import score
from .euroc_io import load_euroc_gt

PROVENANCE = ("vio_commit", "image_digest", "harness_commit", "config_hash")


class VarianceError(RuntimeError):
    pass


def spread(values) -> dict:
    ok = np.array([v for v in values if v is not None], dtype=float)
    out = {"n": int(len(ok)), "failed": int(sum(v is None for v in values))}
    if len(ok) == 0:
        return {**out, **{k: None for k in ("median", "mean", "std", "mad", "min", "max", "range")}}
    med = float(np.median(ok))
    return {**out, "median": med, "mean": float(ok.mean()),
            "std": float(ok.std(ddof=1)) if len(ok) > 1 else 0.0,
            "mad": float(1.4826 * np.median(np.abs(ok - med))),
            "min": float(ok.min()), "max": float(ok.max()), "range": float(ok.max() - ok.min())}


def _num(seq_dir: Path, name: str):
    try:
        return float((seq_dir / name).read_text().strip())
    except (OSError, ValueError):
        return None


def collect_runs(variant_root: Path, seq: str, data_root: Path, align: str,
                 skip_s: float) -> list[dict]:
    seq_dir = Path(variant_root) / seq
    gt = load_euroc_gt(Path(data_root) / seq / score.GT_REL)
    runs = []
    for k, path in score.find_runs(seq_dir):
        r = {"k": k, "ok": False, "ate_rmse": None, "rpe_rmse": None, "coverage": None,
             "n": None, "traj_sha1": None, "wall_s": _num(seq_dir, f"run{k}.wall_s"),
             "reason": ""}
        try:
            m = score._score_run(path, gt, align, skip_s)
            r.update(ok=True, ate_rmse=float(m["ate"]["rmse"]), rpe_rmse=float(m["rpe"]["rmse"]),
                     coverage=float(m["ate"]["coverage"]), n=int(m["ate"]["n"]),
                     traj_sha1=hashlib.sha1(path.read_bytes()).hexdigest())
        except score.ScoreError as e:
            r["reason"] = str(e)
        runs.append(r)
    return runs


def summarise(runs: list[dict]) -> dict:
    shas = {r["traj_sha1"] for r in runs if r["ok"]}
    return {"runs": len(runs), "failed": sum(not r["ok"] for r in runs),
            "failed_reasons": [f"run{r['k']}: {r['reason']}" for r in runs if not r["ok"]],
            "distinct_trajectories": len(shas),
            "ate_rmse": spread([r["ate_rmse"] for r in runs]),
            "rpe_rmse": spread([r["rpe_rmse"] for r in runs]),
            "coverage_min": min((r["coverage"] for r in runs if r["ok"]), default=None),
            "wall_s": spread([r["wall_s"] for r in runs])}


def _limits(thresholds: dict, seq: str) -> tuple[str, float]:
    if seq not in thresholds.get("sequences", {}):
        raise VarianceError(f"{seq} not in thresholds")
    lim = {**thresholds.get("defaults", {}), **thresholds["sequences"][seq]}
    return lim.get("align", "posyaw"), float(lim.get("skip_s", 0.0))


def build_baseline(variant_root: Path, seqs, data_root: Path, thresholds: dict) -> dict:
    out = {"variant": Path(variant_root).name, "sequences": {}}
    for seq in seqs:
        f = Path(variant_root) / f"{seq}.json"
        if not f.exists():
            raise VarianceError(f"{f} missing: run score.py on this variant first")
        res = json.loads(f.read_text())
        align, skip_s = _limits(thresholds, seq)
        runs = collect_runs(variant_root, seq, data_root, align, skip_s)
        out["sequences"][seq] = {
            **{k: res.get(k) for k in PROVENANCE},
            "align": align, "skip_s": skip_s, "runs": len(runs),
            "per_run_ate_rmse": [r["ate_rmse"] for r in runs],
            "per_run_rpe_rmse": [r["rpe_rmse"] for r in runs],
            "per_run_ok": [r["ok"] for r in runs],
        }
    return out


# ---------------------------------------------------------------- plot
# Reference categorical palette, fixed order (dataviz skill; validated: all checks pass,
# contrast WARN relieved by the x-axis variant labels and the table in docs/variance.md).
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
INK, MUTED, GRID, SURFACE = "#1f1f1e", "#6b6b68", "#e6e6e3", "#fcfcfb"


def plot_spread(table: dict, variants: list[str], seqs: list[str], path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, len(seqs), figsize=(4.2 * len(seqs), 4.2), facecolor=SURFACE)
    axes = np.atleast_1d(axes)
    rng = np.random.default_rng(0)
    for ax, seq in zip(axes, seqs):
        ax.set_facecolor(SURFACE)
        cols = [v for v in variants if seq in table.get(v, {})]
        for i, v in enumerate(cols):
            runs = table[v][seq]["runs_detail"]
            y = np.array([r["ate_rmse"] for r in runs if r["ok"]]) * 1e3
            x = i + rng.uniform(-0.12, 0.12, len(y))
            ax.scatter(x, y, s=34, color=SERIES[variants.index(v)], edgecolor=SURFACE,
                       linewidth=1.5, zorder=3)
            if len(y):
                ax.hlines(np.median(y), i - 0.28, i + 0.28, color=INK, linewidth=2, zorder=4)
            s = table[v][seq]
            note = f"{s['distinct_trajectories']} distinct"
            if s["failed"]:
                note += f"\n{s['failed']} failed"
            ax.annotate(note, (i, 1.0), xycoords=("data", "axes fraction"), ha="center",
                        va="bottom", fontsize=8, color=MUTED)
        ax.set_xticks(range(len(cols)), [c.replace("_", "\n", 1) for c in cols], fontsize=8,
                      color=INK)
        ax.set_xlim(-0.6, len(cols) - 0.4)
        ax.set_title(seq, fontsize=10, color=INK, pad=22)
        ax.grid(axis="y", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
        for sp in ("left", "bottom"):
            ax.spines[sp].set_color(GRID)
        ax.tick_params(colors=MUTED, labelsize=8)
        ax.yaxis.set_major_formatter(matplotlib.ticker.FormatStrFormatter("%.1f"))
    axes[0].set_ylabel("ATE RMSE per run [mm]  (bar = median)", fontsize=9, color=INK)
    fig.suptitle("OpenVINS v2.7 run-to-run spread, 10 runs per variant", fontsize=11,
                 color=INK, x=0.01, ha="left")
    fig.tight_layout()
    fig.savefig(path, dpi=110, facecolor=SURFACE)
    plt.close(fig)


def main(argv=None) -> int:
    import yaml

    ap = argparse.ArgumentParser(description="Summarise S3 variance runs.")
    ap.add_argument("--root", type=Path, default=Path("results/variance"))
    ap.add_argument("--seqs", nargs="+", required=True)
    ap.add_argument("--variants", nargs="+",
                    default=["A_serial", "B_serial_cpus2", "C_serial_det", "D_realtime"])
    ap.add_argument("--baseline-variant", default="A_serial")
    ap.add_argument("--data", type=Path, default=Path("data/euroc"))
    ap.add_argument("--thresholds", type=Path, default=score.ROOT / "configs" / "thresholds.yaml")
    ap.add_argument("--out-dir", type=Path, default=Path("docs/variance"))
    a = ap.parse_args(argv)

    thr = yaml.safe_load(a.thresholds.read_text())
    a.out_dir.mkdir(parents=True, exist_ok=True)
    table: dict = {}
    for v in a.variants:
        for seq in a.seqs:
            if not (a.root / v / seq).is_dir():
                continue
            align, skip_s = _limits(thr, seq)
            runs = collect_runs(a.root / v, seq, a.data, align, skip_s)
            res_f = a.root / v / f"{seq}.json"
            res = json.loads(res_f.read_text()) if res_f.exists() else {}
            table.setdefault(v, {})[seq] = {**summarise(runs), "runs_detail": runs,
                                            **{k: res.get(k) for k in PROVENANCE}}
    baseline = build_baseline(a.root / a.baseline_variant, a.seqs, a.data, thr)
    (a.out_dir / "baseline.json").write_text(json.dumps(baseline, indent=2, allow_nan=False) + "\n")
    (a.out_dir / "variance.json").write_text(json.dumps(table, indent=2, allow_nan=False) + "\n")
    plot_spread(table, a.variants, a.seqs, a.out_dir / "spread.png")
    for v, per in table.items():
        for seq, s in per.items():
            r = s["ate_rmse"]
            fmt = lambda x: "  n/a " if x is None else f"{x * 1e3:7.3f}"  # noqa: E731
            print(f"{v:15s} {seq:13s} runs={s['runs']:2d} failed={s['failed']} "
                  f"distinct={s['distinct_trajectories']:2d} ATE[mm] median={fmt(r['median'])} "
                  f"std={fmt(r['std'])} range={fmt(r['range'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
