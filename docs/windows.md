# Window set (session S7)

`python -m vioreg.window_cli` turns scored runs into the labeling set: `labels/windows.csv`
(metadata), `labels/windows_ui.csv` (what the labeler reads) and one contact sheet PNG per
window in `labels/sheets/` (12 left-camera frames, 4 x 3, gitignored, regenerable).

**Sources.** `natural`: the 8 worst 5-second windows of run1 of each sequence. `control`: its 5
best windows. `injected`: the 3 worst windows of each faulted copy in `labels/fault_manifest.csv`
(identity rows skipped). Defaults: `--top-k 8 --control-k 5 --inj-top-k 3`; the guide targets 150+
windows with about 20 to 25 percent controls, and the CLI warns when the set misses that.
Only run1 of a sequence is used (`--all-runs` lifts it): serial repeats are near-duplicates.

**Blind labeling.** The UI reads only `windows_ui.csv` (`window_id`, `sheet`). Window ids are
salted hashes and sheet files are named by id. `windows.csv` has no fault column, and the run id
of a faulted run is an opaque `<seq>__inj-<hash>`, because the real id embeds the fault. The
mapping back is `fault_manifest.csv`.

**Frames.** Cut from the bag inside the pinned image (`vioreg.bagframes`: digest verified, no
network, bag read-only, nearest `/cam0/image_raw` frame to each of 12 evenly spaced times),
written as PGM to a scratch directory, composed here, then deleted.

**Not yet done.** No real window set exists: it needs all 11 sequences scored and the faulted
runs on every sequence, which run in Codespaces. Tested here with fake bags only; the
rosbag path in `bagframes.extract` has not run against a real bag.
