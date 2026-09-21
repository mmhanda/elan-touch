"""
elanmatch - partial-print matcher for 80x80 ELAN touch sensors (04f3:0c4f family).

Identity is decided on a clean fine-detail band (ridge fundamental and 2nd harmonic removed):
two different fingers' ridges align almost as well as the same finger's on a 3.5 mm patch, but
their ridge edges / pores do not. The ridge band is only used to propose poses.
"""
import numpy as np
import cv2

H = W = 80
N = 128                                        # FFT size; shifts beyond +-MAXS would wrap around
MAXS = N - W
COARSE = np.arange(-30, 31, 3)                 # pose proposals
_fy, _fx = np.meshgrid(np.fft.fftfreq(H), np.fft.fftfreq(W), indexing="ij")
_RAD = np.hypot(_fy, _fx)
_WIN = np.outer(np.hanning(H) ** 0.25, np.hanning(W) ** 0.25).astype(np.float32)
_ONES = np.ones((H, W), np.float32)
NEFF_PX = 7.0                                  # detail-band pixels per independent sample (measured)


def _lcn(d):
    return (d / (cv2.GaussianBlur(d * d, (0, 0), 8) ** 0.5 + 1e-3)).astype(np.float32)


def ridge_band(lin):
    return _lcn(cv2.GaussianBlur(lin, (0, 0), 1.0) - cv2.GaussianBlur(lin, (0, 0), 4.5))


def detail_band(lin, p_lo=2.4, p_hi=4.6):
    lo, hi = 1.0 / p_hi, 1.0 / p_lo
    m = 1 / (1 + np.exp(-(_RAD - lo) / 0.008)) / (1 + np.exp((_RAD - hi) / 0.008))
    return _lcn(np.real(np.fft.ifft2(np.fft.fft2(lin * _WIN) * m)).astype(np.float32))


FIXED = {"ridge": None, "detail": None}       # the sensor's own pattern in each band (zero-mean)


def set_fixed_pattern(ridge=None, detail=None):
    """About half of the detail-band energy of every touch is a fixed sensor pattern (pixel response
    errors, marks on the coating). It must be projected out or every finger matches every other
    at the identity pose - and it acts as noise at every other pose."""
    FIXED["ridge"], FIXED["detail"] = ridge, detail


def _deflate(img, which):
    P = FIXED[which]
    if P is None:
        return img
    x = img - img.mean()
    out = x - (x * P).sum() / ((P * P).sum() + 1e-9) * P
    return (out / (cv2.GaussianBlur(out * out, (0, 0), 8) ** 0.5 + 1e-3)).astype(np.float32)


def bands(lin):
    return _deflate(ridge_band(lin), "ridge"), _deflate(detail_band(lin), "detail")


def _cc(a, b):
    a, b = a - a.mean(), b - b.mean()
    return float((a * b).sum() / np.sqrt((a * a).sum() * (b * b).sum() + 1e-9))


def condition(frames, bg, gain, settle_r=0.97, min_run=2):
    """Flat-field the frames of one touch and average the longest run where the finger is at rest.
    Returns None if the finger never settled."""
    lin = [(f.astype(np.float32) - bg) / gain for f in frames]
    rb = [ridge_band(x) for x in lin]
    r = [_cc(rb[i], rb[i + 1]) for i in range(len(rb) - 1)] + [0.0]
    best, cur = (0, -1), None
    for i, v in enumerate(r):
        if v >= settle_r:
            cur = i if cur is None else cur
        elif cur is not None:
            if i - cur > best[1] - best[0]:
                best = (cur, i)
            cur = None
    if best[1] - best[0] + 1 < min_run:
        return None
    return np.mean(lin[best[0]:best[1] + 1], axis=0)


def _rot(img, ang, nearest=False):
    m = cv2.getRotationMatrix2D((W / 2, H / 2), float(ang), 1.0)
    return cv2.warpAffine(img, m, (W, H), flags=cv2.INTER_NEAREST if nearest else cv2.INTER_LINEAR)


class View:
    """An enrolled touch: FFTs of both bands."""

    def __init__(self, lin):
        self.lin = lin.astype(np.float32)
        r, d = bands(lin)
        self.F_ridge, self.F_detail = np.fft.rfft2(r, (N, N)), np.fft.rfft2(d, (N, N))
        self.F_detail_full = np.fft.fft2(d, (N, N))


_F_ONES = np.fft.rfft2(_ONES, (N, N))


