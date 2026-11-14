"""Host fingerprint: which CPU produced a set of results.

    python -m vioreg.hostinfo --out docs/sensitivity/host_codespace.json

Written as a SIDECAR next to results, never into the frozen result JSON. It exists so
results from different machines (this Codespace vs the GitHub-hosted runner, S5) can be
compared knowing whether the SIMD code paths Eigen/OpenCV pick at build or run time could
differ. Note: the OpenVINS image is built with -O3 and the compiler's default -march, so the
build itself does not target AVX; OpenCV and Eigen may still dispatch at run time.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
from pathlib import Path

SIMD_RE = re.compile(r"^(avx|avx2|avx512.*|fma|sse4.*)$")


def parse_cpuinfo(text: str) -> dict:
    blocks = [b for b in re.split(r"\n(?=processor\s*:)", text.strip()) if b.strip()]
    if not blocks or "processor" not in blocks[0]:
        raise ValueError("no processor entries in cpuinfo")

    def field(block, name):
        m = re.search(rf"^{name}\s*:\s*(.*)$", block, re.M)
        return m.group(1).strip() if m else None

    flag_sets = [frozenset((field(b, "flags") or "").split()) for b in blocks]
    simd = sorted(f for f in flag_sets[0] if SIMD_RE.match(f))
    return {
        "cpu_model": field(blocks[0], "model name"),
        "vendor": field(blocks[0], "vendor_id"),
        "logical_cpus": len(blocks),
        "simd_flags": simd,
        "simd_flags_uniform": all({f for f in fs if SIMD_RE.match(f)} == set(simd)
                                  for fs in flag_sets),
    }


def fingerprint(cpuinfo_path: str | Path = "/proc/cpuinfo") -> dict:
    h = parse_cpuinfo(Path(cpuinfo_path).read_text())
    u = platform.uname()
    h.update({
        "usable_cpus": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "kernel": u.release,
        "machine": u.machine,
        "hostname_hint": "github-actions" if os.environ.get("GITHUB_ACTIONS") == "true"
        else ("codespace" if os.environ.get("CODESPACES") == "true" else "other"),
    })
    return h


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Write a CPU/SIMD fingerprint sidecar.")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(fingerprint(), indent=2) + "\n")
    print(a.out.read_text(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
