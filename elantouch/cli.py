"""elan-touch command line."""
import argparse
import os
import shutil
import subprocess
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
            print(f"   {tries:>2}. {'MATCH   ' if matched else 'no match'}  z={z:4.1f}  overlap={overlap:4.0%}  "
                  f"{ms:4.0f} ms  [{tpl.finger}, {len(tpl.views)} views]")
            eng.wait_lift()
    print(f"\nrecognised {ok}/{tries} touches ({ok / max(tries, 1):.0%})")


def cmd_enroll(args):
    user, finger = _user(args), args.finger
    tpl = store.Template.load(user, finger)
    if tpl.views and not args.extend:
        sys.exit(f"{finger} already has {len(tpl.views)} views - use --extend to add more, or delete it first")
    if tpl.full:
        sys.exit(f"{finger} is full ({len(tpl.views)} views). To start over: sudo elan-touch delete --finger {finger}")
    print(f"Use ONE finger for this whole session - every touch becomes part of '{finger}'.")
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
            # topping up an existing finger is about variety: insist on a real session
            if args.extend and n < 20:
                continue
            if len(tpl.views) >= args.min and len(recent) == 10 and sum(recent) >= 8:
                print("\nyour touches are now recognised reliably.")
                break
            if tpl.full:
                print(f"\nthe template is full ({len(tpl.views)} views) - stopping.")
                break
        tpl.save()
    print(f"saved {len(tpl.views)} views. Only enrollment adds views; run `enroll --extend` to widen coverage.")


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


def cmd_check(args):
    """The test that decides whether the fingerprint may be switched on: the enrolled finger must be
    accepted, and no other finger may be - judged live, by the person who knows which is which."""
    user = _user(args)
    templates = [store.Template.load(user, f) for f in store.fingers(user)]
    if not templates:
        sys.exit(f"no fingers enrolled for {user}")

    def series(eng, count):
        scores = []
        while len(scores) < count:
            status, lin = eng.touch(timeout=90)
            if status == Touch.TIMEOUT:
                break
            if status == Touch.UNSETTLED:
                print("        hold still a moment longer - lift and try again")
                eng.wait_lift()
                continue
            z = max(eng.score(t, lin)[0] for t in templates)
            scores.append(z)
            print(f"   {len(scores):>2}/{count}  {'MATCH   ' if z >= ACCEPT_Z else 'no match'}  z={z:4.1f}")
            eng.wait_lift()
        return scores

    with Engine() as eng:
        print(f"PART 1 of 2 - {args.own} touches with your ENROLLED finger only, the way you normally touch.")
        input("   press ENTER when ready... ")
        own = series(eng, args.own)
        print(f"\nPART 2 of 2 - {args.other} touches with OTHER fingers. Never the enrolled one.")
        print("   Change finger every few touches (both hands), and vary the position.")
        input("   press ENTER when ready... ")
        other = series(eng, args.other)

    accepted = sum(z >= ACCEPT_Z for z in own)
    wrong = [z for z in other if z >= ACCEPT_Z]
    print("\n" + "=" * 66)
    print(f" enrolled finger : accepted {accepted}/{len(own)} single touches")
    print(f" other fingers   : accepted {len(wrong)}/{len(other)}"
          + (f"   <-- scores {[round(z, 1) for z in wrong]}" if wrong else ""))
    print(f" highest other-finger score: {max(other) if other else 0:.1f}  (threshold {ACCEPT_Z})")
    if wrong:
        print(" RESULT: FAIL - another finger was accepted. Keep the fingerprint OFF.")
        print("         The template holds prints of more than one finger: delete it and enroll again,")
        print("         one finger only:  sudo elan-touch delete && sudo elan-touch enroll")
    elif not other:
        print(" RESULT: incomplete - part 2 was not done.")
    elif accepted * 3 < len(own):
        print(" RESULT: other fingers are rejected, but your own finger is accepted too rarely.")
        print("         Widen coverage:  sudo elan-touch enroll --extend")
    else:
        print(" RESULT: PASS - to switch the fingerprint on:")
        print("         sudo elan-touch pam on && systemctl --user enable --now elan-touch-unlock")
    print("=" * 66)


# The fingerprint is wired into sudo and the polkit agent ONLY - never into
# /etc/pam.d/common-auth. common-auth is included by the display manager and by
# login, so a fingerprint service that stalls there freezes the machine's login
# screen and locks the user out. Scoped this way the worst case is a slow sudo,
# which Ctrl-C recovers from while the desktop keeps working.
PAM_TARGETS = ("/etc/pam.d/sudo", "/etc/pam.d/polkit-1")
PAM_BEGIN = "# elan-touch begin (remove with: elan-touch pam off)\n"
PAM_END = "# elan-touch end\n"
# success=done ends the auth stack successfully; anything else - including the
# module being absent or broken - is ignored and the password prompt follows.
PAM_LINE = "auth\t[success=done default=ignore]\tpam_fprintd.so max-tries=1 timeout=10\n"
PAM_STALE = "/usr/share/pam-configs/elan-touch"


def _pam_strip(text):
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        if line == PAM_BEGIN:
            skip = True
        elif line == PAM_END:
            skip = False
        elif not skip:
            out.append(line)
    return "".join(out)


def pam_enabled():
    for path in PAM_TARGETS:
        try:
            if PAM_BEGIN in open(path).read():
                return True
        except OSError:
            pass
    return False


def _pam_write(path, text):
    """Replace a PAM file atomically: a half-written /etc/pam.d/sudo is unusable."""
    tmp = path + ".elan-touch.tmp"
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def cmd_pam(args):
    if args.state is not None:
        # Any earlier install put pam_fprintd into common-auth via pam-auth-update.
        # Always take it back out: that is the configuration that can freeze login.
        env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
        for profile in ("elan-touch", "fprintd"):
            subprocess.run(["pam-auth-update", "--remove", profile], env=env,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        if os.path.exists(PAM_STALE):
            os.remove(PAM_STALE)

        for path in PAM_TARGETS:
            try:
                text = _pam_strip(open(path).read())
            except OSError:
                continue
            if args.state == "on":
                anchor = "@include common-auth"
                if anchor not in text:
                    print(f"{path}: no '{anchor}' line, skipped", file=sys.stderr)
                    continue
                text = text.replace(anchor, PAM_BEGIN + PAM_LINE + PAM_END + anchor, 1)
            if not os.path.exists(path + ".elan-touch.orig"):
                shutil.copy2(path, path + ".elan-touch.orig")
            _pam_write(path, text)

    print("fingerprint for sudo and system password dialogs:",
          "on" if pam_enabled() else "off")
    print("login screen and lock screen: never uses the fingerprint via PAM "
          "(see elan-touch-unlock for touch-to-unlock)")


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
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("enroll", help="enroll a finger (adaptive)")
    p.add_argument("--finger", default=DEFAULT_FINGER)
    p.add_argument("--extend", action="store_true")
    p.add_argument("--min", type=int, default=20)
    p.add_argument("--max", type=int, default=80)
    p.set_defaults(func=cmd_enroll)
    p = sub.add_parser("check", help="guided accept/reject test - run it before switching the fingerprint on")
    p.add_argument("--own", type=int, default=10)
    p.add_argument("--other", type=int, default=20)
    p.set_defaults(func=cmd_check)
    p = sub.add_parser("pam", help="use the fingerprint for sudo, polkit and login (Debian/Ubuntu)")
    p.add_argument("state", nargs="?", choices=("on", "off"))
    p.set_defaults(func=cmd_pam)
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
