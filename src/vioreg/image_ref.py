"""Image identity (S5b): the pinned ref and its REGISTRY digest.

docker/IMAGE_REF holds exactly one line, <repo>@sha256:<64 hex>: the image published by
.github/workflows/publish-image.yml. The env var IMAGE overrides it (same form required).

The recorded image_digest is the registry (manifest) digest, read from the ref and
confirmed by `docker image inspect` RepoDigests (<repo>@<digest>). It names the same bytes
on every host, every pull and every image store. A local build's image ID is NOT used:
  * a rebuild is not reproducible (apt-get update), so every rebuild gets a new ID;
  * the ID depends on Docker's storage mode: the containerd image store reports the
    manifest(-list) digest, the classic overlay2 store reports the config digest, so even
    byte-identical images carry different IDs on different hosts.
Neither can key a baseline. Anything that cannot be confirmed raises ImageRefError, and
callers fail closed.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
IMAGE_REF_FILE = ROOT / "docker" / "IMAGE_REF"
# <repo>[:tag]@sha256:<64 lowercase hex>; docker/IMAGE_REF itself carries no tag.
DIGEST_REF_RE = re.compile(
    r"^(?P<repo>[a-z0-9][a-z0-9._/:-]*?)(?::[\w][\w.-]{0,127})?@(?P<digest>sha256:[0-9a-f]{64})$")


class ImageRefError(RuntimeError):
    pass


def parse(ref: str) -> tuple[str, str]:
    """(repo, digest) of a digest-pinned ref, else ImageRefError."""
    m = DIGEST_REF_RE.match(ref or "")
    if not m:
        raise ImageRefError(f"not a digest-pinned image ref (<repo>@sha256:<64 hex>): {ref!r}")
    return m.group("repo"), m.group("digest")


def pinned_ref(env=None, path: Path = IMAGE_REF_FILE) -> str:
    """$IMAGE if set and non-empty, else the single line of docker/IMAGE_REF."""
    env = os.environ if env is None else env
    ref = env.get("IMAGE") or ""
    if not ref:
        try:
            lines = Path(path).read_text().splitlines()
        except OSError as e:
            raise ImageRefError(f"cannot read {path}: {e}") from e
        if len(lines) != 1:
            raise ImageRefError(f"{path} must hold exactly one line, has {len(lines)}")
        ref = lines[0].strip()
    parse(ref)
    return ref


def registry_digest(ref: str, repo_digests) -> str | None:
    """sha256:... if `ref` pins a digest and docker lists <repo>@<digest>, else None."""
    try:
        repo, digest = parse(ref)
    except ImageRefError:
        return None
    return digest if f"{repo}@{digest}" in (repo_digests or []) else None


def _run(cmd: list[str]) -> bytes:
    return subprocess.run(cmd, check=True, capture_output=True, timeout=120).stdout


def docker_inspect(ref: str) -> dict:
    try:
        return json.loads(_run(["docker", "image", "inspect", ref]))[0]
    except Exception as e:  # noqa: BLE001 - absent image, no docker, bad JSON: all unknown
        raise ImageRefError(f"cannot inspect {ref} (pull it first: docker pull {ref}): {e}") from e


def verify_image(ref: str, inspect=docker_inspect) -> str:
    """Registry digest of the local image `ref`; raises unless it equals the pinned digest."""
    repo, digest = parse(ref)
    info = inspect(ref)
    got = registry_digest(ref, info.get("RepoDigests"))
    if got != digest:
        raise ImageRefError(
            f"local image {ref} is not the pinned one: RepoDigests {info.get('RepoDigests')} "
            f"do not include {repo}@{digest} (image ID {info.get('Id')})")
    return got
