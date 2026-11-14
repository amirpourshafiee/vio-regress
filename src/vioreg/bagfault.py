"""Apply ONE fault to a COPY of a EuRoC ROS bag (S6). Runs inside the pinned VIO image.

    python3 -m vioreg.bagfault --in /in/MH_01_easy.bag --out /out/MH_01_easy.bag \
        --fault time-offset --params '{"offset_ms": 5}' --seed 0 --summary /out/summary.json

Never call this directly on original data: vioreg.fault_cli mounts the original bag
read-only, checks its checksum before and after, and only writes under data/faulted/.

Python 3.8 + numpy 1.17 only (the pinned image has no scipy); rosbag is imported in main()
so the pure core below is testable on the host with fake messages.

Bag layout (inspected in the pinned image): /cam0/image_raw and /cam1/image_raw are
sensor_msgs/Image mono8 752x480 at 20 Hz; /imu0 is sensor_msgs/Imu at 200 Hz;
/leica/position is geometry_msgs/PointStamped. OpenVINS' serial node orders and pairs
messages by bag RECORD time, and uses header.stamp as the measurement time. So:
  imu-noise    /imu0 angular_velocity and linear_acceleration + N(0, sigma) (seeded);
  gamma, blur  pixel data of both cameras;
  drop-frames  keep every k-th stereo pair (cam0 + cam1 with the same header.stamp);
  time-offset  header.stamp of both cameras + offset (a camera clock offset);
  identity     nothing (control: proves the rewrite alone changes no pose).
Record times, topics and every other field are never changed.
"""
from __future__ import annotations

import argparse
import json
import math
import sys

import numpy as np

from . import faults

CAM_TOPICS = ("/cam0/image_raw", "/cam1/image_raw")
IMU_TOPIC = "/imu0"

# fault -> {param: (type, check, description)}
SPEC = {
    "identity": {},
    "imu-noise": {"sigma_w": (float, lambda v: 0 <= v <= 1.0, "gyro sigma rad/s, 0..1"),
                  "sigma_a": (float, lambda v: 0 <= v <= 10.0, "accel sigma m/s^2, 0..10")},
    "gamma": {"gamma": (float, lambda v: 0.1 <= v <= 10 and v != 1.0, "0.1..10, not 1")},
    "blur": {"length": (int, lambda v: 3 <= v <= 51 and v % 2 == 1, "odd px, 3..51")},
    "drop-frames": {"keep_every": (int, lambda v: 2 <= v <= 10, "2..10")},
    "time-offset": {"offset_ms": (float, lambda v: v != 0 and abs(v) <= 1000,
                                  "ms, nonzero, |v| <= 1000")},
}
RANDOM_FAULTS = {"imu-noise"}  # the only ones the seed affects


class BagFaultError(RuntimeError):
    pass


