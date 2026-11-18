# Codebook for the window labels (S8)

A label means **the most plausible visual cause from the frames**, not the root cause. You
cannot see inside the estimator: a window with real blur can still be a time-offset bug.

You see one contact sheet: 12 left-camera frames in time order (left to right, top to bottom),
about 5 seconds. You do not see the sequence, the error or whether a fault was injected.

| key | label | meaning | visible evidence |
|---|---|---|---|
| 1 | motion_blur | Fast motion smears the image | streaked edges across several consecutive frames |
| 2 | low_texture | Little to track | blank wall or floor fills most of the frame |
| 3 | illumination | Too dark, overexposed, flicker | washed out or crushed regions; brightness changes between frames |
| 4 | handling_event | Pick-up, set-down, carried | sudden view changes at sequence start or end (Machine Hall) |
| 5 | no_visual_cause | Frames look fine | suspect estimator, config or timing; controls should mostly land here |
| 6 | unclear | Ambiguous or mixed | use sparingly; its rate is tracked |

Rules:
- Pick the single best label. If two apply, pick the one that looks stronger across more
  frames; use `unclear` only when you cannot choose, and note why.
- Label before any judge has run. Do not look at errors or at pass-1 labels during pass 2.
- Refine this taxonomy after your first 30 labels (guide 8.1), then re-label those 30.

Examples (guide 8.3 asks for 2 example windows per label). Window ids refer to `labels/windows_ui.csv`;
the sheets are regenerable and are not committed. These were picked from human pass-1 labels
(`human_v1.csv`, 2026-11-17) as windows whose frames clearly show the label, before any judge run.

| label | examples | what to look at |
|---|---|---|
| motion_blur | `395c5a404b`, `8e80ee6a16` | whole frames smeared in nearly every tile, not just one |
| low_texture | `b9ad0a51fd` | carpet fills the lower half; little to track (second example still open) |
| illumination | `96f186b4bc`, `583ff18a31` | crushed dark regions next to blown-out windows or walls |
| handling_event | `d64dc6baa8` | view jumps and tilts, ends close to the floor (second example still open) |
| no_visual_cause | `91b59958c6` | sharp, well exposed, rich structure in every tile (second example still open) |
| unclear | `94140fa64d`, `8b8ad410a1` | mixed or changing scene (draped cloth, close board, dark) with no single cause |

Open slots stay empty on purpose: a window goes in only if a second look agrees with the label.
Known boundary pairs seen while choosing: carpet or floor filling the view is low_texture, not
no_visual_cause; a camera resting or descending near the floor is handling_event only if the view
visibly changes within the sheet, otherwise low_texture.
