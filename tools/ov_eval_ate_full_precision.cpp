// Calls ov_eval's own ResultTrajectory::calculate_ate (unmodified libov_eval_lib from the
// pinned image) and prints the same statistics as error_singlerun, at full precision.
// usage: ate_full_precision <align_mode> <file_gt.txt> <file_est.txt>   (same order as error_singlerun)
#include <cstdio>
#include "calc/ResultTrajectory.h"
#include "utils/Statistics.h"
int main(int argc, char **argv) {
  if (argc < 4) { std::fprintf(stderr, "usage: %s <align_mode> <gt> <est>\n", argv[0]); return 2; }
  ov_eval::ResultTrajectory traj(argv[3], argv[2], argv[1]);  // (est, gt, mode), as error_singlerun.cpp:109
  ov_eval::Statistics ori, pos;
  traj.calculate_ate(ori, pos);
  std::printf("ov_eval %s rmse_pos=%.9f mean_pos=%.9f max_pos=%.9f min_pos=%.9f n=%zu\n",
              argv[1], pos.rmse, pos.mean, pos.max, pos.min, pos.values.size());
  return 0;
}
