"""The VIO image must be pinned to an exact upstream commit.

A moving branch makes every "regression" ambiguous (our harness, or their commit?),
so this fails closed on anything other than a full 40-hex SHA.
"""
import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "docker" / "Dockerfile.openvins"


def test_ov_ref_is_full_commit_sha():
    m = re.search(r"^ARG OV_REF=(\S*)$", DOCKERFILE.read_text(), re.M)
    assert m, "Dockerfile.openvins must declare ARG OV_REF=<sha>"
    assert re.fullmatch(r"[0-9a-f]{40}", m.group(1)), f"OV_REF is not a full SHA: {m.group(1)!r}"


def test_build_verifies_checked_out_commit():
    text = DOCKERFILE.read_text()
    assert "rev-parse HEAD" in text, "build must assert HEAD == OV_REF after checkout"
