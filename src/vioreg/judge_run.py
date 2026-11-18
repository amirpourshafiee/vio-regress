"""Run the VLM judge over the labeled windows (S10).

Rules enforced here, each with a test:
- the judge sees the contact sheet only: the same thing the human labeler saw. No sequence name,
  times or error values, so the agreement score compares like with like;
- this code never opens human_v1.csv or windows.csv (labels, sources, errors);
- dev split only. The test split needs --test-once, writes a new file and runs once;
- a hard call cap (--max-calls, no default). Retries count against it. It stops before exceeding it
  and keeps the partial output;
- responses are cached by sha256(image) + prompt + model + repeat index, so a rerun is free.

    python -m vioreg.judge_run --max-calls 300 --dry-run
    python -m vioreg.judge_run --max-calls 300
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import judge

COLUMNS = ["window_id", "split", "run", "label", "confidence", "evidence", "model",
           "prompt_version", "cached", "ok"]
DEFAULT_MODEL = "claude-sonnet-5-5"


class CapExceeded(RuntimeError):
    """Raised before a call that would exceed the hard cap."""


class CountingClient:
    """Wraps an Anthropic-style client; every .messages.create counts, retries included."""

    def __init__(self, client, max_calls: int):
        self._client, self.max_calls, self.calls = client, max_calls, 0
        self.input_tokens = self.output_tokens = 0
        self.messages = self

    def create(self, **kw):
        if self.calls >= self.max_calls:
            raise CapExceeded(f"hard cap of {self.max_calls} calls reached")
        self.calls += 1
        resp = self._client.messages.create(**kw)
        u = getattr(resp, "usage", None)
        self.input_tokens += int(getattr(u, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(u, "output_tokens", 0) or 0)
        return resp


def cache_key(png: bytes, prompt: str, model: str, repeat: int) -> str:
    h = hashlib.sha256()
    h.update(hashlib.sha256(png).digest())
    for part in (prompt, model, str(repeat)):
        h.update(b"\0" + part.encode())
    return h.hexdigest()


def _rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def load_windows(labels_dir: Path, split: str):
    """[(window_id, sheet_path)] for one split. Reads windows_ui.csv and split.csv only."""
    labels_dir = Path(labels_dir)
    ui = {r["window_id"]: r["sheet"] for r in _rows(labels_dir / "windows_ui.csv")}
    sp = {r["window_id"]: r["split"] for r in _rows(labels_dir / "split.csv")}
    if set(sp) != set(ui):
        raise ValueError("split.csv does not cover windows_ui.csv exactly: "
                         f"{len(set(sp) ^ set(ui))} ids differ")
    out = []
    for wid in sorted(ui):
        if sp[wid] != split:
            continue
        p = labels_dir / ui[wid]
        if not p.exists():
            raise FileNotFoundError(f"sheet for window {wid} not found: {p}")
        out.append((wid, p))
    return out


def plan_run(labels_dir, split, model, repeats, cache_dir):
    """Counts only, no API. calls_needed = uncached (window, repeat) pairs."""
    wins = load_windows(Path(labels_dir), split)
    cache_dir = Path(cache_dir)
    need = 0
    for _, p in wins:
        png = p.read_bytes()
        for r in range(repeats):
            if not (cache_dir / f"{cache_key(png, judge.PROMPT_V1, model, r)}.json").exists():
                need += 1
    return {"windows": len(wins), "repeats": repeats,
            "calls_total": len(wins) * repeats, "calls_needed": need}


def run_judge(labels_dir, split, client, model, repeats, max_calls, cache_dir, out_path,
              date=None, test_once=False, overwrite=False):
    if split not in ("dev", "test"):
        raise ValueError(f"split must be dev or test, got {split!r}")
    out_path = Path(out_path)
    if split == "test":
        if not test_once:
            raise PermissionError("the test split runs once, only with --test-once, "
                                  "after the prompt and model are frozen")
        if out_path.exists():
            raise FileExistsError(f"{out_path} exists; the test split is run once")
    elif out_path.exists() and not overwrite:
        raise FileExistsError(f"{out_path} exists (use --overwrite or another --out)")

    wins = load_windows(Path(labels_dir), split)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    counting = CountingClient(client, max_calls)
    rows, cached_n, capped = [], 0, False
    for rep in range(repeats):           # repeat outermost: a capped run still covers every window
        for wid, p in wins:
            png = p.read_bytes()
            f = cache_dir / f"{cache_key(png, judge.PROMPT_V1, model, rep)}.json"
            if f.exists():
                v, cached = json.loads(f.read_text()), True
                v["ok"] = True
            else:
                try:
                    v = judge.judge_sheet(counting, model, png)
                except CapExceeded:
                    capped = True
                    break
                cached = False
                if v["ok"]:
                    f.write_text(json.dumps({k: v[k] for k in ("label", "confidence", "evidence")}))
            cached_n += cached
            rows.append({"window_id": wid, "split": split, "run": rep, "label": v["label"],
                         "confidence": v["confidence"], "evidence": v["evidence"],
                         "model": model, "prompt_version": judge.PROMPT_VERSION,
                         "cached": cached, "ok": v["ok"]})
        if capped:
            break
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, COLUMNS, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    return {"rows": len(rows), "calls": counting.calls, "cached": cached_n, "capped": capped,
            "input_tokens": counting.input_tokens, "output_tokens": counting.output_tokens,
            "out": str(out_path)}


def build_client():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY is not set in this shell. "
                         "Run: read -s ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY")
    import anthropic
    return anthropic.Anthropic(api_key=key)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--labels-dir", default="labels")
    ap.add_argument("--split", default="dev", choices=["dev", "test"])
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--max-calls", type=int, required=True,
                    help="hard cap on API calls, retries included (no default on purpose)")
    ap.add_argument("--cache-dir", default=None, help="default: <labels-dir>/judge_cache")
    ap.add_argument("--out", default=None, help="default: <labels-dir>/vlm_<model>_<date>_<split>.csv")
    ap.add_argument("--date", default=None)
    ap.add_argument("--test-once", action="store_true",
                    help="allow the test split; run it once with the frozen prompt and model")
    ap.add_argument("--overwrite", action="store_true", help="dev output only")
    ap.add_argument("--dry-run", action="store_true", help="count calls, no API, no key needed")
    a = ap.parse_args(argv)
    labels_dir = Path(a.labels_dir)
    date = a.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cache_dir = Path(a.cache_dir) if a.cache_dir else labels_dir / "judge_cache"
    out = Path(a.out) if a.out else labels_dir / f"vlm_{a.model}_{date}_{a.split}.csv"
    if a.dry_run:
        print(json.dumps(plan_run(labels_dir, a.split, a.model, a.repeats, cache_dir)))
        return 0
    try:
        s = run_judge(labels_dir, a.split, build_client(), a.model, a.repeats, a.max_calls,
                      cache_dir, out, date=date, test_once=a.test_once, overwrite=a.overwrite)
    except (PermissionError, FileExistsError, FileNotFoundError, ValueError) as e:
        print(f"error: {e}")
        return 1
    print(json.dumps(s))
    if s["capped"]:
        print("STOPPED at the call cap; partial output kept. Raise --max-calls to continue "
              "(cached responses are free).")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
