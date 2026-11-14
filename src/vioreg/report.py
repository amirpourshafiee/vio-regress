"""Markdown job summary for the nightly VIO job (session S5a).

    python -m vioreg.report results --seqs MH_01_easy --host results/host_ci.json \
        >> "$GITHUB_STEP_SUMMARY"

Informational only: the VERDICT is the gate (pytest -m sequence). This report makes three
things impossible to confuse:
  * NEEDS RE-BASELINE  the result's (vio_commit, image_digest, config_hash) has no baseline
                       entry. Not a quality statement; the comparison below is then made
                       against the closest baseline entry and labelled informational.
  * QUALITY FAIL       a hard check, the RPE ratio or the ATE aggregate failed.
  * MISSING            no usable result.
  * DATA FETCH FAILED  the EuRoC data could not be fetched (HTTP 429/5xx or a dropped
                       connection after retries, or a fetch error): "data fetch failed, not
                       a quality result". Still a gate failure (fail closed).
and reports separately, per sequence:
  (a) pose reproduction: each run vs the baseline trajectory (pose_equivalent), and the runs
      vs each other (determinism on this host);
  (b) the RPE ratio vs the baseline entry, with the margin (single-sequence ATE ratio shown
      for information, never gated).
It never raises: anything unreadable is printed as such.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import gate

LABEL = {"pass": "PASS", "needs-rebaseline": "NEEDS RE-BASELINE", "quality-fail": "QUALITY FAIL",
         "missing": "MISSING", "invalid": "INVALID RESULT", "fetch-failed": "DATA FETCH FAILED"}


def _nearest_entry(cfg: dict, seq: str, result: dict):
    entries = (cfg.get("sequences", {}).get(seq) or {}).get("baselines") or []
    if not entries:
        return None
    return max(entries, key=lambda e: sum(e.get(k) == result.get(k) for k in gate.KEY_FIELDS))


def _host_section(host: Path | None, reference: Path | None) -> list[str]:
    def load(p):
        try:
            return json.loads(Path(p).read_text()) if p else None
        except (OSError, ValueError):
            return None
    h, r = load(host), load(reference)
    if h is None:
        return ["**Host:** host fingerprint unavailable", ""]
    out = ["### Host", "", "| | CI runner | Baseline host |", "|---|---|---|"]
    rr = r or {}
    out.append(f"| CPU | {h.get('cpu_model')} | {rr.get('cpu_model', 'n/a')} |")
    out.append(f"| logical CPUs | {h.get('logical_cpus')} | {rr.get('logical_cpus', 'n/a')} |")
    out.append(f"| SIMD | {' '.join(h.get('simd_flags', []))} | {' '.join(rr.get('simd_flags', []))} |")
    if r:
        a, b = set(h.get("simd_flags", [])), set(r.get("simd_flags", []))
        diff = []
        if a - b:
            diff.append(f"only on CI: {' '.join(sorted(a - b))}")
        if b - a:
            diff.append(f"only on baseline host: {' '.join(sorted(b - a))}")
        same_cpu = h.get("cpu_model") == r.get("cpu_model")
        out += ["", f"{'Same CPU model' if same_cpu else 'different CPU'}"
                    f"{'; SIMD flags identical' if not diff else '; ' + '; '.join(diff)}."]
    return out + [""]


def _sequence_section(results_dir: Path, seq: str, cfg: dict, root: Path, fails: list[str],
                      verdict: str) -> list[str]:
    from .score import find_runs
    from .variance import distinct_trajectories, pose_equivalent

    out = [f"### {seq}: **{LABEL[verdict]}**", ""]
    for f in fails:
        out.append(f"- {f}")
    r, err = gate.load_result(Path(results_dir) / f"{seq}.json")
    if r is None:
        return out + ([f"- {err}"] if err and not fails else []) + [""]
    entry = _nearest_entry(cfg, seq, r)
    if entry is None:
        return out + ["- no baseline entry for this sequence", ""]
    differs = [k for k in gate.KEY_FIELDS if r.get(k) != entry.get(k)]
    info = " (informational: key differs, so a difference here is expected, not a regression signal)" \
        if differs else ""
    if differs:
        out.append("- key: " + "; ".join(
            f"{k} differs (result {gate._short(r.get(k))}, baseline {gate._short(entry.get(k))})"
            for k in differs))
    else:
        out.append(f"- key: match {gate._key_str(seq, r)}")

    # (a) pose reproduction
    runs = find_runs(Path(results_dir) / seq)
    ref = entry.get("trajectory")
    if not runs:
        out.append("- (a) poses: no run*.tum next to the result")
    elif not ref or not (Path(root) / ref).is_file():
        out.append("- (a) poses: no baseline trajectory to compare with")
    else:
        same = []
        for _, p in runs:
            try:
                same.append(pose_equivalent(p, Path(root) / ref))
            except Exception:  # noqa: BLE001 - unreadable run: not reproduced
                same.append(False)
        state = "reproduced exactly" if all(same) else "differ from the baseline"
        out.append(f"- (a) poses: {state} ({sum(same)}/{len(same)} runs identical to "
                   f"{ref}){info}")
    if len(runs) > 1:
        try:
            n = distinct_trajectories([p for _, p in runs])
            out.append(f"  - runs agree with each other: "
                       f"{'yes' if n == 1 else 'NO'} ({n} distinct trajector{'y' if n == 1 else 'ies'})")
        except Exception as e:  # noqa: BLE001
            out.append(f"  - runs agree with each other: unknown ({e})")

    # (b) RPE ratio (+ informational ATE)
    rpe, ate = r.get("rpe") or {}, r.get("ate") or {}
    m = cfg.get("margins") or {}
    if gate._num(rpe.get("rmse")) and gate._num(entry.get("rpe_rmse")):
        ratio = rpe["rmse"] / entry["rpe_rmse"]
        out.append(f"- (b) RPE ratio {ratio:.3f} (margin {m.get('rpe_ratio_max', float('nan')):.2f}; "
                   f"{rpe['rmse'] * 1e3:.2f} mm vs {entry['rpe_rmse'] * 1e3:.2f} mm){info}")
    else:
        out.append("- (b) RPE ratio: no usable RPE")
    if gate._num(ate.get("rmse")) and gate._num(entry.get("ate_rmse")):
        out.append(f"- ATE ratio {ate['rmse'] / entry['ate_rmse']:.3f} on this sequence "
                   "(information only; ATE is gated as the geometric mean over all sequences)")
    return out + [""]


def render(results_dir: Path, cfg: dict, root: Path, seqs: list[str] | None = None,
           host: Path | None = None, reference_host: Path | None = None,
           injected_fault: str = "") -> str:
    out = ["## vioreg nightly", ""]
    if injected_fault:
        out += [f"> **FAULT INJECTED: `{injected_fault}`** (red-build demo). Each sequence "
                f"was replayed from a faulted copy of its bag (labels/fault_manifest.csv). A "
                f"QUALITY FAIL below is the expected outcome, not a regression.", ""]
    out += _host_section(host, reference_host)
    if seqs == []:
        return "\n".join(out)
    try:
        rep = gate.evaluate_results_dir(results_dir, cfg, require=True, root=root, scope=seqs)
    except Exception as e:  # noqa: BLE001 - the report must still print something
        return "\n".join(out + [f"gate evaluation failed: {e}"])
    counts = {v: list(rep["verdicts"].values()).count(v) for v in gate.VERDICTS}
    out += ["**Gate:** " + ", ".join(f"{LABEL[v]} {n}" for v, n in counts.items() if n), ""]
    if rep["aggregate_skipped"]:
        out += [f"ATE aggregate skipped: {rep['aggregate_skipped']}", ""]
    elif rep["aggregate"]:
        out += [f"ATE aggregate: {'; '.join(rep['aggregate'])}", ""]
    for seq, fails in rep["sequences"].items():
        out += _sequence_section(results_dir, seq, cfg, root, fails, rep["verdicts"][seq])
    for w in rep["warnings"]:
        out.append(f"> WARNING: {w}")
    return "\n".join(out)


def main(argv=None) -> int:
    import yaml

    from .score import ROOT
    ap = argparse.ArgumentParser(description="Markdown summary of a nightly VIO run.")
    ap.add_argument("results", type=Path)
    ap.add_argument("--seqs", nargs="*", default=None)
    ap.add_argument("--thresholds", type=Path, default=ROOT / "configs" / "thresholds.yaml")
    ap.add_argument("--root", type=Path, default=ROOT)
    ap.add_argument("--host", type=Path, default=None)
    ap.add_argument("--reference-host", type=Path,
                    default=ROOT / "docs" / "sensitivity" / "host_codespace.json")
    ap.add_argument("--injected-fault", default="",
                    help="the nightly `fault` input; prints a red-build-demo banner if set")
    a = ap.parse_args(argv)
    try:
        cfg = yaml.safe_load(a.thresholds.read_text())
    except (OSError, ValueError) as e:
        print(f"## vioreg nightly\n\nthresholds unreadable: {e}")
        return 0
    seqs = gate.parse_scope(" ".join(a.seqs)) if a.seqs else None
    print(render(a.results, cfg, a.root, seqs, a.host, a.reference_host, a.injected_fault))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
