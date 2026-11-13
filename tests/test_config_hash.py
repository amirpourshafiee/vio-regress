"""config_hash = sha1 over estimator_config.yaml + the kalibr yamls it references,
concatenated in sorted file-name order. Fails closed on anything missing."""
import hashlib

import pytest

from vioreg import config_hash as ch

EST = b"""%YAML:1.0
verbosity: "INFO"
relative_config_imu: "kalibr_imu_chain.yaml"
relative_config_imucam: "kalibr_imucam_chain.yaml"
"""
IMU = b"imu0:\n  accelerometer_noise_density: 2.0e-03\n"
CAM = b"cam0:\n  intrinsics: [458.654, 457.296, 367.215, 248.375]\n"


def files(**over):
    d = {"estimator_config.yaml": EST, "kalibr_imu_chain.yaml": IMU,
         "kalibr_imucam_chain.yaml": CAM}
    d.update(over)
    return {k: v for k, v in d.items() if v is not None}


def reader(d):
    def read(name):
        if name not in d:
            raise FileNotFoundError(name)
        return d[name]
    return read


def test_refs_parsed_from_estimator_config():
    assert ch.config_refs(EST) == ["kalibr_imu_chain.yaml", "kalibr_imucam_chain.yaml"]


def test_refs_tolerate_unquoted_values_and_comments():
    est = b'relative_config_imu: kalibr_imu_chain.yaml # imu\nrelative_config_imucam: "c.yaml"\n'
    assert ch.config_refs(est) == ["kalibr_imu_chain.yaml", "c.yaml"]


def test_hash_is_sha1_of_contents_in_sorted_name_order():
    # sorted: estimator_config.yaml < kalibr_imu_chain.yaml < kalibr_imucam_chain.yaml
    expected = hashlib.sha1(EST + IMU + CAM).hexdigest()
    assert ch.config_hash(reader(files())) == expected


def test_hash_changes_when_any_referenced_file_changes():
    base = ch.config_hash(reader(files()))
    assert ch.config_hash(reader(files(**{"kalibr_imu_chain.yaml": IMU + b"#x\n"}))) != base
    assert ch.config_hash(reader(files(**{"kalibr_imucam_chain.yaml": CAM + b"#x\n"}))) != base
    assert ch.config_hash(reader(files(**{"estimator_config.yaml": EST + b"#x\n"}))) != base


def test_unreferenced_files_do_not_affect_hash():
    d = files()
    d["kalibr_imu_chain_OLD.yaml"] = b"stale"
    assert ch.config_hash(reader(d)) == ch.config_hash(reader(files()))


def test_missing_referenced_file_fails_closed():
    with pytest.raises(ch.ConfigHashError, match="kalibr_imucam_chain.yaml"):
        ch.config_hash(reader(files(**{"kalibr_imucam_chain.yaml": None})))


@pytest.mark.parametrize("key", [b"relative_config_imu", b"relative_config_imucam"])
def test_missing_reference_key_fails_closed(key):
    est = b"\n".join(l for l in EST.splitlines() if not l.startswith(key + b":"))
    with pytest.raises(ch.ConfigHashError, match=key.decode()):
        ch.config_hash(reader(files(**{"estimator_config.yaml": est})))


@pytest.mark.parametrize("ref", [b"/etc/passwd", b"../other/kalibr.yaml"])
def test_reference_must_stay_inside_config_dir(ref):
    est = EST.replace(b'"kalibr_imu_chain.yaml"', b'"' + ref + b'"')
    with pytest.raises(ch.ConfigHashError, match="outside"):
        ch.config_hash(reader(files(**{"estimator_config.yaml": est})))


def test_empty_file_fails_closed():
    with pytest.raises(ch.ConfigHashError, match="empty"):
        ch.config_hash(reader(files(**{"kalibr_imu_chain.yaml": b""})))


def test_dir_reader(tmp_path):
    for k, v in files().items():
        (tmp_path / k).write_bytes(v)
    assert ch.config_hash(ch.dir_reader(tmp_path)) == hashlib.sha1(EST + IMU + CAM).hexdigest()
