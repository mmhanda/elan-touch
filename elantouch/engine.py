"""Capture a touch and decide identity. Views are only ever added by enrollment."""
import threading
import time

import numpy as np

from . import config, match as M
from .sensor import Sensor, SensorError
from .store import Calibration, Template

_CFG = config.load()
ACCEPT_Z = _CFG["accept_z"]
SETTLE_R = 0.97
MAX_FRAMES = 45         # ~1.3 s of holding still before giving up on a touch


class Touch:
    OK, TIMEOUT, CANCELLED, UNSETTLED = "ok", "timeout", "cancelled", "unsettled"


class Engine:
    def __init__(self, raw=False):
        """raw=True captures without correction maps - only used to build them."""
        self.sensor = Sensor()
        self.cal = Calibration.load()
        self.raw = raw
        if not raw:
            if not self.cal.ready:
                raise SensorError("this sensor has not been calibrated yet - run: sudo elan-touch calibrate")
            M.set_fixed_pattern(self.cal.fpn_ridge, self.cal.fpn_detail)
        self.lock = threading.Lock()

    def __enter__(self):
        self.sensor.open()
        return self

    def __exit__(self, *exc):
        self.sensor.close()

    def _background(self):
        """Refresh the empty-sensor reference when the sensor is actually empty; else reuse the last."""
        if not self.sensor.finger(120):
            bg = np.mean([self.sensor.frame() for _ in range(3)], axis=0)
            self.cal.save_background(bg)
        elif self.cal.background is None:
            raise SensorError("lift your finger once so the sensor can take its empty reference")
        return self.cal.background

    def touch(self, timeout=15.0, cancel=None, on_finger=None):
        """Wait for a touch and return (status, conditioned image). The finger keeps sliding for the
        first frames; only a run of mutually consistent frames is used."""
        bg, gain = self._background(), (1.0 if self.raw else self.cal.gain)
        deadline = time.time() + timeout
        while not self.sensor.finger(250):
            if cancel is not None and cancel.is_set():
                return Touch.CANCELLED, None
            if time.time() > deadline:
                return Touch.TIMEOUT, None
        if on_finger:
            on_finger(True)
        run, prev = [], None
        for _ in range(MAX_FRAMES):
            lin = (self.sensor.frame() - bg) / gain
            rb = M.ridge_band(lin)
            if prev is not None and M._cc(prev, rb) >= SETTLE_R:
                run.append(lin)
                if len(run) >= 5:
                    break
            else:
                run = [lin] if len(run) < 3 else run
                if len(run) >= 3:
                    break
            prev = rb
            if not self.sensor.finger(200):
                break
        if len(run) < 3:
            return Touch.UNSETTLED, None
        return Touch.OK, np.mean(run, axis=0).astype(np.float32)

    @staticmethod
    def same_placement(a, b):
        """True if two conditioned touches show the same skin in the same place - a finger that
        simply stayed where it was tells the matcher nothing new."""
        return M._cc(M.ridge_band(a), M.ridge_band(b)) >= 0.90

    def wait_lift(self, cancel=None, limit=10.0):
        t0 = time.time()
        clear = 0
        while clear < 2 and time.time() - t0 < limit and not (cancel is not None and cancel.is_set()):
            clear = 0 if self.sensor.finger(150) else clear + 1

    # ---- decisions -----------------------------------------------------------------------

    @staticmethod
    def score(template, lin):
        if not template.views:
            return (-9.0, 0.0, 0.0, -1, (0, 0, 0))
        views = getattr(template, "_cache", None)
        if views is None or len(views) != len(template.views):
            views = template._cache = [M.View(v) for v in template.views]
        return M.match(views, M.Probe(lin))

    # There is deliberately no "learn from a successful verification" here. An earlier version
    # added confident matches to the template; a single false accept then enrolled the wrong
    # finger, which made the next false accept easier, and so on (see docs/FINDINGS.md section 8).
    # Only enrollment - explicit, authenticated, with the owner present - may add views.