class Probe:
    """A touch to verify: rotated copies are built lazily and cached."""

    def __init__(self, lin):
        self.ridge, self.detail = bands(lin)
        self._cache = {}

    def at(self, ang):
        """(conj FFT of rotated ridge band, conj FFT of rotated detail band, overlap-count map)"""
        ang = int(ang)
        if ang not in self._cache:
            mask = _rot(_ONES, ang, True)
            gr, gd = _rot(self.ridge, ang) * mask, _rot(self.detail, ang) * mask
            ov = np.fft.irfft2(_F_ONES * np.conj(np.fft.rfft2(mask, (N, N))), (N, N))
            self._cache[ang] = (np.conj(np.fft.rfft2(gr, (N, N))), np.conj(np.fft.rfft2(gd, (N, N))), ov)
        return self._cache[ang]


UP = 4                                         # sub-pixel factor for the final decision
SUB = {"dx": 0.0, "dy": 0.0}                   # sub-pixel offset of the last refined peak


def _subpixel_peak(view, probe, ang, ix, iy, reach=1):
    """Band-limited (Fourier zero-padded) interpolation of the detail correlation around an integer
    peak. The detail band has ~3 px periods: half a pixel of misalignment halves the correlation."""
    mask = _rot(_ONES, ang, True)
    gd = _rot(probe.detail, ang) * mask
    X = np.fft.fftshift(view.F_detail_full * np.conj(np.fft.fft2(gd, (N, N))))
    big = np.zeros((N * UP, N * UP), complex)
    o = (N * UP - N) // 2
    big[o:o + N, o:o + N] = X
    c = np.real(np.fft.ifft2(np.fft.ifftshift(big))) * UP * UP
    offs = list(range(-reach * UP, reach * UP + 1))
    ys = [(iy * UP + d) % (N * UP) for d in offs]
    xs = [(ix * UP + d) % (N * UP) for d in offs]
    win = c[np.ix_(ys, xs)]
    j = int(np.argmax(win))
    SUB["dy"], SUB["dx"] = offs[j // len(offs)] / UP, offs[j % len(offs)] / UP
    return float(win.flat[j])


_sy, _sx = np.meshgrid(np.fft.fftfreq(N, 1 / N), np.fft.fftfreq(N, 1 / N), indexing="ij")
_VALID = (np.abs(_sy) <= MAXS) & (np.abs(_sx) <= MAXS)


def _corr(Fv, Gp, ov, min_overlap):
    """Mean product over the overlap = correlation, because both bands have unit local variance."""
    c = np.fft.irfft2(Fv * Gp, (N, N))
    return np.where(_VALID & (ov >= min_overlap * H * W), c / np.maximum(ov, 1.0), -1.0)


def match(views, probe, keep=60, min_overlap=0.30, subpixel=8):
    """Returns the best (z, r, overlap_fraction, view_index, (angle, dx, dy))."""
    props = []
    for vi, v in enumerate(views):
        for ang in COARSE:
            Gr, _, ov = probe.at(ang)
            # rank by overlap-weighted evidence: chance alignments on small overlaps reach a high
            # raw correlation and would otherwise crowd out the true large-overlap pose
            e = _corr(v.F_ridge, Gr, ov, min_overlap) * np.sqrt(ov)
            k = int(np.argmax(e))
            props.append((float(e.flat[k]), vi, int(ang), k))
    props.sort(reverse=True)

    best = (-9.0, 0.0, 0.0, -1, (0, 0, 0))
    cands = []
    for _, vi, ang0, k in props[:keep]:
        iy, ix = divmod(k, N)
        for ang in range(ang0 - 2, ang0 + 3):
            _, Gd, ov = probe.at(ang)
            c = _corr(views[vi].F_detail, Gd, ov, min_overlap)
            ys = [(iy + d) % N for d in (-2, -1, 0, 1, 2)]
            xs = [(ix + d) % N for d in (-2, -1, 0, 1, 2)]
            win, ovw = c[np.ix_(ys, xs)], ov[np.ix_(ys, xs)]
            z = win * np.sqrt(np.maximum(ovw, 1.0) / NEFF_PX)
            j = int(np.argmax(z))
            jy, jx = divmod(j, 5)
            cands.append((float(z.flat[j]), float(win.flat[j]), float(ovw.flat[j]), vi, ang, xs[jx], ys[jy]))
    cands.sort(reverse=True)
    for z0, r0, ovn, vi, ang, ix, iy in cands[:subpixel]:
        r = _subpixel_peak(views[vi], probe, ang, ix, iy) / max(ovn, 1.0)
        z = r * np.sqrt(ovn / NEFF_PX)
        if z > best[0]:
            best = (z, r, ovn / (H * W), vi, (ang, _sx[0, ix] + SUB["dx"], _sy[iy, 0] + SUB["dy"]))
    if not cands:
        return best
    return best if subpixel else (cands[0][0], cands[0][1], cands[0][2] / (H * W), cands[0][3], (cands[0][4], 0, 0))
