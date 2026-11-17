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

Examples (guide 8.3 asks for 2 example windows per label): **TODO.** Add 2 window ids per label
here after the first 30 labels, from windows you are confident about. No real windows exist yet.
