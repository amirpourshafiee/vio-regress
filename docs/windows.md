# Window set (session S7)

`python -m vioreg.window_cli` turns scored runs into the labeling set: `labels/windows.csv`
(metadata), `labels/windows_ui.csv` (what the labeler reads) and one contact sheet PNG per
window in `labels/sheets/` (12 left-camera frames, 4 x 3, gitignored, regenerable).

**Sources.** `natural`: the 8 worst 5-second windows of run1 of each sequence. `control`: its 5
best windows. `injected`: the 3 worst windows of each faulted copy in `labels/fault_manifest.csv`
(identity rows skipped). Defaults: `--top-k 8 --control-k 5 --inj-top-k 3`; the guide targets 150+
windows with about 20 to 25 percent controls, and the CLI warns when the set misses that.
Only run1 of a sequence is used (`--all-runs` lifts it): serial repeats are near-duplicates.

**Start-up trim.** Windows are cut only after the first `--trim-start-s` seconds (default
`windows.TRIM_START_S` = 15) counted from each run's **first pose**. This drops the estimator's
start-up transient, which in Stage 1 on MH_01_easy took most of the "worst" windows. It is
**not** the gate's `skip_s` (`configs/thresholds.yaml`, passed as `--skip-s`). That value counts
from ground-truth start and, on the gated sequences, removes no poses at all
(docs/crosscheck.md).

**Scoring sequences outside the gate.** `score.py` refuses a sequence that is not in
`configs/thresholds.yaml`, and only the 3 gated sequences are listed there. `--ungated` scores
an unlisted EuRoC sequence with the defaults (`align` from `defaults`, `skip_s` 0) and prints
`<seq>: UNGATED`. The result JSON keeps its frozen key set. A listed sequence is scored as
before, and the gate still fails any sequence it has no baseline for.

**No repeated moments.** An injected window never overlaps in time a natural or control window
of the same sequence, and stays at least `--inj-gap-s` (default `windows.INJ_GAP_S` = 5 s, one
window length) away from each of them. Without the gap, adjacent windows showed the same scene
clean and then faulted. The injected run takes its next-worst window that keeps the gap. If
fewer than `--inj-top-k` are left, it uses what exists and prints
`NOTE <run_id>: only N of K injected windows`. Injected windows of different faults may still
overlap one another.

**Blind labeling.** The UI reads only `windows_ui.csv` (`window_id`, `sheet`). Window ids are
salted hashes and sheet files are named by id. `windows.csv` has no fault column, and the run id
of a faulted run is an opaque `<seq>__inj-<hash>`, because the real id embeds the fault. The
mapping back is `fault_manifest.csv`.

**Frames.** Cut from the bag inside the pinned image (`vioreg.bagframes`: digest verified, no
network, bag read-only, nearest `/cam0/image_raw` frame to each of 12 evenly spaced times),
written as PGM to a scratch directory, composed here, then deleted.

**Inputs and checks.** Natural runs are `results/<seq>/run1.tum`, only for folders named like a
EuRoC sequence (`^(MH|V1|V2)_0[1-5]_[a-z]+$`), so `results/s1`, `results/variance` and so on are
ignored. Faulted runs are `results/faults/<run_id>/<seq>/run1.tum` (the path `fault_cli` prints).
A manifest row with no results there, a duplicate row, a run with no scorable poses, or a
missing bag is printed as `SKIPPED <run_id>: <reason>`. It is never dropped silently. The set
is then smaller, and the final `check_set` warnings say so.

**All 11 sequences (disk does not hold every bag at once).** Do one sequence at a time, then
one final step:

1. For each sequence, with its bags present: `--sheets-only --seqs <SEQ>`. This cuts the
   missing sheets, keeps the valid ones, and writes no CSVs. It can resume: re-running it cuts
   only what is missing or truncated. Sheets are written to a temp name and renamed.
2. Delete that sequence's bags.
3. At the end: `--reuse-sheets`. This needs no bags and no Docker. If any window lacks a valid
   sheet (missing, truncated, or new ids after a re-run moved the worst windows), it exits 1,
   writes no CSVs and removes old ones. A sheet whose id is not in the set gets a warning and
   never enters the set.

Fault set (S7, decided 2026-11-17): `blur --length 25`, `gamma --gamma 2.2`,
`time-offset --offset-ms 100` on every sequence. That is 3 runs x 3 windows, plus 8 natural and
5 control windows per sequence: at most 242 windows with 22.7 % controls. drop-frames was left
out: nothing in the frames shows it, and it would put controls at exactly 20.0 %. Take run ids
from `fault_cli`, never retype them. The last line of `--print-data-root` is the copy's DATA
root, and its folder name is the run id. Rebuilding a deleted copy (S6 sequences) does not
add a second manifest row.

Pass the same `--fault` list to every `window_cli` call. The manifest also holds the 14 S6 faults
of MH_01_easy, V1_01_easy and MH_03_medium, and those runs have results. Without the filter they
would be planned too, have no bags and no sheets, and the final step would fail. A spec with no
manifest row for a sequence is printed as `SKIPPED <seq> fault <spec>`.

```bash
set -euo pipefail   # a failed fault_cli must stop the loop, not yield an empty RID
PY=.venv/bin/python; S=MH_02_easy
FAULTS="--fault blur=25 --fault gamma=2.2 --fault time-offset=100"   # same on EVERY call
scripts/fetch_euroc.sh data/euroc "$S"
RUNS=1 MODE=serial bash scripts/run_sequence.sh "$S" results
$PY -m vioreg.score --seqs "$S" --out results --data data/euroc --ungated
for f in "blur --length 25" "gamma --gamma 2.2" "time-offset --offset-ms 100"; do
  D=$($PY -m vioreg.fault_cli $f --seq "$S" --print-data-root | tail -n1); RID=$(basename "$D")
  [ -f "$D/$S/$S.bag" ] || { echo "no faulted bag for $f"; exit 1; }
  RUNS=1 MODE=serial DATA="$D" bash scripts/run_sequence.sh "$S" "results/faults/$RID"
  $PY -m vioreg.score --seqs "$S" --out "results/faults/$RID" --data data/euroc --ungated
done
$PY -m vioreg.window_cli --sheets-only --seqs "$S" $FAULTS
# then delete data/euroc/$S/$S.bag and data/faulted/${S}__*; after all 11:
$PY -m vioreg.window_cli --reuse-sheets $FAULTS
```

For MH_01_easy, V1_01_easy and MH_03_medium the fault runs already exist from S6: rebuild the
copies with `fault_cli` but skip the two run/score lines.

The expected labels (blur = motion_blur, gamma = illumination, time-offset = no_visual_cause)
are only for judging the judge afterwards. Labeling stays blind.

**Not yet done.** No real window set exists: it needs all 11 sequences scored and the faulted
runs on every sequence, which run in Codespaces. Tested here with fake bags only; the
rosbag path in `bagframes.extract` has not run against a real bag.