def validate(fault: str, params: dict) -> dict:
    """Typed, range-checked params, exactly the keys the fault takes."""
    if fault not in SPEC:
        raise BagFaultError(f"unknown fault {fault!r}; one of {sorted(SPEC)}")
    spec = SPEC[fault]
    if set(params) != set(spec):
        raise BagFaultError(f"{fault} takes exactly {sorted(spec)}, got {sorted(params)}")
    out = {}
    for k, (typ, ok, desc) in spec.items():
        v = params[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise BagFaultError(f"{fault}.{k} must be a number, got {v!r}")
        if typ is int and float(v) != int(v):
            raise BagFaultError(f"{fault}.{k} must be an integer, got {v!r}")
        v = typ(v)
        if not ok(v):
            raise BagFaultError(f"{fault}.{k}={v} out of range ({desc})")
        out[k] = v
    return out


def _stamp(msg) -> int:
    return int(msg.header.stamp.to_nsec())


def _pixels(msg) -> np.ndarray:
    if msg.encoding != "mono8":
        raise BagFaultError(f"only mono8 images are supported, got {msg.encoding!r}")
    a = np.frombuffer(msg.data, np.uint8)
    if a.size != msg.height * msg.step or msg.step < msg.width:
        raise BagFaultError(f"image size mismatch: {a.size} bytes for {msg.height}x{msg.step}")
    return a.reshape(msg.height, msg.step)


def _set_pixels(msg, rows: np.ndarray, img: np.ndarray) -> None:
    rows = rows.copy()
    rows[:, :msg.width] = img
    msg.data = rows.tobytes()


class Plan:
    """What the second pass needs, precomputed in the first (streaming; images never held)."""

    def __init__(self, fault, params, seed):
        self.fault, self.params, self.seed = fault, params, seed
        self.imu_noise = None   # (n, 6) array, consumed in record order
        self.imu_i = 0
        self.keep = None        # set of kept camera header stamps (drop-frames)


def make_plan(fault, params, seed, read) -> Plan:
    p = Plan(fault, validate(fault, params), int(seed))
    if fault == "imu-noise":
        n = sum(1 for _ in read([IMU_TOPIC]))
        zeros = np.zeros((n, 7))
        noisy = faults.add_imu_noise(zeros, p.params["sigma_w"], p.params["sigma_a"], p.seed)
        p.imu_noise = noisy[:, 1:7]
    elif fault == "drop-frames":
        stamps = {t: set() for t in CAM_TOPICS}
        for topic, msg, _ in read(list(CAM_TOPICS)):
            stamps[topic].add(_stamp(msg))
        if stamps[CAM_TOPICS[0]] != stamps[CAM_TOPICS[1]]:
            raise BagFaultError("drop-frames needs every cam0 frame to have a stereo partner "
                                "in cam1 with the same header.stamp")
        p.keep = set(faults.drop_frames(sorted(stamps[CAM_TOPICS[0]]), p.params["keep_every"]))
    return p


def transform(plan: Plan, topic, msg, make_time):
    """The message to write (modified in place), or None to drop it."""
    f = plan.fault
    if topic == IMU_TOPIC and f == "imu-noise":
        d = plan.imu_noise[plan.imu_i]
        plan.imu_i += 1
        for k, v in zip("xyz", d[0:3]):
            setattr(msg.angular_velocity, k, getattr(msg.angular_velocity, k) + float(v))
        for k, v in zip("xyz", d[3:6]):
            setattr(msg.linear_acceleration, k, getattr(msg.linear_acceleration, k) + float(v))
    elif topic in CAM_TOPICS:
        if f == "gamma":
            rows = _pixels(msg)
            _set_pixels(msg, rows, faults.apply_gamma(rows[:, :msg.width], plan.params["gamma"]))
        elif f == "blur":
            rows = _pixels(msg)
            _set_pixels(msg, rows, faults.motion_blur(rows[:, :msg.width], plan.params["length"]))
        elif f == "drop-frames":
            if _stamp(msg) not in plan.keep:
                return None
        elif f == "time-offset":
            new = int(faults.shift_cam_time(np.array([_stamp(msg)], dtype=np.int64),
                                            plan.params["offset_ms"])[0])
            msg.header.stamp = make_time(new)
    return msg


def rewrite(read, write, fault, params, seed, make_time) -> dict:
    """read(topics=None) -> iterator of (topic, msg, record_time), in record order; called
    once more per pass. write(topic, msg, record_time). Returns written counts per topic."""
    plan = make_plan(fault, params, seed, read)
    counts = {}
    for topic, msg, t in read(None):
        out = transform(plan, topic, msg, make_time)
        if out is not None:
            write(topic, out, t)  # the ORIGINAL record time, always
            counts[topic] = counts.get(topic, 0) + 1
    if plan.imu_noise is not None and plan.imu_i != len(plan.imu_noise):
        raise BagFaultError(f"IMU count changed between passes ({plan.imu_i} vs "
                            f"{len(plan.imu_noise)})")
    return counts


def _imu_values(msg):
    return tuple(getattr(msg.angular_velocity, k) for k in "xyz") + \
        tuple(getattr(msg.linear_acceleration, k) for k in "xyz")


def compare(orig, faulted) -> dict:
    """Summary of what changed, per topic: message counts, header-stamp shift (for stamps
    present in both, matched in order), IMU values changed, and whether the record-time
    sequence of the surviving messages is unchanged."""
    def collect(it):
        d = {}
        for topic, msg, t in it:
            e = d.setdefault(topic, {"stamps": [], "rt": [], "imu": [], "data": []})
            e["stamps"].append(_stamp(msg) if hasattr(msg, "header") else None)
            e["rt"].append(int(t.to_nsec()))
            if topic == IMU_TOPIC:
                e["imu"].append(_imu_values(msg))
            if topic in CAM_TOPICS:
                e["data"].append(hash(bytes(msg.data)))
        return d
    a, b = collect(orig), collect(faulted)
    s = {"topics": {}, "counts_equal": True, "record_times_equal": True}
    for topic in sorted(set(a) | set(b)):
        ea, eb = a.get(topic, {"stamps": [], "rt": []}), b.get(topic, {"stamps": [], "rt": []})
        n = [len(ea["rt"]), len(eb["rt"])]
        row = {"count": n}
        if n[0] != n[1]:
            s["counts_equal"] = False
            rt_a = set(ea["rt"])
            s["record_times_equal"] &= all(r in rt_a for r in eb["rt"])
        else:
            s["record_times_equal"] &= ea["rt"] == eb["rt"]
            if ea["stamps"] and ea["stamps"][0] is not None:
                diffs = [y - x for x, y in zip(ea["stamps"], eb["stamps"])]
                row["stamp_shift_ns"] = {"min": min(diffs), "max": max(diffs)}
            if topic == IMU_TOPIC:
                row["values_changed"] = sum(x != y for x, y in zip(ea["imu"], eb["imu"]))
            if topic in CAM_TOPICS:
                row["images_changed"] = sum(x != y for x, y in zip(ea["data"], eb["data"]))
        s["topics"][topic] = row
    return s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Apply one fault to a copy of a EuRoC bag.")
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fault", required=True, choices=sorted(SPEC))
    ap.add_argument("--params", default="{}", help="JSON object")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--summary", default=None, help="write the compare() summary here (JSON)")
    a = ap.parse_args(argv)
    import rosbag  # noqa: PLC0415 - only inside the image
    import rospy  # noqa: PLC0415

    if a.src == a.out:
        raise SystemExit("bagfault: refusing to write over the input")
    params = json.loads(a.params)
    make_time = lambda ns: rospy.Time(int(ns) // 1_000_000_000, int(ns) % 1_000_000_000)
    with rosbag.Bag(a.src, "r") as src, rosbag.Bag(a.out, "w") as dst:
        counts = rewrite(lambda topics=None: src.read_messages(topics=topics), dst.write,
                         a.fault, params, a.seed, make_time)
    with rosbag.Bag(a.src, "r") as x, rosbag.Bag(a.out, "r") as y:
        summary = compare(x.read_messages(), y.read_messages())
    summary.update({"fault": a.fault, "params": validate(a.fault, params), "seed": a.seed,
                    "written": counts})
    text = json.dumps(summary, indent=1, sort_keys=True)
    if a.summary:
        with open(a.summary, "w") as f:
            f.write(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
