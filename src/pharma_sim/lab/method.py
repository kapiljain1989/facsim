"""The observation layer: what an injection of a sample actually looks like.

``methods.yaml`` declares the *true* chromatographic behaviour of each analyte
and how it responds to the operating conditions. This module turns that truth
plus a set of conditions plus who ran it on what into a digitised chromatogram.

Nothing here writes a result. It produces a trace; the peak table comes from
:func:`pharma_sim.lab.chromatography.integrate` reading that trace back. Keeping
the two apart is the whole point — otherwise "measured resolution" would just be
the configured number with noise added.

Variability is composed from independent sources, each drawn from its own named
RNG stream so that adding an instrument does not perturb another instrument's
history:

===========================  ==============================================
source                       shared by
===========================  ==============================================
sample preparation           every injection of the same preparation
injection                    one injection
analyst bias                 every injection that analyst performs
day bias                     every injection on that day
instrument bias, precision   configured bias is fixed; precision is per
                             injection
calibration drift            grows with days since the instrument was
                             calibrated
column ageing                grows with injections on that column
===========================  ==============================================

This is why validated repeatability and intermediate precision come out
*different*: repeatability holds analyst, day and instrument fixed, and
intermediate precision changes all three at once.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from pharma_sim.engine.rng import RngRegistry
from pharma_sim.lab.chromatography import (
    IntegratedPeak,
    PeakSpec,
    TraceSpec,
    integrate,
    synthesise,
)
from pharma_sim.lab.config import (
    Analyst,
    Conditions,
    Instrument,
    LabConfig,
    Method,
    MethodAnalyte,
)

__all__ = [
    "ColumnState",
    "Preparation",
    "InjectionRequest",
    "InjectionResult",
    "MethodModel",
    "condition_multiplier",
]

#: Flow is the one condition whose effect is a power law rather than a local
#: linearisation: retention is inversely proportional to flow rate over the whole
#: usable range, so a fractional-per-unit coefficient would be wrong at the ends.
_EXPONENT_FACTORS = frozenset({"flow_rate_ml_min"})


def condition_multiplier(
    sensitivity: dict[str, float], actual: Conditions, reference: Conditions
) -> float:
    """Combined effect of the operating conditions on one peak property.

    ``flow_rate_ml_min`` is applied as ``(actual/reference) ** coefficient``;
    every other factor as ``1 + coefficient * (actual - reference)``. A factor
    absent from ``sensitivity`` has no effect.
    """
    multiplier = 1.0
    actual_values = actual.as_dict()
    reference_values = reference.as_dict()
    for factor, coefficient in sensitivity.items():
        current = actual_values[factor]
        nominal = reference_values[factor]
        if factor in _EXPONENT_FACTORS:
            if nominal > 0.0 and current > 0.0:
                multiplier *= (current / nominal) ** coefficient
        else:
            multiplier *= 1.0 + coefficient * (current - nominal)
    return max(multiplier, 1e-6)


@dataclass(frozen=True, slots=True)
class ColumnState:
    """Which column, and how much life it has had."""

    column_id: str
    injections: int = 0

    def after(self, count: int) -> ColumnState:
        return ColumnState(self.column_id, self.injections + count)


@dataclass(frozen=True, slots=True)
class Preparation:
    """One sample preparation, which may be injected more than once.

    The distinction matters and is easy to lose: five replicate injections of one
    preparation measure the *instrument*, and six separate preparations measure
    the *method*. System suitability does the former, repeatability the latter,
    and they give different numbers because preparation error only enters once.
    """

    preparation_id: str
    sample_id: str
    #: Nominal concentration per analyte, in the method's declared units.
    concentrations: dict[str, float] = field(default_factory=dict)
    #: What this preparation is for, carried through to the result for context.
    purpose: str = "SAMPLE"
    #: Filter applied, if any — filter validation compares against unfiltered.
    filter: str | None = None
    #: Hours the solution stood before injection, for solution stability.
    standing_hours: float = 0.0


@dataclass(frozen=True, slots=True)
class InjectionRequest:
    """Everything that makes one injection different from another."""

    injection_id: str
    sequence_id: str
    injection_number: int
    preparation: Preparation
    conditions: Conditions
    instrument: Instrument
    analyst: Analyst
    column: ColumnState
    #: Day index within the study, so a day bias can be shared across a day.
    day_index: int = 0
    #: Days between the instrument's last calibration and this injection.
    days_since_calibration: float = 0.0


@dataclass(frozen=True, slots=True)
class InjectionResult:
    """A completed injection: the trace, and what the integrator found in it."""

    injection_id: str
    sequence_id: str
    injection_number: int
    preparation_id: str
    sample_id: str
    purpose: str
    method_id: str
    instrument_id: str
    analyst_id: str
    column_id: str
    conditions: Conditions
    times_min: list[float]
    response: list[float]
    peaks: tuple[IntegratedPeak, ...]
    #: Ground truth, for the evaluation store only. Never exported operationally.
    truth: tuple[PeakSpec, ...] = ()

    def peak_for(self, analyte_id: str) -> IntegratedPeak | None:
        for peak in self.peaks:
            if peak.analyte_id == analyte_id:
                return peak
        return None

    def area_of(self, analyte_id: str) -> float | None:
        peak = self.peak_for(analyte_id)
        return None if peak is None else peak.area

    @property
    def total_area(self) -> float:
        return sum(peak.area for peak in self.peaks)


class MethodModel:
    """Runs injections for one analytical method."""

    __slots__ = ("_method", "_config", "_rngs")

    def __init__(self, method: Method, config: LabConfig, rngs: RngRegistry) -> None:
        self._method = method
        self._config = config
        self._rngs = rngs

    @property
    def method(self) -> Method:
        return self._method

    # ------------------------------------------------------------------ truth
    def expected_retention(self, analyte: MethodAnalyte, request: InjectionRequest) -> float:
        """Retention time under the request's conditions and column age.

        Used both to build the peak and, afterwards, to decide which integrated
        peak is which — a robustness condition can move retention by 10%, so
        matching against the nominal time would fail exactly when it matters.
        """
        ageing = self._method.column_ageing
        multiplier = condition_multiplier(
            analyte.retention_sensitivity, request.conditions, self._method.nominal_conditions
        )
        aged = 1.0 + ageing.retention_per_injection * request.column.injections
        return analyte.retention_time_min * multiplier * max(aged, 0.5)

    def _peak_spec(self, analyte: MethodAnalyte, request: InjectionRequest) -> PeakSpec | None:
        concentration = request.preparation.concentrations.get(analyte.analyte_id, 0.0)
        if concentration <= 0.0:
            return None

        ageing = self._method.column_ageing
        nominal = self._method.nominal_conditions

        retention = self.expected_retention(analyte, request)
        jitter = self._method.variability.retention_jitter_min
        if jitter > 0.0:
            retention += self._stream("retention", request).gauss(0.0, jitter)

        # Width scales with retention so that plate count stays put unless
        # efficiency itself changed, then takes the efficiency terms and the
        # column's accumulated wear.
        width_scale = retention / analyte.retention_time_min
        sigma = (
            analyte.sigma_min
            * width_scale
            * condition_multiplier(analyte.efficiency_sensitivity, request.conditions, nominal)
            * (1.0 + ageing.sigma_per_injection * request.column.injections)
        )
        tau = (
            analyte.tau_min
            * condition_multiplier(analyte.tailing_sensitivity, request.conditions, nominal)
            * (1.0 + ageing.tau_per_injection * request.column.injections)
        )

        area = concentration * analyte.response_factor * self._response_multiplier(request)
        # Injection volume is proportional to mass on column.
        area *= request.conditions.injection_volume_ul / nominal.injection_volume_ul

        return PeakSpec(
            analyte_id=analyte.analyte_id,
            retention_time_min=max(retention, 0.05),
            sigma_min=max(sigma, 1e-4),
            tau_min=max(tau, 0.0),
            area=max(area, 0.0),
        )

    def _stream(self, purpose: str, request: InjectionRequest):
        return self._rngs.child(
            "lab", self._method.method_id, purpose, request.injection_id
        )

    def _response_multiplier(self, request: InjectionRequest) -> float:
        """Composed response bias for one injection.

        Each term has its own stream keyed by the thing it belongs to, so the
        preparation term is identical across replicate injections of the same
        preparation while the injection term is not.
        """
        variability = self._method.variability
        method_id = self._method.method_id
        multiplier = 1.0

        if variability.sample_preparation_rsd > 0.0:
            prep = self._rngs.child(
                "lab", method_id, "preparation", request.preparation.preparation_id
            )
            multiplier *= 1.0 + prep.gauss(0.0, variability.sample_preparation_rsd)

        if variability.injection_rsd > 0.0:
            multiplier *= 1.0 + self._stream("injection", request).gauss(
                0.0, variability.injection_rsd
            )

        if variability.day_bias_sd > 0.0:
            day = self._rngs.child("lab", method_id, "day", request.day_index)
            multiplier *= 1.0 + day.gauss(0.0, variability.day_bias_sd)

        analyst = request.analyst
        multiplier *= 1.0 + analyst.bias
        if analyst.precision_rsd > 0.0:
            multiplier *= 1.0 + self._stream(
                f"analyst:{analyst.analyst_id}", request
            ).gauss(0.0, analyst.precision_rsd)

        instrument = request.instrument
        multiplier *= 1.0 + instrument.bias
        if instrument.precision_rsd > 0.0:
            multiplier *= 1.0 + self._stream(
                f"instrument:{instrument.instrument_id}", request
            ).gauss(0.0, instrument.precision_rsd)

        # Detector response walks away from its calibrated value with time.
        multiplier *= 1.0 + (
            instrument.calibration.response_drift_per_day * request.days_since_calibration
        )

        # A solution that has stood loses a little analyte. Declared per method
        # via solution stability; approximated here as a slow first-order loss.
        if request.preparation.standing_hours > 0.0:
            multiplier *= math.exp(-0.00035 * request.preparation.standing_hours)

        return max(multiplier, 0.0)

    # ------------------------------------------------------------- observation
    def trace_spec(self, request: InjectionRequest) -> TraceSpec:
        detector = self._method.detector
        return TraceSpec(
            run_time_min=self._method.run_time_min,
            sampling_hz=self._method.sampling_hz,
            noise_sigma=detector.noise_sigma,
            baseline_offset=detector.baseline_offset,
            baseline_drift_per_min=detector.baseline_drift_per_min,
            baseline_wander=detector.baseline_wander,
            baseline_wander_cycles=detector.baseline_wander_cycles,
        )

    def inject(self, request: InjectionRequest, *, keep_trace: bool = True) -> InjectionResult:
        """Acquire one injection and integrate it.

        Args:
            keep_trace: retain the digitised points on the result. A validation
                study is ~140 injections of several thousand points each, so a
                caller that only wants the peak table can drop them.
        """
        specs = tuple(
            spec
            for spec in (
                self._peak_spec(analyte, request) for analyte in self._method.analytes
            )
            if spec is not None
        )
        noise_stream = self._stream("detector", request)
        times, response = synthesise(specs, self.trace_spec(request), noise_stream)
        peaks = self._assign(integrate(times, response), request)

        return InjectionResult(
            injection_id=request.injection_id,
            sequence_id=request.sequence_id,
            injection_number=request.injection_number,
            preparation_id=request.preparation.preparation_id,
            sample_id=request.preparation.sample_id,
            purpose=request.preparation.purpose,
            method_id=self._method.method_id,
            instrument_id=request.instrument.instrument_id,
            analyst_id=request.analyst.analyst_id,
            column_id=request.column.column_id,
            conditions=request.conditions,
            times_min=times if keep_trace else [],
            response=response if keep_trace else [],
            peaks=peaks,
            truth=specs,
        )

    def _assign(
        self, peaks: list[IntegratedPeak], request: InjectionRequest
    ) -> tuple[IntegratedPeak, ...]:
        """Label integrated peaks with the analyte each one is.

        Matched against retention *expected under these conditions*, not the
        nominal time, and within a window scaled to the peak — which is how a
        data system with a retention window behaves. A peak matching nothing
        keeps ``analyte_id`` as ``None`` and is reported as unknown, which is
        what should happen to a degradant nobody declared.
        """
        expected: list[tuple[str, float]] = []
        for analyte in self._method.analytes:
            expected.append((analyte.analyte_id, self.expected_retention(analyte, request)))

        # Every (peak, analyte) pair that falls inside a retention window,
        # assigned best-gap-first. Walking the peaks in retention order and
        # taking the first acceptable analyte is wrong: a small artefact 0.14 min
        # from the expected time would claim the analyte before the real peak
        # 0.02 min away ever got to ask.
        candidates: list[tuple[float, int, str]] = []
        for position, peak in enumerate(peaks):
            for analyte_id, retention in expected:
                window = max(0.12, 0.03 * retention)
                gap = abs(peak.retention_time_min - retention)
                if gap <= window:
                    candidates.append((gap, position, analyte_id))
        candidates.sort()

        assignment: dict[int, str] = {}
        taken: set[str] = set()
        for _, position, analyte_id in candidates:
            if position in assignment or analyte_id in taken:
                continue
            assignment[position] = analyte_id
            taken.add(analyte_id)

        labelled: list[IntegratedPeak] = []
        for position, peak in enumerate(peaks):
            best = assignment.get(position)
            labelled.append(
                IntegratedPeak(
                    index=peak.index,
                    retention_time_min=peak.retention_time_min,
                    area=peak.area,
                    height=peak.height,
                    width_half_min=peak.width_half_min,
                    start_min=peak.start_min,
                    end_min=peak.end_min,
                    tailing_usp=peak.tailing_usp,
                    plate_count_usp=peak.plate_count_usp,
                    resolution_previous=peak.resolution_previous,
                    signal_to_noise=peak.signal_to_noise,
                    analyte_id=best,
                )
            )
        return tuple(labelled)
