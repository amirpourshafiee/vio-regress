"""Vision-language-model triage of failed sequence windows."""
from __future__ import annotations

import base64
import io
import json
import re
from pathlib import Path

from PIL import Image

LABEL_DESC = {
    "motion_blur": "fast motion smears the image",
    "low_texture": "blank walls or floor, very little to track",
    "illumination": "too dark, overexposed, or flickering",
    "handling_event": "drone picked up, set down, or carried by a person",
    "no_visual_cause": "frames look fine; suspect estimator, config or timing",
    "unclear": "frames are ambiguous or mixed causes",
}
LABELS = list(LABEL_DESC)

PROMPT = """You are triaging a visual-inertial odometry failure.
The image is a contact sheet of consecutive frames from the LEFT camera, in time
order, left to right and top to bottom, covering a window where position error grew.
Context: sequence={sequence}, window={t0:.1f}s to {t1:.1f}s, peak error={peak:.2f} m.

Pick exactly ONE label for the most likely visual cause:
{label_help}

Rules: judge only from what is visible in the frames. Do not guess from the
sequence name. Reply with JSON only, no prose:
{{"label": "<label>", "confidence": <0..1>, "evidence": "<max 2 sentences>"}}"""

LABEL_HELP = "\n".join(f"- {k}: {v}" for k, v in LABEL_DESC.items())


def make_contact_sheet(paths: list[Path], cols: int = 4,
                       tile: tuple[int, int] = (376, 240)) -> bytes:
    """Tile frames into one PNG. One image per call keeps cost and latency down."""
    rows = (len(paths) + cols - 1) // cols
    sheet = Image.new("L", (cols * tile[0], rows * tile[1]))
    for k, p in enumerate(paths):
        im = Image.open(p).convert("L").resize(tile)
        sheet.paste(im, ((k % cols) * tile[0], (k // cols) * tile[1]))
    buf = io.BytesIO()
    sheet.save(buf, format="PNG")
    return buf.getvalue()


def parse_verdict(text: str) -> dict:
    """Extract and validate the JSON verdict. Raises ValueError on anything odd."""
    m = re.search(r"\{.*\}", text, flags=re.S)
    if not m:
        raise ValueError(f"no JSON object in reply: {text[:80]!r}")
    obj = json.loads(m.group(0))
    if obj.get("label") not in LABELS:
        raise ValueError(f"label not in taxonomy: {obj.get('label')!r}")
    conf = float(obj.get("confidence", 0.0))
    if not 0.0 <= conf <= 1.0:
        raise ValueError(f"confidence out of range: {conf}")
    return {"label": obj["label"], "confidence": conf,
            "evidence": str(obj.get("evidence", ""))[:400]}


def judge_window(client, model: str, png: bytes, sequence: str,
                 t0: float, t1: float, peak: float, max_retries: int = 2) -> dict:
    """`client` is anything with .messages.create (the Anthropic SDK client, or a fake)."""
    prompt = PROMPT.format(sequence=sequence, t0=t0, t1=t1, peak=peak,
                           label_help=LABEL_HELP)
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.b64encode(png).decode()}},
        {"type": "text", "text": prompt},
    ]
    last: Exception | None = None
    for _ in range(max_retries + 1):
        resp = client.messages.create(model=model, max_tokens=300,
                                      messages=[{"role": "user", "content": content}])
        try:
            return parse_verdict(resp.content[0].text)
        except (ValueError, json.JSONDecodeError) as exc:
            last = exc
    return {"label": "unclear", "confidence": 0.0, "evidence": f"parse failure: {last}"}
