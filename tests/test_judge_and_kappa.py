import json
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from vioreg.judge import LABELS, judge_window, make_contact_sheet, parse_verdict
from vioreg.kappa import agreement_report, kappa


class FakeClient:
    """Mimics client.messages.create. Replies are consumed in order."""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls += 1
        assert kwargs["messages"][0]["content"][0]["type"] == "image"
        return SimpleNamespace(content=[SimpleNamespace(text=self.replies.pop(0))])


GOOD = json.dumps({"label": "motion_blur", "confidence": 0.8, "evidence": "smeared"})


def test_parse_accepts_json_wrapped_in_prose():
    assert parse_verdict(f"Sure! {GOOD} Hope that helps")["label"] == "motion_blur"


@pytest.mark.parametrize("bad", ["no json here", '{"label": "aliens", "confidence": 1}',
                                 '{"label": "low_texture", "confidence": 7}'])
def test_parse_rejects_invalid(bad):
    with pytest.raises(ValueError):
        parse_verdict(bad)


def test_judge_retries_then_succeeds():
    c = FakeClient(["garbage", GOOD])
    out = judge_window(c, "m", b"png", "MH_03_medium", 10.0, 15.0, 0.4)
    assert out["label"] == "motion_blur" and c.calls == 2


def test_judge_falls_back_to_unclear_not_a_guess():
    c = FakeClient(["garbage"] * 3)
    out = judge_window(c, "m", b"png", "MH_03_medium", 10.0, 15.0, 0.4)
    assert out["label"] == "unclear" and out["confidence"] == 0.0


def test_contact_sheet_dimensions(tmp_path):
    paths = []
    for k in range(6):
        p = tmp_path / f"{k}.png"
        Image.fromarray(np.full((480, 752), 40 * k, np.uint8)).save(p)
        paths.append(p)
    import io
    sheet = Image.open(io.BytesIO(make_contact_sheet(paths, cols=4, tile=(100, 60))))
    assert sheet.size == (400, 120)


def test_kappa_hand_computed():
    # po = 3/4, pe = .5*.25 + .5*.75 = .5, kappa = (.75 - .5) / .5 = .5
    assert kappa([0, 0, 1, 1], [0, 1, 1, 1], [0, 1]) == pytest.approx(0.5)


def test_kappa_perfect_and_chance():
    rng = np.random.default_rng(0)
    a = rng.integers(0, 3, 600)
    assert kappa(a, a, [0, 1, 2]) == pytest.approx(1.0)
    assert abs(kappa(a, rng.integers(0, 3, 600), [0, 1, 2])) < 0.1


def test_kappa_punishes_majority_class_guessing():
    human = ["no_visual_cause"] * 90 + ["motion_blur"] * 10
    vlm = ["no_visual_cause"] * 100  # 90 percent accurate, zero skill
    rep = agreement_report(human, vlm, LABELS)
    assert rep["accuracy"] == pytest.approx(0.9)
    assert rep["kappa"] == pytest.approx(0.0, abs=1e-9)


def test_group_bootstrap_is_wider_for_clustered_agreement():
    from vioreg.kappa import bootstrap_ci
    rng = np.random.default_rng(5)
    a, b, g = [], [], []
    for grp, agree in enumerate(np.linspace(0.15, 1.0, 11)):  # per-sequence agreement
        h = rng.integers(0, 3, 30)
        v = np.where(rng.random(30) < agree, h, rng.integers(0, 3, 30))
        a += list(h); b += list(v); g += [f"seq{grp}"] * 30
    lo_i, hi_i = bootstrap_ci(a, b, [0, 1, 2], n=800)
    lo_g, hi_g = bootstrap_ci(a, b, [0, 1, 2], n=800, groups=g)
    assert (hi_g - lo_g) > (hi_i - lo_i)


def test_report_has_prevalence_adjusted_kappa_and_per_class():
    human = ["motion_blur"] * 5 + ["low_texture"] * 5
    vlm = ["motion_blur"] * 4 + ["low_texture"] * 6
    rep = agreement_report(human, vlm, LABELS)
    assert rep["prevalence_adjusted_kappa"] == pytest.approx((6 * 0.9 - 1) / 5)
    assert rep["per_class"]["motion_blur"]["recall"] == pytest.approx(0.8)
    assert rep["per_class"]["low_texture"]["precision"] == pytest.approx(5 / 6)
