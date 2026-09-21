"""elan-touch command line."""
import argparse
import os
import sys
import time

import numpy as np

from . import __version__, match as M, store
from .engine import ACCEPT_Z, Engine, Touch
from .sensor import SensorError

DEFAULT_FINGER = "right-index-finger"


def _user(args):
    return args.user or os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"


def cmd_status(args):
    cal = store.Calibration.load()
    print(f"elan-touch {__version__}")
    print(f"calibration maps : {'present' if cal.ready else 'MISSING'}")
    try:
        with Engine() as eng:
            print(f"sensor           : {eng.sensor.width}x{eng.sensor.height}, firmware 0x{eng.sensor.firmware:04x}")
    except SensorError as exc:
        print(f"sensor           : {exc}")
    user = _user(args)
    names = store.fingers(user)
    print(f"user             : {user}")
    for f in names:
        t = store.Template.load(user, f)
        print(f"  {f:<22} {len(t.views)} views, {int(sum(t.hits))} successful matches")
    if not names:
        print("  (no fingers enrolled)")


def cmd_verify(args):
    user = _user(args)
    names = store.fingers(user)
    if not names:
        sys.exit(f"no fingers enrolled for {user} - run: sudo elan-touch enroll")
    templates = [store.Template.load(user, f) for f in names]
    ok = tries = 0
    with Engine() as eng:
        print(f"touch the sensor ({args.count} rounds, Ctrl-C to stop). Accept threshold z >= {ACCEPT_Z}\n")
        while tries < args.count:
            status, lin = eng.touch(timeout=30)
            if status == Touch.TIMEOUT:
                print("   (waiting...)")
                continue
            if status == Touch.UNSETTLED:
                print("   hold still a moment longer - lift and try again")
                eng.wait_lift()
                continue
            t0 = time.time()
            best = max(((eng.score(t, lin), t) for t in templates), key=lambda x: x[0][0])
            (z, r, overlap, vi, pose), tpl = best
            ms = 1000 * (time.time() - t0)
            tries += 1
            matched = z >= ACCEPT_Z
            ok += matched
            grew = eng.learn(tpl, lin, best[0]) if matched and not args.no_learn else False
            print(f"   {tries:>2}. {'MATCH   ' if matched else 'no match'}  z={z:4.1f}  overlap={overlap:4.0%}  "
                  f"{ms:4.0f} ms  [{tpl.finger}, {len(tpl.views)} views]{'  +learned' if grew else ''}")
            eng.wait_lift()
    print(f"\nrecognised {ok}/{tries} touches ({ok / max(tries, 1):.0%})")


def cmd_enroll(args):
    user, finger = _user(args), args.finger
    tpl = store.Template.load(user, finger)
    if tpl.views and not args.extend:
        sys.exit(f"{finger} already has {len(tpl.views)} views - use --extend to add more, or delete it first")
    print(f"enrolling {finger} for {user}. This sensor sees only ~3.5 mm of skin, so it needs many touches.")
    print("Touch the way you will when logging in. Lift fully each time and let the position vary a little.")
    print(f"It finishes by itself once your touches are being recognised reliably (max {args.max} touches).\n")
    recent, n = [], 0
    with Engine() as eng:
        while n < args.max:
            status, lin = eng.touch(timeout=60)
            if status == Touch.TIMEOUT:
                break
            if status == Touch.UNSETTLED:
                print("   hold still a moment longer - lift and try again")
                eng.wait_lift()
                continue
            n += 1
            z, r, overlap, vi, pose = eng.score(tpl, lin)
            known = z >= ACCEPT_Z
            recent = (recent + [known])[-10:]
            if not known or overlap < 0.85:
                tpl.add(lin)
                tpl._cache = None
            print(f"   {n:>3}. {'recognised (z=%4.1f)' % z if known else 'new area          '}  "
                  f"views={len(tpl.views):>2}  last 10 recognised: {sum(recent)}/{len(recent)}")
            eng.wait_lift()
            if len(tpl.views) >= args.min and len(recent) == 10 and sum(recent) >= 8:
                print("\nyour touches are now recognised reliably.")
                break
        tpl.save()
    print(f"saved {len(tpl.views)} views. It keeps learning from successful logins.")


def cmd_calibrate(args):
    from . import calibrate as C
    cal = store.Calibration.load()
    if cal.ready and not args.force:
        sys.exit("this sensor is already calibrated - use --force to redo it (enrolled fingers must then be re-enrolled)")
    print(f"Calibration measures the SENSOR, not you: {args.count} touches, nothing about them is kept except an average.")
    print("Use SEVERAL different fingers and put them down in different places and angles - variety is what")
    print("makes the fingerprints average out so that only the sensor's own pattern remains.\n")
    raws = []
    with Engine(raw=True) as eng:
        while len(raws) < args.count:
            status, raw = eng.touch(timeout=60)
            if status == Touch.TIMEOUT:
                break
            if status == Touch.OK:
                raws.append(raw)
                print(f"   {len(raws):>3}/{args.count}  ok - lift, then a different finger or position")
            else:
                print("        hold still a moment longer - lift and try again")
            eng.wait_lift()
    if len(raws) < 20:
        sys.exit("not enough touches for a usable calibration (need at least 20)")
    gain, ridge, detail = C.build_maps(raws)
    leftover = C.residual_identity_correlation(raws, gain, ridge, detail)
    cal.save_maps(gain, ridge, detail)
    print(f"\npixel gain spread {gain.std():.1%} (weakest pixel {gain.min():.2f}); fixed pattern carried "
          f"{detail.var():.0%} of the fine-detail energy")
    print(f"after correction, unrelated touches correlate at {leftover:.3f} when overlaid (should be below ~0.10)")
    print("saved. Next: sudo elan-touch enroll")


def cmd_delete(args):
    store.delete(_user(args), args.finger)
    print("deleted")


def main():
    ap = argparse.ArgumentParser(prog="elan-touch", description="Touch fingerprint support for 80x80 ELAN sensors.")
    ap.add_argument("--user")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p = sub.add_parser("verify", help="live recognition test")
    p.add_argument("-n", "--count", type=int, default=10)
    p.add_argument("--no-learn", action="store_true")
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("enroll", help="enroll a finger (adaptive)")
    p.add_argument("--finger", default=DEFAULT_FINGER)
    p.add_argument("--extend", action="store_true")
    p.add_argument("--min", type=int, default=20)
    p.add_argument("--max", type=int, default=80)
    p.set_defaults(func=cmd_enroll)
    p = sub.add_parser("delete")
    p.add_argument("--finger")
    p.set_defaults(func=cmd_delete)
    p = sub.add_parser("calibrate", help="measure this sensor's own pattern (once per device)")
    p.add_argument("-n", "--count", type=int, default=40)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_calibrate)
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("elan-touch needs root for the USB device: run it with sudo")
    try:
        args.func(args)
    except SensorError as exc:
        sys.exit(f"elan-touch: {exc}")
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
