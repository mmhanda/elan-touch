# elan-touch

Touch-to-authenticate for the small **80×80 ELAN USB fingerprint sensors** (`04f3:0c4f`) on Linux.

libfprint lists this sensor as supported, but in practice it enrolls and then never matches - see
the long-running forum threads. `elan-touch` replaces the matching approach entirely and plugs in as
a drop-in **fprintd-compatible D-Bus service**, so `sudo`, polkit prompts, display-manager login and
desktop settings use it without modification.

Rest your finger on the sensor, as on Windows. No swiping.

| | stock libfprint `elan` driver | elan-touch |
|---|---|---|
| technique | slow swipe | touch |
| genuine finger | score **0-3** (needs 24) | accepted on ~4 of 5 prompts (3 placements each) from a sparse 25-view template; better with fuller enrollment |
| a different finger | score 0-3 - indistinguishable from the owner | highest score 4.64 against a threshold of 6.0 (33 recorded touches - a small sample, see below) |
| calibration | aborts with `Calibration failed!` (8 of 10 attempts in one test) | waits the firmware out |
| time per decision | - | ~300 ms |

Measured on one Acer Predator Triton 300 SE (PT314-51s), firmware `0x0161`, Ubuntu 22.04. Details,
method and caveats are in [docs/FINDINGS.md](docs/FINDINGS.md).

## Status and honest limits

* **Tested on a single laptop.** The sensor ships in many Acer/ASUS models; reports are welcome.
* **This is convenience-grade biometrics, not a security product.** The wrong-finger figure comes from
  33 recorded touches of *one person's other fingers*. That shows the matcher separates fingers; it
  cannot establish a false-accept rate (zero accepts in 33 tries only bounds it below ~9 % at 95 %
  confidence). A sensor that sees 3.5 mm of skin is inherently weaker than a full-size reader. Your
  password keeps working everywhere and remains the real credential.
* **Version 0.1.0 had a serious flaw - upgrade.** It added confident verification matches to the
  template. One wrong-finger accept therefore enrolled that finger, and it was then accepted
  routinely. Templates created or used with 0.1.0 should be deleted and re-enrolled. Since 0.2.0
  only enrollment adds views, the threshold carries real margin, and repeated failures pause the
  fingerprint. Details in [docs/FINDINGS.md](docs/FINDINGS.md), section 7.
* Templates are small images of skin patches, stored root-only in `/var/lib/elan-touch/`.
* The matcher is Python + NumPy + OpenCV. A C port (as a libfprint driver) is the long-term goal.

## Install

```sh
git clone https://github.com/mmhanda/elan-touch && cd elan-touch
./install.sh                       # Debian/Ubuntu; installs dependencies, takes over the fprintd name

sudo elan-touch calibrate          # once per device - see "Why calibration" below
sudo elan-touch enroll             # adaptive: stops by itself once you are recognised reliably
sudo elan-touch verify             # live check with scores

sudo elan-touch pam on                                # sudo, polkit, display manager (15 s window)
systemctl --user enable --now elan-touch-unlock       # touch-to-unlock, see below
```

`./uninstall.sh` restores the stock fprintd (`--purge` also deletes calibration and fingers). Nothing
is replaced on disk: the service is a unit named `fprintd.service` in `/etc/systemd/system`, which
takes precedence over the packaged one, and D-Bus activation follows it.

### Touch-to-unlock

Lock screens without fingerprint support of their own (KDE Plasma before 5.25, for one) only start
PAM after you press Enter. `elan-touch-unlock` is a small per-user service that runs a verification
while the session is locked and asks logind to unlock it on a match, so a touch is enough.

### Commands

```
sudo elan-touch status                      sensor, calibration, enrolled fingers
sudo elan-touch calibrate [-n 40] [--force]
sudo elan-touch enroll [--finger right-index-finger] [--extend]
sudo elan-touch verify [-n 10]
sudo elan-touch delete [--finger NAME]
sudo elan-touch pam [on|off]                fingerprint for sudo / polkit / login
```

During a prompt you do not need to lift your finger between attempts: if the first placement is
not recognised, shift it slightly - each new placement is a fresh attempt (five per prompt), while a
finger that has not moved is not counted again.

The standard tools work too: `fprintd-list`, `fprintd-verify`, `fprintd-enroll`, `fprintd-delete`.
Thresholds live in `/etc/elan-touch.conf` ([example](elan-touch.conf.example)).

## How it works

An 80×80 frame at this sensor's resolution holds about **seven ridges** - a 3.5 mm patch of a
fingertip. Three things follow, and each one was measured before it was believed:

1. **The ridge pattern carries almost no identity at this size.** Seven roughly parallel ridges are
   a sinusoidal grating, and one finger's grating aligns with anyone else's: different fingers reach
   a ridge-band correlation of 0.85 at their best alignment, the same as genuine pairs. Minutiae
   (libfprint/bozorth3), SIFT, AKAZE, ORB and plain correlation all fail for this reason.
2. **Identity lives in the fine detail** - ridge edges, width variation, pores - at periods of
   2.4-4.6 px, with the ridge fundamental (~11 px) *and its second harmonic* filtered out. There,
   unrelated fingers correlate at 0.04 and overlapping touches of the same finger at 0.2-0.6.
3. **Half of that band is the sensor, not the finger.** A fixed pattern (pixel response, marks on
   the coating) carries ~49 % of every touch's fine-detail energy. Left in, every finger matches
   every other when simply overlaid; removed, the worst impostor drops from z = 16 to z = 4.6.
   A per-pixel gain map (7 % spread, some pixels at half sensitivity) is corrected first.

The pipeline: flat-field → average the frames where the finger has settled → propose poses by
FFT correlation of the ridge band over rotations → decide on the fixed-pattern-free detail band with
sub-pixel refinement → express the result in standard deviations of the impostor distribution.

**Coverage is the real limit**, not the matcher: a 3.5 mm window lands on different skin each time
(10 of 25 early touches overlapped none of the others). So enrollment is adaptive - it continues
until 8 of your last 10 touches were recognised. Coverage only ever grows through enrollment
(`enroll --extend`, up to 64 views) - never silently during verification, see the 0.1.0 flaw noted above.

### Why calibration

The gain map and fixed pattern are properties of *your* sensor, so they are measured, not shipped:
`elan-touch calibrate` averages ~40 touches from several fingers and positions until the
fingerprints cancel and only the sensor remains. It reports a self-check (unrelated touches should
then correlate below ~0.10 when overlaid).

## A libfprint bug found along the way

This firmware needs **0.3-1.9 s** to finish a calibration cycle; libfprint's `elan` driver gives it
**0.5 s** and then fails the whole operation with `Calibration failed!`. A one-line fix is in
[patches/](patches/libfprint-elan-calibration-timeout.patch), write-up in
[docs/libfprint-calibration-bug.md](docs/libfprint-calibration-bug.md).

## Reproducing the measurements

`tools/collect.py` records raw touches into a **local, root-only** dataset and `tools/evaluate.py`
reports genuine / impostor separation on it. The dataset is raw biometric data: never commit it
(`.gitignore` blocks `*.npz`), delete it when done.

## Credits and licence

The USB wire protocol comes from libfprint's `elan` driver by Igor Filatov and Sébastien Béchet,
whose source comments predicted exactly this outcome for small square sensors.
LGPL-2.1-or-later, the same terms as libfprint, to keep upstreaming possible.
