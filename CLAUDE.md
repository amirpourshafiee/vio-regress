# CLAUDE.md — working rules for vioreg

vioreg is an open-source VIO (visual-inertial odometry) regression harness on the
public EuRoC MAV dataset. These rules apply to every session.

## Data and provenance
- Public data and public code only. Never reference, copy, or describe anything from
  any employer (code, data, configs, names, internal URLs, results).

## Workflow
- Before any change, create a branch named `feature-<brief-description>`. Never commit
  to `main` directly.
- Write tests first, or alongside the code they cover.
- Run `pytest -m "not slow and not sequence"` before every commit. Do not commit on red.
- Commit messages are detailed: a short subject, then a body that explains what changed
  and why (including anything rejected or deferred).
- Never commit datasets, rosbags, keys/secrets, or any file over 1 MB. `data/` and
  `results/` are gitignored; keep it that way.
- Do not `git push`, and do not edit anything under `.github/workflows/`, without asking
  the owner first.

## Harness invariants
- The gate fails closed: missing results, unparsable JSON, missing metrics, low
  association coverage, or an unknown sequence is a FAIL, never a skip or a pass.
- Timestamps are float seconds everywhere inside the harness (EuRoC nanoseconds are
  converted at load time).
- Quaternions are x y z w internally (EuRoC CSV is w x y z; convert at load time).
- Every result JSON records `vio_commit`, `image_digest`, `harness_commit`, and
  `config_hash`. A result without all four is invalid.

## Session log
- `NEXT.md` holds a 5-line handoff (done, next, open questions). Update it at the end
  of every session.
