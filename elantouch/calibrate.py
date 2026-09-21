"""
Per-device correction maps.

Two properties of the sensor must be measured before matching can work, because together they are
about as strong as the fingerprint itself:
  * gain        - per-pixel sensitivity (7% spread on the unit this was developed on; some pixels at half)
  * fixed pattern - what the sensor contributes to each band regardless of the finger. In the
                  fine-detail band it was ~49% of the energy of every touch.
Both are estimated by averaging many touches from different fingers and positions, so that ridges
average out and only the sensor remains.
"""
import cv2
import numpy as np

from . import match as M


def build_maps(raws):
    """raws: settled (frame - background) averages, one per touch, NOT gain-corrected."""
    mean = np.mean(raws, axis=0)
    gain = (mean / cv2.GaussianBlur(mean, (0, 0), 10)).astype(np.float32)   # smooth part = pressure, not pixels
    lins = [r / gain for r in raws]
    ridge = np.mean([M.ridge_band(x) for x in lins], axis=0)
    detail = np.mean([M.detail_band(x) for x in lins], axis=0)
    return gain, (ridge - ridge.mean()).astype(np.float32), (detail - detail.mean()).astype(np.float32)


def residual_identity_correlation(raws, gain, ridge, detail):
    """After correction, two different touches must be uncorrelated when simply overlaid."""
    M.set_fixed_pattern(ridge, detail)
    bands = [M.bands(r / gain)[1] for r in raws]
    idx = np.random.default_rng(0).choice(len(bands), size=(min(200, len(bands) ** 2), 2))
    return float(np.median([abs(M._cc(bands[i], bands[j])) for i, j in idx if i != j]))
