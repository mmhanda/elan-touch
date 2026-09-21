"""On-disk state: sensor calibration and per-user fingerprint templates (root-only)."""
import os
import time

import numpy as np

ROOT = "/var/lib/elan-touch"
SENSOR = os.path.join(ROOT, "sensor")
USERS = os.path.join(ROOT, "users")
MAX_VIEWS = 64


def _ensure(path):
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _atomic_save(path, **arrays):
    tmp = path + ".tmp.npz"
    np.savez_compressed(tmp, **arrays)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Calibration:
    """Per-device correction maps. `gain` is the per-pixel sensitivity; the fixed patterns are what
    the sensor itself contributes to each band (about half the fine-detail energy of every touch)."""

    def __init__(self):
        self.gain = self.fpn_ridge = self.fpn_detail = self.background = None

    @classmethod
    def load(cls):
        c = cls()
        for name in ("gain", "fpn_ridge", "fpn_detail", "background"):
            path = os.path.join(SENSOR, name + ".npy")
            if os.path.exists(path):
                setattr(c, name, np.load(path).astype(np.float32))
        return c

    @property
    def ready(self):
        return self.gain is not None and self.fpn_detail is not None

    def save_maps(self, gain, fpn_ridge, fpn_detail):
        _ensure(SENSOR)
        for name, arr in (("gain", gain), ("fpn_ridge", fpn_ridge), ("fpn_detail", fpn_detail)):
            np.save(os.path.join(SENSOR, name + ".npy"), arr.astype(np.float32))
            os.chmod(os.path.join(SENSOR, name + ".npy"), 0o600)
            setattr(self, name, arr.astype(np.float32))

    def save_background(self, bg):
        self.background = bg.astype(np.float32)
        _ensure(SENSOR)
        np.save(os.path.join(SENSOR, "background.npy"), self.background)


class Template:
    """All enrolled views of one finger. A view is the conditioned linear image of one touch."""

    def __init__(self, user, finger):
        self.user, self.finger = user, finger
        self.views, self.hits, self.created = [], [], time.time()

    @property
    def path(self):
        return os.path.join(USERS, self.user, self.finger + ".npz")

    @classmethod
    def load(cls, user, finger):
        t = cls(user, finger)
        if os.path.exists(t.path):
            z = np.load(t.path)
            t.views = [v.astype(np.float32) for v in z["views"]]
            t.hits = list(z["hits"]) if "hits" in z else [0] * len(t.views)
            t.created = float(z["created"]) if "created" in z else time.time()
        return t

    def save(self):
        _ensure(os.path.dirname(self.path))
        _atomic_save(self.path, views=np.stack(self.views).astype(np.float16),
                     hits=np.array(self.hits, np.int32), created=self.created)

    @property
    def full(self):
        return len(self.views) >= MAX_VIEWS

    def add(self, lin):
        """Append a view. A full template refuses: silently replacing old views (what 0.2.0 did)
        threw away the earliest - best verified - part of an enrollment."""
        if self.full:
            return False
        self.views.append(lin.astype(np.float32))
        self.hits.append(0)
        return True


def fingers(user):
    d = os.path.join(USERS, user)
    return sorted(f[:-4] for f in os.listdir(d) if f.endswith(".npz")) if os.path.isdir(d) else []


def delete(user, finger=None):
    for f in fingers(user):
        if finger in (None, f):
            os.remove(os.path.join(USERS, user, f + ".npz"))
