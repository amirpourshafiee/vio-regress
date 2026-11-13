Done (S1): CLAUDE.md; OpenVINS v2.7 image (93adc241, 7.86 GB); fetch_euroc.sh range-fetch (MH_01_easy 3.61 GB of 12.7 GB).
Done (S1): MH_01_easy serial run -> results/MH_01_easy/run1.tum, 2767 poses; ATE posyaw rmse 0.091 m, coverage 0.973, scale 1.008.
Next (S2): scripts/run_sequence.sh wrapping the S1 docker command, writing result JSON with vio_commit/image_digest/harness_commit/config_hash.
Next: strip -g3 debug info from devel/ (802 MB); prune 3 GB build cache; disk is ~4.8 GB free with image + one bag.
Open: is RC 429 rate-limiting a CI risk (cache data/euroc)? commit or ignore poetry.lock? keep bag_start=40 vs skip_s=40 both?
