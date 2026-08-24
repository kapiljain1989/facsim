"""Chromatogram synthesis and peak integration.

A real chromatogram is not a list of peak areas. It is a digitised detector
signal, and the peak table is whatever an integration algorithm managed to find
in it. This module keeps that distinction, because it is the distinction a
chromatographer checks first:

* :func:`synthesise` produces a signal trace from the *true* peak parameters —
  exponentially modified Gaussians on a drifting baseline with detector noise.
* :func:`integrate` recovers peaks from that trace with no knowledge of the
  truth: it estimates noise, fits a baseline, detects maxima, walks out to the
  peak boundaries and computes the USP descriptors from the digitised points.

So area, tailing, plate count and resolution are *measurements*, not
declarations. A method whose true resolution is 2.1 reports something near 2.1
with run-to-run scatter, and reports less than 2.0 when the conditions drift —
which is what makes a robustness study mean anything.

Stdlib only, deliberately: ``requirements.txt`` keeps the dependency set to four
packages and there is no reason for this module to break that.

References for the descriptor formulae are USP <621> *Chromatography*; the
exponentially modified Gaussian is Grushka (1972).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from random import Random

__all__ = [
    "PeakSpec",
    "TraceSpec",
    "IntegratedPeak",
    "emg",
    "erfcx",
    "synthesise",
    "integrate",
    "gaussian_half_width",
]

#: Ratio of full width at half maximum to sigma for a Gaussian: 2*sqrt(2*ln2).
_FWHM_PER_SIGMA = 2.0 * math.sqrt(2.0 * math.log(2.0))

#: Above this, ``exp(z*z)`` is at risk and the asymptotic series is both safe
#: and accurate to roughly 1e-9. Below it, the direct product is more accurate.
_ERFCX_ASYMPTOTIC_FROM = 20.0

_SQRT_PI = math.sqrt(math.pi)


def gaussian_half_width(sigma: float) -> float:
    """Width at half height of a pure Gaussian of standard deviation ``sigma``."""
    return _FWHM_PER_SIGMA * sigma


def erfcx(z: float) -> float:
    """Scaled complementary error function, ``exp(z**2) * erfc(z)``.

    Needed because the EMG is a product of a large exponential and a tiny
    ``erfc``. Evaluating them separately overflows one and underflows the other
    long before the product stops being representable.

    For ``z`` below the asymptotic threshold the direct product is used. Above
    it, the standard asymptotic series

        erfcx(z) ~ 1/(z*sqrt(pi)) * (1 - 1/(2z^2) + 3/(4z^4) - 15/(8z^6) + ...)

    which converges quickly once ``z`` is large.
    """
    if z < 0.0:
        # erfcx grows like exp(z^2) for negative z; the reflection keeps the
        # positive branch (where the series is valid) as the only thing computed.
        return 2.0 * math.exp(z * z) - erfcx(-z)
    if z < _ERFCX_ASYMPTOTIC_FROM:
        return math.exp(z * z) * math.erfc(z)

    inv = 1.0 / (2.0 * z * z)
    term = 1.0
    total = 1.0
    for n in range(1, 12):
        term *= -(2.0 * n - 1.0) * inv
        total += term
        if abs(term) < 1e-17:
            break
    return total / (z * _SQRT_PI)


def emg(t: float, centre: float, sigma: float, tau: float, area: float) -> float:
    """Exponentially modified Gaussian at time ``t``.

    A Gaussian convolved with a one-sided exponential decay. ``sigma`` sets the
    symmetric width, ``tau`` the tailing: at ``tau`` near zero the peak is
    Gaussian, and increasing it produces the front-sharp, back-drawn-out shape
    real columns give. ``area`` is the analytic area, so response stays
    proportional to concentration regardless of shape.

    The branch is a numerical one, not a modelling one — both arms compute the
    same function, and the split only avoids overflow. See :func:`erfcx`.
    """
    if sigma <= 0.0:
        raise ValueError("sigma must be positive")
    if tau <= 0.0:
        # Degenerate case: a pure Gaussian, which is what tau -> 0 converges to.
        return area / (sigma * math.sqrt(2.0 * math.pi)) * math.exp(
            -0.5 * ((t - centre) / sigma) ** 2
        )

    delta = t - centre
    z = (sigma / tau - delta / sigma) / math.sqrt(2.0)
    scale = area / (2.0 * tau)

    if z >= 0.0:
        # exp(-delta^2/2sigma^2) <= 1 and erfcx(z) <= 1 here, so nothing grows.
        gauss = math.exp(-0.5 * (delta / sigma) ** 2) if abs(delta / sigma) < 40.0 else 0.0
        return scale * gauss * erfcx(z)

    # z < 0 implies delta > sigma^2/tau > 0, which makes this exponent strictly
    # negative — it can underflow to zero but cannot overflow.
    exponent = sigma * sigma / (2.0 * tau * tau) - delta / tau
    if exponent < -700.0:
        return 0.0
    return scale * math.exp(exponent) * math.erfc(z)


@dataclass(frozen=True, slots=True)
class PeakSpec:
    """The true chromatographic behaviour of one analyte in one injection.

    This is ground truth. Nothing downstream of :func:`synthesise` may read it —
    the whole point is that the peak table is recovered from the trace.
    """

    analyte_id: str
    retention_time_min: float
    sigma_min: float
    tau_min: float
    area: float


@dataclass(frozen=True, slots=True)
class TraceSpec:
    """Acquisition and detector parameters for one injection."""

    run_time_min: float
    sampling_hz: float
    #: Detector noise standard deviation, in response units.
    noise_sigma: float = 0.0
    #: Constant detector offset.
    baseline_offset: float = 0.0
    #: Linear baseline climb across the run, in response units per minute.
    baseline_drift_per_min: float = 0.0
    #: Amplitude of a slow sinusoidal baseline wander, as gradient methods show.
    baseline_wander: float = 0.0
    #: Periods of that wander across the whole run.
    baseline_wander_cycles: float = 1.5

    @property
    def sample_count(self) -> int:
        return int(round(self.run_time_min * 60.0 * self.sampling_hz)) + 1

    def time_at(self, index: int) -> float:
        return index / (self.sampling_hz * 60.0)


def synthesise(
    peaks: tuple[PeakSpec, ...] | list[PeakSpec],
    spec: TraceSpec,
    rng: Random,
) -> tuple[list[float], list[float]]:
    """Build a digitised chromatogram. Returns ``(times_min, response)``.

    Peaks are summed, so an unresolved pair genuinely overlaps and the
    integrator has to cope with it rather than being handed two clean areas.
    """
    times: list[float] = []
    response: list[float] = []
    total_minutes = spec.run_time_min

    for index in range(spec.sample_count):
        t = spec.time_at(index)
        signal = spec.baseline_offset + spec.baseline_drift_per_min * t
        if spec.baseline_wander:
            signal += spec.baseline_wander * math.sin(
                2.0 * math.pi * spec.baseline_wander_cycles * t / max(total_minutes, 1e-9)
            )
        for peak in peaks:
            # Skip analytes that cannot contribute here: an EMG is negligible
            # more than a few sigma before its centre, and this keeps a 6-analyte
            # 15-minute run at 5 Hz to a sensible number of evaluations.
            lead = peak.retention_time_min - 6.0 * peak.sigma_min
            trail = peak.retention_time_min + 8.0 * peak.sigma_min + 12.0 * peak.tau_min
            if t < lead or t > trail:
                continue
            signal += emg(t, peak.retention_time_min, peak.sigma_min, peak.tau_min, peak.area)
        if spec.noise_sigma:
            signal += rng.gauss(0.0, spec.noise_sigma)
        times.append(t)
        response.append(signal)

    return times, response


@dataclass(frozen=True, slots=True)
class IntegratedPeak:
    """What the integrator found. Every field is measured from the trace."""

    index: int
    retention_time_min: float
    area: float
    height: float
    width_half_min: float
    start_min: float
    end_min: float
    tailing_usp: float | None
    plate_count_usp: float | None
    resolution_previous: float | None
    signal_to_noise: float | None
    #: Assigned by the caller against the method's expected retention windows.
    analyte_id: str | None = None


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return 0.0
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return 0.5 * (ordered[middle - 1] + ordered[middle])


#: Fraction of window noise estimates to take. Windows overlapping a peak read
#: high, so the low end of the distribution is where the true noise sits. 0.35
#: was chosen empirically: it lands within 1% of the injected noise on traces
#: carrying six peaks across three orders of magnitude of noise level, at the
#: cost of reading about 9% low on a peak-free trace, where the estimate matters
#: least because there is no signal-to-noise to compute.
_NOISE_QUANTILE = 0.35


def robust_noise_sigma(response: list[float]) -> float:
    """Detector noise standard deviation, estimated in the presence of peaks.

    A standard deviation over the whole trace measures the peaks, not the noise.
    Differencing removes the baseline and most of the peak shape, and the median
    absolute deviation of those differences ignores the largest excursions the
    peak flanks contribute. The 1.4826 factor converts MAD to sigma for a normal
    distribution; the sqrt(2) undoes the variance doubling that differencing
    introduces.

    A single MAD over the whole trace still reads about 30% high, because the
    flanks are numerous enough to move the median. Estimating per window and
    taking a low quantile of those estimates is the same idea as measuring noise
    in a blank stretch, without having to be told where the blank stretch is.
    """
    count = len(response)
    if count < 16:
        return 0.0
    diffs = [response[i + 1] - response[i] for i in range(count - 1)]

    # Window wide enough for a stable MAD, narrow enough that a peak cannot
    # contaminate most windows. Scaled to the trace so cadence does not matter.
    window = max(40, min(400, len(diffs) // 22))
    step = max(1, window // 2)

    estimates: list[float] = []
    for start in range(0, max(1, len(diffs) - window + 1), step):
        chunk = diffs[start : start + window]
        if len(chunk) < 8:
            continue
        centre = _median(chunk)
        estimates.append(_median([abs(value - centre) for value in chunk]))

    if not estimates:
        centre = _median(diffs)
        return 1.4826 * _median([abs(v - centre) for v in diffs]) / math.sqrt(2.0)

    estimates.sort()
    index = min(len(estimates) - 1, int(len(estimates) * _NOISE_QUANTILE))
    return 1.4826 * estimates[index] / math.sqrt(2.0)


def _opened_baseline(response: list[float], window: int) -> list[float]:
    """Morphological opening: rolling minimum, then rolling maximum.

    Erosion followed by dilation removes anything narrower than ``window`` while
    leaving slower baseline movement intact. Used only to *detect* peak regions —
    the baseline each peak is actually measured against is drawn between its own
    start and end points, which is what a chromatography data system does.
    """
    count = len(response)
    if count == 0:
        return []
    half = max(1, window // 2)

    eroded = [
        min(response[max(0, i - half) : min(count, i + half + 1)]) for i in range(count)
    ]
    dilated = [
        max(eroded[max(0, i - half) : min(count, i + half + 1)]) for i in range(count)
    ]
    return dilated


def _interpolate_crossing(
    times: list[float], values: list[float], start: int, stop: int, level: float
) -> float | None:
    """Time at which ``values`` crosses ``level`` walking from ``start`` to ``stop``."""
    if start == stop:
        return None
    step = 1 if stop > start else -1
    for index in range(start, stop, step):
        following = index + step
        if not 0 <= following < len(values):
            break
        a, b = values[index], values[following]
        if (a >= level > b) or (a < level <= b):
            if b == a:
                return times[index]
            frac = (level - a) / (b - a)
            return times[index] + frac * (times[following] - times[index])
    return None


def _descriptors(
    times: list[float],
    corrected: list[float],
    left: int,
    apex: int,
    right: int,
    noise_pp: float,
) -> IntegratedPeak:
    """Measure one peak's USP descriptors from the digitised points."""
    height = corrected[apex]

    area = 0.0
    for index in range(left, right):
        area += 0.5 * (corrected[index] + corrected[index + 1]) * (
            times[index + 1] - times[index]
        )

    half = height / 2.0
    left_half = _interpolate_crossing(times, corrected, apex, left, half)
    right_half = _interpolate_crossing(times, corrected, apex, right, half)
    width_half = (
        right_half - left_half
        if left_half is not None and right_half is not None
        else 0.0
    )

    # USP <621> tailing factor: W(5%) / (2f), f being apex minus leading edge at
    # 5% height. Exactly 1.0 for a symmetric peak, above it for a tailing one.
    five = height * 0.05
    left_five = _interpolate_crossing(times, corrected, apex, left, five)
    right_five = _interpolate_crossing(times, corrected, apex, right, five)
    tailing: float | None = None
    if left_five is not None and right_five is not None:
        front = times[apex] - left_five
        if front > 0.0:
            tailing = (right_five - left_five) / (2.0 * front)

    # USP <621> plate count, half-height method.
    plates = 5.54 * (times[apex] / width_half) ** 2 if width_half > 0.0 else None

    return IntegratedPeak(
        index=0,
        retention_time_min=times[apex],
        area=area,
        height=height,
        width_half_min=width_half,
        start_min=times[left],
        end_min=times[right],
        tailing_usp=tailing,
        plate_count_usp=plates,
        resolution_previous=None,
        signal_to_noise=(2.0 * height / noise_pp) if noise_pp > 0.0 else None,
    )


