from vioreg.euroc_io import load_euroc_gt


def test_load_euroc_gt_reorders_quaternion_wxyz_to_xyzw(tmp_path):
    f = tmp_path / "data.csv"
    f.write_text("#hdr\n1403636579763555584,1,2,3,0.5,0.1,0.2,0.3\n")
    tr = load_euroc_gt(f)
    assert tr.q[0].tolist() == [0.1, 0.2, 0.3, 0.5]  # x y z w
    assert tr.p[0].tolist() == [1, 2, 3]
    assert abs(tr.t[0] - 1403636579.763555584) < 1e-6
