#!/usr/bin/env python3
"""Build a golden bundle for the gate tests from one sweep variant's real results.

    python scripts/experiments/make_golden_bundle.py <variant> <out.json> [--root results/sensitivity]

The bundle holds that variant's real per-sequence score JSONs (frozen contract, unchanged
values) RE-KEYED to the baseline's (vio_commit, image_digest, config_hash) from
configs/thresholds.yaml. Why re-key: under gate v2 a different config_hash means "needs
re-baseline", so the ratio margins only ever judge same-key results (another host, harness
or environment). The sweep variants are the measured proxy for how far an innocent
(or a bad) perturbation moves the metrics; re-keying asks the gate exactly that question.
The variant's real config_hash is kept in the bundle's "source" block.
"""
import argparse
import json
from pathlib import Path

import yaml

ap = argparse.ArgumentParser()
ap.add_argument("variant")
ap.add_argument("out", type=Path)
ap.add_argument("--root", type=Path, default=Path("results/sensitivity"))
ap.add_argument("--thresholds", type=Path, default=Path("configs/thresholds.yaml"))
a = ap.parse_args()
cfg = yaml.safe_load(a.thresholds.read_text())
bundle = {"source": {"variant": a.variant, "root": str(a.root), "real_key": {}},
          "results": {}}
for seq, sq in sorted(cfg["sequences"].items()):
    r = json.loads((a.root / a.variant / f"{seq}.json").read_text())
    bundle["source"]["real_key"][seq] = {k: r[k] for k in ("vio_commit", "image_digest", "config_hash")}
    base = sq["baselines"][0]
    for k in ("vio_commit", "image_digest", "config_hash"):
        r[k] = base[k]
    bundle["results"][seq] = r
a.out.parent.mkdir(parents=True, exist_ok=True)
a.out.write_text(json.dumps(bundle, indent=1) + "\n")
print(f"wrote {a.out}")
