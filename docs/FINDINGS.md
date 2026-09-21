# Findings

Everything here was measured on one machine: Acer Predator Triton 300 SE (PT314-51s), ELAN
`04f3:0c4f`, firmware `0x0161`, Ubuntu 22.04, libfprint 1.94.3. One person, four fingers
(A = login finger, 36 touches; B, C, D = other fingers, 12 touches each), recorded with
`tools/collect.py`. Small numbers - read them as "does this separate fingers at all", not as
population statistics. Only statistics were ever inspected; no fingerprint image was viewed.

## 1. The sensor

* 80×80 px, raw 16-bit frames over bulk USB, ~29 ms per frame.
* Ridge energy peaks at 7 cycles / 80 px: a ridge period of ~11.4 px, so a frame holds about seven
  ridges, roughly a 3.5 mm patch.
* An empty sensor reads a stable ~5380 counts; a finger adds ~5000 with a ridge contrast of ~14 %.
* A finger keeps sliding and pressing for the first frames of a touch: the first frame correlates
  0.09 with the last (band-passed), while settled consecutive frames correlate 0.99-1.00. Only a
  run of mutually consistent frames is usable.

## 2. Baseline: stock libfprint

| technique | assembled image | minutiae |
|---|---|---|
| touch | 120×141-154 | 4-6 |
| slow swipe toward the user | 120×253-409 | 7-15 |
| slow swipe sideways | 120×120-210 | 1-4 |

The driver is written for swiping (`FP_SCAN_TYPE_SWIPE`, frame stitching) and its orientation
assumption is right for this laptop, but even the best swipe yields ~15 minutiae against the
40-100 of a full print. Enrolled with slow swipes, then verified:

| | bozorth3 score (threshold 24) |
|---|---|
| enrolled finger | 3, 0 |
| a different finger | 0, 0, 3 |

8 of 10 genuine attempts died with `Calibration failed!` (see the bug write-up).

## 3. What does not work, and why

First attempts on single touch frames (leave-one-out for the login finger, other fingers as
impostors):

| matcher | genuine median | impostor max | genuine rejected at FAR = 0 |
|---|---|---|---|
| SIFT (×3 upscaled, ratio test, RANSAC similarity) | 0 inliers | 4 | 68 % |
| AKAZE | 0 | 6 | 77 % |
| ORB | 0 | 12 | 82 % |
| rotation-searched masked NCC, ridge band | 0.68 | 0.84 | 97 % |

Keypoint methods find nothing repeatable in seven near-parallel ridges. Correlation fails for a
more fundamental reason - the ridge band itself is not discriminative at this size:

| required overlap | impostor ridge-band NCC, max over alignments |
|---|---|
| ≥ 35 % | 0.85 |
| ≥ 50 % | 0.81 |
| ≥ 80 % | 0.71 |

Two patches of roughly parallel ridges with the same period are two gratings; they align.

## 4. Two sensor effects nobody corrects for

**Per-pixel gain.** The mean response under a finger, divided by its smooth part, has a spread of
7-8 % with some pixels at half sensitivity. Ridge contrast is 14 %, so this pattern is about half as
strong as the ridges and is printed onto every touch. Background subtraction does not remove it
(it is multiplicative). Uncorrected, unrelated fingers correlate 0.07 when merely overlaid.

**Fixed pattern in the fine-detail band.** After flat-fielding, the detail band (periods 2.4-4.6 px)
of unrelated fingers still correlates **0.59** when overlaid with no shift. Averaging the band over
many touches exposes a fixed pattern holding ~49 % of the band's energy; it is stable (r = 0.96
between the pattern estimated from finger A and from fingers B+C+D). Projecting it out brings the
overlay correlation to -0.05. Left in, it produces false matches at the identity pose (impostor
z = 10-16); everywhere else it acts as noise as strong as the signal.

## 5. What does work

The fine-detail band, with the ridge fundamental **and its second harmonic** (5.7 px) excluded,
aligned using poses proposed by the ridge band:

| | detail-band correlation |
|---|---|
| unrelated fingers | median 0.04, p99 0.14 |
| impostor pairs whose ridges align best (ridge r = 0.80, 0.77, ...) | 0.03-0.14 |
| same finger, overlapping touches | 0.2-0.6 |

