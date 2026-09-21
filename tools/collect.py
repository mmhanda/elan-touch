#!/usr/bin/env python3
"""
Record raw touches into a local dataset for matcher research:  sudo tools/collect.py [DIR]

This stores RAW FINGERPRINT IMAGES. Keep the directory root-only, never commit it, and delete it
when you are done. Finger "A" is the one you would log in with; B, C, D are other fingers used as
impostors. Nothing is displayed - the only feedback is per-touch statistics.
"""
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from elantouch.sensor import Sensor  # noqa: E402

PLAN = [("A", "the finger you would log in with", 36), ("B", "a different finger", 12),
        ("C", "another different finger", 12), ("D", "another different finger", 12)]
MAX_FRAMES = 30


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "/var/lib/elan-touch-dev/dataset"
    os.makedirs(root, mode=0o700, exist_ok=True)
    s = Sensor()
    s.open()
    try:
        for label, who, count in PLAN:
            d = os.path.join(root, label)
            os.makedirs(d, mode=0o700, exist_ok=True)
            i = len([n for n in os.listdir(d) if n.endswith(".npz")])
            if i >= count:
                continue
            input(f"=== finger {label}: {who} - {count} touches. Finger OFF the sensor, press ENTER... ")
            bg = np.mean([s.frame() for _ in range(3)], axis=0)
            while i < count:
                print(f"   [{label} {i + 1:>2}/{count}] touch and rest ~1 s ... ", end="", flush=True)
                while not s.finger(500):
                    pass
                frames = [s.frame()]
                while len(frames) < MAX_FRAMES and s.finger(200):
                    frames.append(s.frame())
                if len(frames) >= 5:
                    np.savez_compressed(os.path.join(d, f"{i:03d}.npz"), frames=np.stack(frames).astype(np.uint16),
                                        background=bg.astype(np.float32), t=time.time())
                    i += 1
                    print(f"ok ({len(frames)} frames) - lift")
                else:
                    print("too short - again")
                clear = 0
                while clear < 3:
                    clear = 0 if s.finger(150) else clear + 1
                if i % 12 == 0:
                    bg = np.mean([s.frame() for _ in range(3)], axis=0)
    finally:
        s.close()


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("run with sudo (raw USB access)")
    main()
