#!/usr/bin/env python3
"""
Measure the matcher on a dataset recorded with collect.py:  sudo tools/evaluate.py [DIR]

Genuine: every touch of finger A against a template made of all OTHER touches of A (leave-one-out).
Impostor: every touch of B/C/D against A's full template, and A's touches against theirs.
Prints numbers only.
"""
import glob
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from elantouch import calibrate as C, match as M  # noqa: E402


def main():
    root = sys.argv[1] if len(sys.argv) > 1 else "/var/lib/elan-touch-dev/dataset"
    raw = {}
    ones = np.ones((M.H, M.W), np.float32)
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        for f in sorted(glob.glob(os.path.join(d, "*.npz"))):
            z = np.load(f)
            x = M.condition(z["frames"].astype(np.float32), z["background"], ones)
            if x is not None:
                raw.setdefault(os.path.basename(d), []).append(x)
    if "A" not in raw:
        sys.exit(f"no dataset in {root}")
    gain, ridge, detail = C.build_maps([x for v in raw.values() for x in v])
    M.set_fixed_pattern(ridge, detail)
    print("settled touches:", {k: len(v) for k, v in raw.items()},
          f"| pixel gain spread {gain.std():.1%}, fixed pattern = {detail.var():.0%} of the detail band")
    lin = {k: [x / gain for x in v] for k, v in raw.items()}
    views = {k: [M.View(x) for x in v] for k, v in lin.items()}
    gen = [M.match(views["A"][:i] + views["A"][i + 1:], M.Probe(x))[0] for i, x in enumerate(lin["A"])]
    imp = []
    for k in lin:
        if k != "A":
            imp += [M.match(views["A"], M.Probe(x))[0] for x in lin[k]]
            imp += [M.match(views[k], M.Probe(x))[0] for x in lin["A"]]
    gen, imp = np.array(gen), np.array(imp)
    print(f"impostor z : median {np.median(imp):.2f}  p90 {np.percentile(imp, 90):.2f}  max {imp.max():.2f}   (n={len(imp)})")
    print(f"genuine  z : median {np.median(gen):.2f}  max {gen.max():.2f}   (n={len(gen)}, leave-one-out)")
    for thr in (imp.max() + 0.01, 5.0, 5.5, 6.0):
        print(f"   z >= {thr:4.2f}: accepts {(gen >= thr).mean():>4.0%} of genuine touches, {(imp >= thr).sum()}/{len(imp)} impostors")


if __name__ == "__main__":
    main()
