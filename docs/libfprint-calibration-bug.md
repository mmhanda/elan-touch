# elan: `Calibration failed!` because the firmware needs longer than the driver waits

*Ready to file at https://gitlab.freedesktop.org/libfprint/libfprint/-/issues*

**Device:** ELAN `04f3:0c4f`, firmware `0x0161`, 80×80, in an Acer Predator PT314-51s.
**libfprint:** 1.94.3 (Ubuntu 22.04). `drivers/elan.c` and `elan.h` are functionally identical in
1.95.2 (only `g_memdup2` and the emulation helper changed), so current releases are affected.

## Symptom

Enroll and verify abort intermittently with the fatal error `Calibration failed!`, typically after
the sensor has been used a few times in a row. In one verification run 8 of 10 consecutive
attempts failed this way, followed by `The driver encountered a protocol error with the device`
(`pre_scan` answering `0xff`, "not calibrated"). Because the error is fatal rather than a retry,
fprintd abandons the whole enrollment.

## Cause

`elan_need_calibration()` asks for a recalibration whenever the fresh background mean differs from
the stored calibration mean by more than `ELAN_CALIBRATION_MAX_DELTA` - which happens as soon as the
sensor has warmed up or picked up moisture. The calibrate state machine then polls `0x40 0x23` and
expects the status to go `0x01` (busy) → `0x03` (done), allowing `ELAN_CALIBRATION_ATTEMPTS` = 10
polls 50 ms apart, i.e. **0.5 s**.

Timing the same cycle directly over USB on this firmware (four consecutive cycles, polling every
50 ms, empty sensor):

| cycle | `0x01` → `0x03` |
|---|---|
| 1 | 1.13 s |
| 2 | 1.94 s |
| 3 | 0.72 s |
| 4 | 0.31 s |

Three of four exceed the driver's budget. The firmware does finish - the driver just stops
listening, and the next activation usually trips the same check again.

## Proposed fix

Raise `ELAN_CALIBRATION_ATTEMPTS` to 100 (5 s), still well inside `ELAN_CMD_TIMEOUT`:

```diff
-#define ELAN_CALIBRATION_ATTEMPTS 10
+#define ELAN_CALIBRATION_ATTEMPTS 100
```

Not yet built against libfprint itself: the timing above was measured with a standalone USB client
(`elantouch/sensor.py` in https://github.com/mmhanda/elan-touch), which waits up to 6 s and has not
seen a calibration failure since. Reporting the condition as a retry instead of a fatal error would
make enrollment more forgiving still.

## Separate from this bug

Matching on this 80×80 sensor does not work even when calibration succeeds (bozorth3 scores 0-3
for the enrolled finger and for other fingers alike, against a threshold of 24). That is a matcher
limitation rather than a driver bug; measurements and a working alternative are in the repository
above.
