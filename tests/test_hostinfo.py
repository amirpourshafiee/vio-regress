"""Host fingerprint for cross-machine comparison (sidecar, never in the result JSON)."""
import json

from vioreg import hostinfo

CPUINFO = """processor	: 0
vendor_id	: AuthenticAMD
model name	: AMD EPYC 7763 64-Core Processor
flags		: fpu sse sse2 ssse3 fma sse4_1 sse4_2 avx avx2 avx512f avx512dq avx512_vnni bmi2 sha_ni
processor	: 1
vendor_id	: AuthenticAMD
model name	: AMD EPYC 7763 64-Core Processor
flags		: fpu sse sse2 ssse3 fma sse4_1 sse4_2 avx avx2 avx512f avx512dq avx512_vnni bmi2 sha_ni
"""


def test_parse_cpuinfo_model_and_logical_cpus():
    h = hostinfo.parse_cpuinfo(CPUINFO)
    assert h["cpu_model"] == "AMD EPYC 7763 64-Core Processor"
    assert h["vendor"] == "AuthenticAMD"
    assert h["logical_cpus"] == 2


def test_simd_flags_keep_only_eigen_opencv_relevant():
    h = hostinfo.parse_cpuinfo(CPUINFO)
    assert h["simd_flags"] == ["avx", "avx2", "avx512_vnni", "avx512dq", "avx512f", "fma",
                               "sse4_1", "sse4_2"]


def test_flags_differing_between_cores_is_reported():
    odd = CPUINFO.replace("avx512_vnni bmi2 sha_ni\nprocessor\t: 1", "bmi2 sha_ni\nprocessor\t: 1", 1)
    h = hostinfo.parse_cpuinfo(odd)
    assert h["simd_flags_uniform"] is False


def test_empty_cpuinfo_fails_closed():
    import pytest
    with pytest.raises(ValueError):
        hostinfo.parse_cpuinfo("")


def test_fingerprint_is_json_and_has_no_result_keys(tmp_path):
    f = tmp_path / "cpuinfo"
    f.write_text(CPUINFO)
    h = hostinfo.fingerprint(cpuinfo_path=f)
    json.dumps(h)
    assert {"cpu_model", "logical_cpus", "simd_flags", "kernel", "machine"} <= set(h)
    assert not {"ate", "rpe", "vio_commit"} & set(h)