def integrate(
    times: list[float],
    response: list[float],
    *,
    noise_multiple: float = 4.0,
    min_width_points: int = 5,
    valley_ratio: float = 0.85,
) -> list[IntegratedPeak]:
    """Recover peaks from a trace, with no access to the truth that made it.

    The algorithm follows what a chromatography data system does, in the same
    order:

    1. Estimate detector noise robustly (:func:`robust_noise_sigma`).
    2. Find *regions* of the trace that rise above the noise, using a
       morphological baseline for detection only.
    3. Draw each region's baseline as a straight line between its start and end
       points — the classic baseline drop.
    4. Split a region at its valleys when it holds more than one apex, which is
       the perpendicular drop applied to an unresolved pair.
    5. Measure the USP descriptors from the digitised points.

    Args:
        noise_multiple: a region must exceed the baseline by this many noise
            sigmas to be considered a peak.
        min_width_points: narrower excursions are digitisation artefacts.
        valley_ratio: a valley between two apices splits them only if it falls
            below this fraction of the lower apex. Higher means more willing to
            split a shoulder off; 0.85 keeps a genuine shoulder while refusing to
            split a single peak that noise gave two crests.

    Returns peaks in retention order.
    """
    count = len(response)
    if count < min_width_points * 3:
        return []

    sigma = robust_noise_sigma(response)
    #: Peak-to-peak noise, the quantity USP signal-to-noise is defined against.
    noise_pp = 6.0 * sigma

    span = max(response) - min(response)
    # A floor relative to the trace itself, so a noise-free synthetic trace does
    # not drive the threshold to zero and turn float dust into peaks.
    threshold = max(noise_multiple * sigma, span * 1.0e-3, 1.0e-12)

    detection_window = max(min_width_points * 4, count // 20)
    rough = _opened_baseline(response, detection_window)
    detect = [response[i] - rough[i] for i in range(count)]

    # Contiguous regions above threshold.
    regions: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(count):
        if detect[index] > threshold:
            if start is None:
                start = index
        elif start is not None:
            if index - start >= min_width_points:
                regions.append((start, index))
            start = None
    if start is not None and count - start >= min_width_points:
        regions.append((start, count - 1))

    peaks: list[IntegratedPeak] = []
    for left, right in regions:
        # Widen to where the signal genuinely returns to the rough baseline, so
        # the tail is integrated rather than clipped at the threshold crossing.
        edge = threshold * 0.05
        while left > 0 and detect[left - 1] > edge:
            left -= 1
        while right < count - 1 and detect[right + 1] > edge:
            right += 1
        if right - left < min_width_points:
            continue

        # Baseline drop: a straight line under this region only.
        y0, y1 = response[left], response[right]
        run = float(right - left)
        corrected = [0.0] * count
        for index in range(left, right + 1):
            corrected[index] = response[index] - (
                y0 + (y1 - y0) * (index - left) / run
            )

        local_max = max(corrected[left : right + 1])
        if local_max <= threshold:
            continue

        apices = [
            index
            for index in range(left + 1, right)
            if corrected[index] >= corrected[index - 1]
            and corrected[index] > corrected[index + 1]
            and corrected[index] > threshold
        ]
        if not apices:
            apices = [max(range(left, right + 1), key=lambda i: corrected[i])]

        # Merge apices that belong to one crest: adjacent maxima with no real
        # valley between them, or maxima too close to be resolved at all.
        kept: list[int] = []
        for apex in apices:
            if kept:
                previous = kept[-1]
                valley = min(corrected[previous : apex + 1])
                lower = min(corrected[previous], corrected[apex])
                if apex - previous < min_width_points or valley > valley_ratio * lower:
                    if corrected[apex] > corrected[previous]:
                        kept[-1] = apex
                    continue
            kept.append(apex)

        # Perpendicular drop at the valley between consecutive apices.
        bounds: list[tuple[int, int, int]] = []
        for position, apex in enumerate(kept):
            sub_left = left if position == 0 else bounds[-1][2]
            if position == len(kept) - 1:
                sub_right = right
            else:
                following = kept[position + 1]
                sub_right = min(
                    range(apex, following + 1), key=lambda i: corrected[i]
                )
            bounds.append((sub_left, apex, sub_right))

        for sub_left, apex, sub_right in bounds:
            if sub_right - sub_left < min_width_points:
                continue
            peaks.append(
                _descriptors(times, corrected, sub_left, apex, sub_right, noise_pp)
            )

    peaks.sort(key=lambda peak: peak.retention_time_min)

    # USP <621> resolution, half-height form: R = 1.18 dtR / (W1(50%) + W2(50%)).
    resolved: list[IntegratedPeak] = []
    for position, peak in enumerate(peaks):
        resolution: float | None = None
        if position > 0:
            previous = peaks[position - 1]
            widths = previous.width_half_min + peak.width_half_min
            if widths > 0.0:
                resolution = (
                    1.18 * (peak.retention_time_min - previous.retention_time_min) / widths
                )
        resolved.append(
            IntegratedPeak(
                index=position + 1,
                retention_time_min=peak.retention_time_min,
                area=peak.area,
                height=peak.height,
                width_half_min=peak.width_half_min,
                start_min=peak.start_min,
                end_min=peak.end_min,
                tailing_usp=peak.tailing_usp,
                plate_count_usp=peak.plate_count_usp,
                resolution_previous=resolution,
                signal_to_noise=peak.signal_to_noise,
            )
        )
    return resolved
