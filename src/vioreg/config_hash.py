"""config_hash: what estimator configuration a run actually used.

    config_hash = sha1( concat( bytes of each file, in sorted file-name order ) )
    files       = estimator_config.yaml
                  + every file it references via relative_config_imu / relative_config_imucam
                    (the kalibr IMU and IMU-camera calibration yamls)

For euroc_mav the sorted order is estimator_config.yaml, kalibr_imu_chain.yaml,
kalibr_imucam_chain.yaml. File names are not hashed separately: the referenced names are
themselves bytes inside estimator_config.yaml, so the hash already commits to which files
were used. Files in the directory that are not referenced do not affect the hash.

Fails closed (ConfigHashError) on a missing reference key, a missing or empty file, or
a reference that points outside the config directory.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Callable

ESTIMATOR = "estimator_config.yaml"
REF_KEYS = ("relative_config_imu", "relative_config_imucam")
_REF_RE = re.compile(rb'^\s*(relative_config_\w+)\s*:\s*"?([^"#\s]+)"?', re.M)

Reader = Callable[[str], bytes]


class ConfigHashError(RuntimeError):
    pass


def config_refs(estimator_yaml: bytes) -> list[str]:
    """Referenced file names, in REF_KEYS order."""
    found = {m.group(1).decode(): m.group(2).decode() for m in _REF_RE.finditer(estimator_yaml)}
    missing = [k for k in REF_KEYS if k not in found]
    if missing:
        raise ConfigHashError(f"{ESTIMATOR} lacks {', '.join(missing)}")
    refs = [found[k] for k in REF_KEYS]
    for r in refs:
        p = PurePosixPath(r)
        if p.is_absolute() or ".." in p.parts:
            raise ConfigHashError(f"reference {r!r} points outside the config directory")
    return refs


def config_hash(read: Reader) -> str:
    est = _read(read, ESTIMATOR)
    names = sorted({ESTIMATOR, *config_refs(est)})
    h = hashlib.sha1()
    for name in names:
        h.update(est if name == ESTIMATOR else _read(read, name))
    return h.hexdigest()


def _read(read: Reader, name: str) -> bytes:
    try:
        data = read(name)
    except (OSError, subprocess.CalledProcessError) as e:
        raise ConfigHashError(f"cannot read {name}: {e}") from e
    if not data:
        raise ConfigHashError(f"{name} is empty")
    return data


def dir_reader(config_dir: str | Path) -> Reader:
    d = Path(config_dir)
    return lambda name: (d / name).read_bytes()


def image_reader(image: str, config_name: str, run=None) -> Reader:
    """Read files from the config directory baked into the Docker image."""
    base = f"/ws/src/open_vins/config/{config_name}"

    def default_run(cmd):
        return subprocess.run(cmd, check=True, capture_output=True, timeout=120).stdout

    run = run or default_run
    return lambda name: run(["docker", "run", "--rm", "--entrypoint", "cat", image,
                             f"{base}/{name}"])