A wider "detail" band that still lets the harmonic through sits at 0.27 for impostors - the ridges
leak in and the separation is gone.

Scores are reported as z = r·√(overlap px / 7): the correlation in standard deviations of what
unrelated images give for that overlap area (7 px per independent sample was measured from the
impostor spread).

| stage added | worst impostor z | genuine accepted at FAR = 0 |
|---|---|---|
| detail band, top-4 poses per view, whole-pixel | 5.96 | 48 % |
| + overlap-weighted pose ranking (without fixed-pattern removal) | 16.1 | 8 % |
| + fixed-pattern removal | 4.47 | 60 % |
| + sub-pixel refinement (Fourier zero-padding, ×4) | 4.64 | 68 % |

With the published code and maps rebuilt by `calibrate`: no other-finger touch above z = 4.64, and
half of the genuine leave-one-out touches at z ≥ 5.0 from a sparse 26-view template. (An earlier
version of this document said "0 of 111 impostor attempts". Those 111 were not independent - the same
33 touches were scored in both directions and against sub-templates - and zero accepts in 33
independent tries cannot bound the false-accept rate below about 9 %. See section 8.)

## 6. Coverage

Registering the 25 usable enrollment touches against each other (both directions must agree; they
do, to ≤ 1.1 px at the patch corners, which is what makes these registrations trustworthy) gives
one group of six, a few pairs, and **ten touches that overlap nothing**. That is the ceiling on
acceptance, and no matcher change moves it. Hence:

* adaptive enrollment - keep going until 8 of the last 10 touches were recognised. On the
  development machine that took 12 more touches on top of the 25 seeded ones;
* ~~learning in use~~ - removed in 0.2.0, see section 7. Widen coverage with `enroll --extend`.

Live after enrollment (0.1.0, threshold 5.0): 11 of 15 single touches recognised, ~300 ms per decision
with 40 views.

## 7. The 0.1.0 flaw: learning from verification

Version 0.1.0 added any verification match with z ≥ 5.5-6 and < 80 % overlap to the template, to grow
coverage over time. On the development machine the owner then tried fingers that were never
enrolled, repeatedly, and some prompts succeeded.

What the audit of that template showed:

* The 25 views that came from the recorded enrollment session score z ≈ 24 against the recorded
  touches of the enrolled finger and 2-4 against the others: genuine.
* The last learned view scored **6.40 against a different finger** and 2.50 against the enrolled one:
  it was that other finger's print. The touch that created it had been accepted because it matched
  an *earlier learned view* at 6.34 - while scoring only 2.49 and 4.02 against the views that came
  from enrollment.
* Against the enrollment-only views, no other-finger touch exceeded 4.64, for any template size from
  5 to 25 views. Template size was not the cause; a first suspicion that it was came from the
  foreign views being present in the larger random subsets.

So the mechanism was a feedback loop, not the matcher's separation: a threshold (5.0) with almost no
margin over the worst wrong-finger score (4.64), unlimited placements, and a template that absorbed
whatever it accepted. One accept enrolled the wrong finger; after that it matched its own print.

Changes in 0.2.0:

* verification never modifies the template; only enrollment (explicit, authenticated) adds views;
* threshold 6.0 instead of 5.0 (worst wrong-finger score on a clean template: 4.64);
* three placements per prompt instead of five, and after three failed prompts in a row the
  fingerprint pauses for 60 s, doubling each time up to 15 min (the password is unaffected);
* the documentation no longer states a false-accept count it cannot support.

Cost: with a sparse 25-view template the enrolled finger is accepted on 40 % of single placements,
about 78 % of prompts. That is what a fuller enrollment is for.

## 8. Open questions

* False accepts across people. Needs volunteers and a protocol; nothing here substitutes for it.
* Mosaicking the views into one registered template would give near-total overlap for every probe
  and average template noise down; the registration evidence in §6 says it is feasible.
* The fixed pattern probably drifts as the coating wears; it could be tracked from ordinary use.
* Other 80×80 ELAN product ids are untested.
