"""Image identity (S5b): the pinned ref in docker/IMAGE_REF and its REGISTRY digest.

The recorded image_digest is the registry (manifest) digest: the same on every host,
every pull and every image store. A local build's image ID is not used: it changes on
every rebuild and differs between image stores (containerd vs overlay2), so it cannot key
a baseline. Anything that cannot be confirmed fails closed.
"""
import json
import re
from pathlib import Path

import pytest

from vioreg import image_ref as ir

ROOT = Path(__file__).resolve().parents[1]
DG = "sha256:" + "c" * 64
REPO = "ghcr.io/owner/vioreg-openvins"


# ---------------------------------------------------------------- docker/IMAGE_REF
def test_image_ref_file_is_one_line_repo_at_sha256():
    text = (ROOT / "docker" / "IMAGE_REF").read_text()
    lines = text.splitlines()
    assert len(lines) == 1 and text.endswith("\n"), "exactly one line"
    assert re.fullmatch(r"ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}", lines[0]), lines[0]


def test_pinned_ref_defaults_to_the_file_and_env_overrides():
    file_ref = (ROOT / "docker" / "IMAGE_REF").read_text().strip()
    assert ir.pinned_ref(env={}) == file_ref
    assert ir.pinned_ref(env={"IMAGE": ""}) == file_ref           # empty = unset
    assert ir.pinned_ref(env={"IMAGE": f"{REPO}@{DG}"}) == f"{REPO}@{DG}"


@pytest.mark.parametrize("bad", ["vioreg-openvins:v2.7", f"{REPO}:d492d65",
                                 f"{REPO}@sha256:abc", f"{REPO}@{DG.upper()}", ""])
def test_pinned_ref_rejects_anything_not_digest_pinned(bad, tmp_path):
    with pytest.raises(ir.ImageRefError):
        ir.pinned_ref(env={"IMAGE": bad} if bad else {}, path=tmp_path / "missing")
    if bad:
        f = tmp_path / "IMAGE_REF"
        f.write_text(bad + "\n")
        with pytest.raises(ir.ImageRefError):
            ir.pinned_ref(env={}, path=f)


def test_pinned_ref_file_must_be_exactly_one_line(tmp_path):
    f = tmp_path / "IMAGE_REF"
    f.write_text(f"{REPO}@{DG}\n{REPO}@{DG}\n")
    with pytest.raises(ir.ImageRefError):
        ir.pinned_ref(env={}, path=f)


# ---------------------------------------------------------------- registry digest
@pytest.mark.parametrize("ref, repo_digests, want", [
    (f"{REPO}@{DG}", [f"{REPO}@{DG}"], DG),
    (f"{REPO}:d492d65@{DG}", [f"{REPO}@{DG}"], DG),           # tag+digest: digest wins
    (f"{REPO}@{DG}", [f"other/repo@{DG}", f"{REPO}@{DG}"], DG),
    (f"{REPO}:d492d65", [f"{REPO}@{DG}"], None),              # tag only: can move
    ("vioreg-openvins:v2.7", [], None),                       # local build
    (f"{REPO}@{DG}", [], None),                               # not confirmed by docker
    (f"{REPO}@{DG}", None, None),
    (f"{REPO}@{DG}", [f"other/repo@{DG}"], None),             # digest of another repo
    (f"{REPO}@{DG}", [f"{REPO}@sha256:{'d' * 64}"], None),    # different digest
    (f"{REPO}@sha256:abc", [f"{REPO}@sha256:abc"], None),     # not 64 hex
])
def test_registry_digest(ref, repo_digests, want):
    assert ir.registry_digest(ref, repo_digests) == want


def inspect_returning(info):
    return lambda ref: info


def test_verify_image_returns_the_registry_digest_not_the_image_id():
    info = {"Id": "sha256:" + "e" * 64, "RepoDigests": [f"{REPO}@{DG}"]}
    assert ir.verify_image(f"{REPO}@{DG}", inspect=inspect_returning(info)) == DG


@pytest.mark.parametrize("info", [
    {"Id": DG, "RepoDigests": []},                             # e.g. a local build tagged so
    {"Id": DG, "RepoDigests": [f"{REPO}@sha256:{'d' * 64}"]},  # pulled digest != pinned
])
def test_verify_image_fails_when_pulled_digest_is_not_the_pinned_one(info):
    with pytest.raises(ir.ImageRefError, match="pinned"):
        ir.verify_image(f"{REPO}@{DG}", inspect=inspect_returning(info))


def test_verify_image_fails_when_image_is_absent():
    def missing(ref):
        raise ir.ImageRefError(f"no such image {ref}")
    with pytest.raises(ir.ImageRefError):
        ir.verify_image(f"{REPO}@{DG}", inspect=missing)


def test_verify_image_rejects_a_tag_ref_before_inspecting():
    called = []
    with pytest.raises(ir.ImageRefError):
        ir.verify_image(f"{REPO}:latest", inspect=lambda r: called.append(r) or {})
    assert not called


def test_docker_inspect_parses_cli_output(monkeypatch):
    info = {"Id": DG, "RepoDigests": [f"{REPO}@{DG}"]}
    monkeypatch.setattr(ir, "_run", lambda cmd: json.dumps([info]).encode())
    assert ir.docker_inspect(f"{REPO}@{DG}") == info
