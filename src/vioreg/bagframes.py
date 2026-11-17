"""Extract left-camera frames from a EuRoC ROS bag at requested times (S7). Runs inside the
pinned VIO image, like vioreg.bagfault.

    python3 -m vioreg.bagframes --bag /in/MH_01_easy.bag --request /out/request.json \
        --out /out/frames

request.json: {"MH_01_easy|0": [t, t, ...], ...}   (key -> times in seconds, header.stamp clock)
Writes <out>/<key-with-|-as-_>_<k:02d>.pgm for every time (the nearest cam0 frame) and
<out>/index.json {key: [{"t": requested, "stamp": used, "file": name}, ...]}.

Python 3.8 + numpy only (no PIL, no cv2 in the image): frames are written as binary PGM and
the host composes the contact sheets. rosbag is imported in main(), so select_nearest and
write_pgm are testable on the host with fake messages.

Only /cam0/image_raw (the LEFT camera, mono8) is read. The bag is never written.
"""
from __future__ import annotations

import argparse
import json
import sys

import numpy as np

CAM_TOPIC = "/cam0/image_raw"


def select_nearest(messages, targets):
    """Stream (stamp_s, payload) pairs in time order; return {target_index: (stamp, payload)}.

    For each target the nearest message wins (ties go to the earlier one). Holds at most one
    previous payload, so a 2.5 GB bag is not loaded into memory. Targets may be unsorted and
    may repeat. Returns {} entries only for targets when the stream is non-empty."""
    order = sorted(range(len(targets)), key=lambda i: targets[i])
    out, ptr, prev = {}, 0, None
    for stamp, payload in messages:
        while ptr < len(order) and targets[order[ptr]] <= stamp:
            i = order[ptr]
            if prev is not None and abs(targets[i] - prev[0]) <= abs(stamp - targets[i]):
                out[i] = prev
            else:
                out[i] = (stamp, payload)
            ptr += 1
        prev = (stamp, payload)
        if ptr >= len(order):
            break
    if prev is not None:  # targets after the last message
        while ptr < len(order):
            out[order[ptr]] = prev
            ptr += 1
    return out


def write_pgm(path, img):
    img = np.ascontiguousarray(img, dtype=np.uint8)
    h, w = img.shape
    with open(path, "wb") as f:
        f.write(b"P5\n%d %d\n255\n" % (w, h))
        f.write(img.tobytes())


def _stream(bag):
    for _topic, msg, _t in bag.read_messages(topics=[CAM_TOPIC]):
        if msg.encoding != "mono8":
            raise SystemExit("bagframes: %s is %s, expected mono8" % (CAM_TOPIC, msg.encoding))
        img = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.width)
        yield msg.header.stamp.to_sec(), img


def extract(bag_path, request, out_dir, stream=None):
    """Write the frames and return the index. stream: injectable for tests."""
    import os
    os.makedirs(out_dir, exist_ok=True)
    keys = sorted(request)
    flat = [(k, j, t) for k in keys for j, t in enumerate(request[k])]
    targets = [t for _, _, t in flat]
    if stream is None:
        import rosbag
        bag = rosbag.Bag(bag_path, "r")
        try:
            found = select_nearest(_stream(bag), targets)
        finally:
            bag.close()
    else:
        found = select_nearest(stream, targets)
    if flat and not found:
        raise SystemExit("bagframes: no %s messages in %s" % (CAM_TOPIC, bag_path))
    index = {k: [] for k in keys}
    for n, (k, j, t) in enumerate(flat):
        stamp, img = found[n]
        name = "%s_%02d.pgm" % (k.replace("|", "_"), j)
        write_pgm(os.path.join(out_dir, name), img)
        index[k].append({"t": t, "stamp": stamp, "file": name})
    with open(os.path.join(out_dir, "index.json"), "w") as f:
        json.dump(index, f, indent=1, sort_keys=True)
    return index


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--bag", required=True)
    p.add_argument("--request", required=True)
    p.add_argument("--out", required=True)
    a = p.parse_args(argv)
    with open(a.request) as f:
        request = json.load(f)
    idx = extract(a.bag, request, a.out)
    print("bagframes: wrote %d frames for %d windows" % (sum(len(v) for v in idx.values()), len(idx)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
