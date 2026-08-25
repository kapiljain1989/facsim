"""ICH Q1A stability, and the shelf life fitted from it.

Nothing here declares a shelf life. Degradation runs on Arrhenius kinetics, the
samples are pulled on the ICH schedule, each pull is *injected on the assay
method* and read back off a synthesised chromatogram, and the shelf life is where
the regression on the limiting attribute meets its specification. Change the
activation energy and the answer changes.

That reuse matters more than it looks. A stability trend built from a formula
plus noise is smooth in a way real trends are not: the same method, analyst and
instrument variability that shows up in a release test also shows up here, and
because analyst and instrument rotate across three years of pulls, a stability
point carries more scatter than a release point does. That scatter is what the
confidence bound in the Q1E fit is for, so a trend without it produces a
shelf life that is too long and looks defensible.

The LIMS records -- sample login, test, second-person review, certificate --
are emitted here because stability is currently their only consumer. When
release testing goes through the same lifecycle they should move to their own
module rather than be duplicated.
"""

from __future__ import annotations

import math
import statistics as stats
from dataclasses import dataclass, field
from datetime import date, timedelta

from pharma_sim.engine.ids import IdFactory
from pharma_sim.engine.rng import RngRegistry
from pharma_sim.lab.config import (
    LabConfig,
    StabilityProtocol,
    StorageCondition,
)
from pharma_sim.lab.method import (
    ColumnState,
    InjectionRequest,
    MethodModel,
    Preparation,
)

__all__ = [
    "acceleration_factor",
    "degraded_percent",
    "ShelfLife",
    "fit_shelf_life",
    "StabilityOutput",
    "run_stability",
]

_GAS_CONSTANT = 8.314  # J / mol / K

#: One-sided t critical values at 95%, indexed by degrees of freedom. Written out
#: because the project keeps four runtime dependencies and this is the only
#: distribution it needs. Beyond the table the normal value is close enough that
#: the difference is far smaller than the scatter being fitted.
_T95 = {
    1: 6.314, 2: 2.920, 3: 2.353, 4: 2.132, 5: 2.015, 6: 1.943, 7: 1.895,
    8: 1.860, 9: 1.833, 10: 1.812, 11: 1.796, 12: 1.782, 13: 1.771, 14: 1.761,
    15: 1.753, 16: 1.746, 17: 1.740, 18: 1.734, 19: 1.729, 20: 1.725,
    22: 1.717, 24: 1.711, 26: 1.706, 28: 1.701, 30: 1.697,
}


def _t_critical(df: int) -> float:
    if df <= 0:
        return _T95[1]
    if df in _T95:
        return _T95[df]
    known = [k for k in _T95 if k <= df]
    return _T95[max(known)] if known and df < 30 else 1.645


def acceleration_factor(condition: StorageCondition, kinetics) -> float:
    """How much faster degradation runs at this condition than at the reference.

    Arrhenius in temperature, a power law in relative humidity. At 40 C / 75% RH
    against 25 C / 60% RH with an activation energy of 88 kJ/mol this is about
    7.4, which is the order real accelerated studies report -- and it is why six
    months at the accelerated condition is treated as informative about two years
    at the long-term one.
    """
    energy = kinetics.activation_energy_kj_mol * 1000.0
    temperature = condition.temperature_c + 273.15
    reference = kinetics.reference_temperature_c + 273.15
    arrhenius = math.exp(-(energy / _GAS_CONSTANT) * (1.0 / temperature - 1.0 / reference))
    humidity = (condition.humidity_pct / kinetics.reference_humidity_pct) ** (
        kinetics.humidity_exponent
    )
    return arrhenius * humidity


def degraded_percent(months: float, condition: StorageCondition, kinetics) -> float:
    """Percent of label claim degraded after ``months`` at ``condition``."""
    if months <= 0.0:
        return 0.0
    return kinetics.reference_rate_percent_per_month * acceleration_factor(
        condition, kinetics
    ) * months


@dataclass(frozen=True, slots=True)
class ShelfLife:
    """A fitted shelf life and the regression it came from."""

    attribute: str
    months: float
    #: Where the confidence bound met the limit, before rounding.
    intersection_months: float
    slope_per_month: float
    intercept: float
    residual_sd: float
    points: int
    limit: float
    #: True when the bound never meets the limit inside the maximum studied.
    limited_by_study_length: bool

    def render(self) -> str:
        return (
            f"{self.attribute:<18} {self.months:>5.0f} months  "
            f"(bound met the {self.limit:g} limit at {self.intersection_months:.1f}, "
            f"slope {self.slope_per_month:+.5f}/month, n={self.points})"
        )


def fit_shelf_life(
    points: list[tuple[float, float]],
    *,
    attribute: str,
    limit: float,
    upper: bool,
    rules,
) -> ShelfLife:
    """ICH Q1E shelf life: where the confidence bound meets the limit.

    The bound, not the mean line. A regression fitted through scattered points
    predicts the mean, and half the batches will be worse than it -- so a shelf
    life set where the mean line crosses the limit is one that half the product
    fails. The one-sided 95% bound is the standard answer and it is always
    shorter.
    """
    count = len(points)
    if count < 3:
        return ShelfLife(attribute, 0.0, 0.0, 0.0, 0.0, 0.0, count, limit, False)

    mean_x = stats.fmean(x for x, _ in points)
    mean_y = stats.fmean(y for _, y in points)
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    slope = sxy / sxx if sxx else 0.0
    intercept = mean_y - slope * mean_x
    residuals = [y - (slope * x + intercept) for x, y in points]
    residual_sd = math.sqrt(sum(r * r for r in residuals) / (count - 2))
    critical = _t_critical(count - 2)

    def bound(month: float) -> float:
        spread = residual_sd * math.sqrt(1.0 / count + (month - mean_x) ** 2 / sxx) if sxx else 0.0
        centre = slope * month + intercept
        return centre + critical * spread if upper else centre - critical * spread

    maximum = rules.maximum_months
    intersection = maximum
    exceeded = False
    step = 0.25
    month = 0.0
    while month <= maximum:
        value = bound(month)
        if (upper and value > limit) or (not upper and value < limit):
            intersection = month
            exceeded = True
            break
        month += step

    step_size = rules.round_down_to_months
    months = math.floor(intersection / step_size) * step_size
    months = min(months, maximum)
    return ShelfLife(
        attribute=attribute,
        months=float(months),
        intersection_months=float(intersection),
        slope_per_month=slope,
        intercept=intercept,
        residual_sd=residual_sd,
        points=count,
        limit=limit,
        limited_by_study_length=not exceeded,
    )


@dataclass
class StabilityOutput:
    """Everything a stability programme produced."""

    protocol_id: str
    samples: list[dict] = field(default_factory=list)
    tests: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    certificates: list[dict] = field(default_factory=list)
    out_of_specification: list[dict] = field(default_factory=list)
    trend: list[dict] = field(default_factory=list)
    shelf_lives: list[ShelfLife] = field(default_factory=list)
    significant_change: list[dict] = field(default_factory=list)
    #: Injections, so the trend is traceable to chromatograms.
    injections: list[dict] = field(default_factory=list)
    peaks: list[dict] = field(default_factory=list)

    @property
    def shelf_life_months(self) -> float:
        """The limiting attribute decides. A product is only as stable as its
        worst-behaving specification, not its average one."""
        if not self.shelf_lives:
            return 0.0
        return min(life.months for life in self.shelf_lives)

    @property
    def limiting_attribute(self) -> str:
        if not self.shelf_lives:
            return ""
        return min(self.shelf_lives, key=lambda life: life.months).attribute

    def summary(self) -> str:
        lines = [
            f"{self.protocol_id}",
            f"  samples {len(self.samples)}  tests {len(self.tests)}"
            f"  results {len(self.results)}  injections {len(self.injections)}"
            f"  OOS {len(self.out_of_specification)}",
        ]
        for change in self.significant_change:
            lines.append(
                f"  significant change at {change['condition_id']} "
                f"{change['months']:g} months: {change['reason']}"
            )
        for life in self.shelf_lives:
            lines.append(f"  {life.render()}")
        lines.append(
            f"  shelf life {self.shelf_life_months:.0f} months, "
            f"limited by {self.limiting_attribute}"
        )
        return "\n".join(lines)


def run_stability(
    config: LabConfig,
    protocol: StabilityProtocol,
    batches: list[tuple[str, date]],
    rngs: RngRegistry,
    ids: IdFactory,
    *,
    keep_traces: bool = False,
) -> StabilityOutput:
    """Execute a stability protocol against real batches.

    Args:
        batches: ``(batch_id, manufactured_on)`` for the primary batches. ICH
            requires three, and they are the plant's rather than invented here --
            a stability programme is run on product that exists.
    """
    stability = config.stability
    method = config.methods.by_id(protocol.method_id)
    if method is None:
        raise KeyError(f"unknown method {protocol.method_id}")
    kinetics = stability.kinetics
    out = StabilityOutput(protocol_id=protocol.protocol_id)
    model = MethodModel(method, config, rngs)
    assay_id = method.assay_analyte.analyte_id
    standard = method.standard_concentration_ug_ml

    accelerated = next(
        (c for c in stability.conditions if c.condition_id == "ACCELERATED"), None
    )
    # Which conditions are actually run. The intermediate condition is only
    # tested if the accelerated one shows significant change, which is what ICH
    # Q1A asks for rather than testing everything regardless.
    significant = False
    if accelerated is not None:
        worst = _attributes_at(
            max(accelerated.timepoints_months), accelerated, stability
        )
        significant = (
            worst["total_impurities"] > stability.specification.total_impurities.upper
            or (100.0 - worst["assay"]) >= stability.significant_change.assay_change_percent
        )
        if significant:
            out.significant_change.append(
                {
                    "condition_id": accelerated.condition_id,
                    "months": max(accelerated.timepoints_months),
                    "reason": f"total impurities reach "
                              f"{worst['total_impurities']:.2f}% against a "
                              f"{stability.specification.total_impurities.upper:g}% limit",
                    "triggers": "INTERMEDIATE condition testing per ICH Q1A(R2) 2.2.7",
                }
            )

    conditions = [
        condition
        for condition in stability.conditions
        if condition.condition_id != "INTERMEDIATE" or significant
    ]

    fit_points: dict[str, list[tuple[float, float]]] = {}

    for batch_index, (batch_id, made_on) in enumerate(batches[: protocol.batches], start=1):
        for condition in conditions:
            for months in condition.timepoints_months:
                pulled = made_on + timedelta(days=int(round(months * 30.44)))
                truth = _attributes_at(months, condition, stability)

                sample_id = ids.next("STS", width=6)
                analyst = protocol.analysts[
                    (batch_index + int(months)) % len(protocol.analysts)
                ]
                instrument_id = protocol.instruments[
                    (batch_index + int(months)) % len(protocol.instruments)
                ]
                out.samples.append(
                    {
                        "sample_id": sample_id,
                        "protocol_id": protocol.protocol_id,
                        "batch_id": batch_id,
                        "product_id": protocol.product_id,
                        "condition_id": condition.condition_id,
                        "condition_label": condition.label,
                        "timepoint_months": months,
                        "pulled_on": pulled.isoformat(),
                        "package": protocol.package,
                        "orientation": protocol.orientations[0],
                        "status": "TESTED",
                        "analyst_id": analyst,
                        "instrument_id": instrument_id,
                    }
                )

                measured = _test_sample(
                    out, config, method, model, protocol, sample_id, batch_id,
                    condition, months, truth, analyst, instrument_id, pulled,
                    standard, assay_id, rngs, ids, keep_traces,
                )

                for attribute, value in measured.items():
                    out.trend.append(
                        {
                            "protocol_id": protocol.protocol_id,
                            "batch_id": batch_id,
                            "condition_id": condition.condition_id,
                            "timepoint_months": months,
                            "attribute": attribute,
                            "value": round(value, 4),
                        }
                    )
                    if condition.condition_id == stability.shelf_life.fit_condition:
                        fit_points.setdefault(attribute, []).append((months, value))

                _evaluate(out, stability, protocol, sample_id, batch_id, condition,
                          months, measured, analyst, pulled, ids)

    # Shelf life, fitted per attribute that has a limit in the specification.
    rules = stability.shelf_life
    for attribute, limit, upper in (
        ("assay", stability.specification.assay.lower, False),
        ("total_impurities", stability.specification.total_impurities.upper, True),
        ("individual_impurity_max", stability.specification.individual_impurity.upper, True),
    ):
        points = fit_points.get(attribute)
        if not points or limit is None:
            continue
        out.shelf_lives.append(
            fit_shelf_life(points, attribute=attribute, limit=limit, upper=upper, rules=rules)
        )

    return out


def _attributes_at(months: float, condition: StorageCondition, stability) -> dict[str, float]:
    """True attribute values at a timepoint, before anybody measures them."""
    kinetics = stability.kinetics
    degraded = degraded_percent(months, condition, kinetics)
    levels = {
        analyte_id: level + kinetics.degradant_split.get(analyte_id, 0.0) * degraded
        for analyte_id, level in kinetics.release_levels_percent.items()
    }
    acceleration = acceleration_factor(condition, kinetics)
    secondary = stability.secondary_attributes
    return {
        "assay": 100.0 - degraded,
        "total_impurities": sum(levels.values()),
        "individual_impurity_max": max(levels.values()),
        "levels": levels,  # type: ignore[dict-item]
        "dissolution": secondary["dissolution"].release_value
        + secondary["dissolution"].change_per_month_at_reference * months * acceleration,
        "water_content": secondary["water_content"].release_value
        + secondary["water_content"].change_per_month_at_reference * months * acceleration,
    }


def _test_sample(
    out, config, method, model, protocol, sample_id, batch_id, condition, months,
    truth, analyst_id, instrument_id, pulled, standard, assay_id, rngs, ids, keep_traces,
) -> dict[str, float]:
    """Inject the sample and read the attributes back off the chromatograms."""
    instrument = config.instruments.instrument(instrument_id)
    analyst = config.instruments.analyst(analyst_id)
    column = config.instruments.columns[0]
    assert instrument is not None and analyst is not None

    levels: dict[str, float] = truth["levels"]  # type: ignore[assignment]
    concentrations = {assay_id: standard * truth["assay"] / 100.0}
    for analyte_id, percent in levels.items():
        concentrations[analyte_id] = standard * percent / 100.0

    # A standard, injected in the same sequence as the samples. Assay has to be
    # measured against it rather than against the method's nominal response
    # factor: detector response drifts, and over a three-year study that drift
    # is far larger than the degradation being measured. Referencing a nominal
    # factor produced an assay trend that rose with time -- the product appearing
    # to gain active as it aged -- which is the classic artefact external
    # standardisation exists to remove. Impurities are already reported as area
    # percent of the main peak, so they were never affected, and that asymmetry
    # is what made the cause obvious.
    standard_preparation = Preparation(
        preparation_id=f"PREP-{sample_id}-STD",
        sample_id=f"STD-{sample_id}",
        concentrations={assay_id: standard},
        purpose="STANDARD",
    )
    standard_areas: list[float] = []
    for replicate in range(1, 3):
        request = InjectionRequest(
            injection_id=ids.next("INJ", width=6),
            sequence_id=f"SEQ-{sample_id}",
            injection_number=replicate,
            preparation=standard_preparation,
            conditions=method.nominal_conditions,
            instrument=instrument,
            analyst=analyst,
            column=ColumnState(column.column_id, column.injections_at_start + replicate),
            day_index=int(months),
            days_since_calibration=float((pulled - instrument.calibration.last_calibrated).days),
        )
        result = model.inject(request, keep_trace=False)
        out.injections.append(
            {
                "injection_id": result.injection_id,
                "sample_id": sample_id,
                "protocol_id": protocol.protocol_id,
                "batch_id": batch_id,
                "condition_id": condition.condition_id,
                "timepoint_months": months,
                "method_id": method.method_id,
                "instrument_id": instrument_id,
                "analyst_id": analyst_id,
                "injected_at": pulled.isoformat(),
                "peaks_found": len(result.peaks),
                "purpose": "STANDARD",
            }
        )
        area = result.area_of(assay_id)
        if area is not None:
            standard_areas.append(area)
    standard_area = stats.fmean(standard_areas) if standard_areas else 0.0

    assays: list[float] = []
    impurity_totals: list[float] = []
    impurity_worst: list[float] = []

    for replicate in range(1, protocol.replicates_per_pull + 1):
        preparation = Preparation(
            preparation_id=f"PREP-{sample_id}-{replicate}",
            sample_id=sample_id,
            concentrations=concentrations,
            purpose="STABILITY",
        )
        request = InjectionRequest(
            injection_id=ids.next("INJ", width=6),
            sequence_id=f"SEQ-{sample_id}",
            injection_number=replicate,
            preparation=preparation,
            conditions=method.nominal_conditions,
            instrument=instrument,
            analyst=analyst,
            column=ColumnState(column.column_id, column.injections_at_start + replicate),
            day_index=int(months),
            days_since_calibration=float((pulled - instrument.calibration.last_calibrated).days),
        )
        result = model.inject(request, keep_trace=keep_traces)

        out.injections.append(
            {
                "injection_id": result.injection_id,
                "sample_id": sample_id,
                "protocol_id": protocol.protocol_id,
                "batch_id": batch_id,
                "condition_id": condition.condition_id,
                "timepoint_months": months,
                "method_id": method.method_id,
                "instrument_id": instrument_id,
                "analyst_id": analyst_id,
                "injected_at": pulled.isoformat(),
                "peaks_found": len(result.peaks),
                "purpose": "STABILITY",
            }
        )
        for peak in result.peaks:
            out.peaks.append(
                {
                    "peak_id": ids.next("PK", width=8),
                    "injection_id": result.injection_id,
                    "analyte_id": peak.analyte_id,
                    "retention_time_min": round(peak.retention_time_min, 5),
                    "area": round(peak.area, 4),
                    "signal_to_noise": (
                        None if peak.signal_to_noise is None
                        else round(peak.signal_to_noise, 2)
                    ),
                }
            )

        # Percent of label claim, and impurities as a percentage of the main
        # peak. Both are ratios of measured areas, so they carry the method's
        # error rather than the truth's.
        main = result.area_of(assay_id)
        if main is None or main <= 0.0 or standard_area <= 0.0:
            continue
        analyte = method.analyte(assay_id)
        assert analyte is not None
        assays.append(100.0 * main / standard_area)

        impurities: list[float] = []
        for peak in result.peaks:
            if peak.analyte_id in {None, assay_id}:
                continue
            other = method.analyte(peak.analyte_id)
            if other is None:
                continue
            # Area percent, corrected by relative response as the method states.
            impurities.append(
                100.0 * peak.area / main * (analyte.response_factor / other.response_factor)
            )
        impurity_totals.append(sum(impurities))
        impurity_worst.append(max(impurities) if impurities else 0.0)

    measured = {
        "assay": stats.fmean(assays) if assays else 0.0,
        "total_impurities": stats.fmean(impurity_totals) if impurity_totals else 0.0,
        "individual_impurity_max": stats.fmean(impurity_worst) if impurity_worst else 0.0,
        "dissolution": truth["dissolution"] + rngs.child(
            "lab", "stability", "dissolution", sample_id
        ).gauss(0.0, 1.6),
        "water_content": truth["water_content"] + rngs.child(
            "lab", "stability", "water", sample_id
        ).gauss(0.0, 0.06),
    }

    for attribute, value in measured.items():
        test_id = ids.next("STT", width=7)
        out.tests.append(
            {
                "test_id": test_id,
                "sample_id": sample_id,
                "attribute": attribute,
                "method_id": method.method_id if attribute in {
                    "assay", "total_impurities", "individual_impurity_max"
                } else "",
                "analyst_id": analyst_id,
                "instrument_id": instrument_id,
                "tested_on": pulled.isoformat(),
            }
        )
        out.results.append(
            {
                "result_id": ids.next("STR", width=7),
                "test_id": test_id,
                "sample_id": sample_id,
                "attribute": attribute,
                "value": round(value, 4),
                "unit": "%" if attribute != "water_content" else "% w/w",
            }
        )
    return measured


def _evaluate(
    out, stability, protocol, sample_id, batch_id, condition, months, measured,
    analyst_id, pulled, ids,
) -> None:
    """Second-person review, a certificate, and an investigation where needed."""
    spec = stability.specification
    breaches: list[str] = []
    if not (spec.assay.lower <= measured["assay"] <= spec.assay.upper):
        breaches.append(f"assay {measured['assay']:.2f}% outside "
                        f"{spec.assay.lower:g}-{spec.assay.upper:g}%")
    if measured["total_impurities"] > spec.total_impurities.upper:
        breaches.append(f"total impurities {measured['total_impurities']:.3f}% above "
                        f"{spec.total_impurities.upper:g}%")
    if measured["individual_impurity_max"] > spec.individual_impurity.upper:
        breaches.append(f"largest individual impurity "
                        f"{measured['individual_impurity_max']:.3f}% above "
                        f"{spec.individual_impurity.upper:g}%")

    reviewer = next(
        (a for a in protocol.analysts if a != analyst_id), analyst_id
    )
    out.reviews.append(
        {
            "review_id": ids.next("REV", width=6),
            "sample_id": sample_id,
            "performed_by": analyst_id,
            "reviewed_by": reviewer,
            "reviewed_on": (pulled + timedelta(days=3)).isoformat(),
            "outcome": "APPROVED" if not breaches else "REFERRED_TO_INVESTIGATION",
        }
    )
    out.certificates.append(
        {
            "certificate_id": ids.next("COA", width=6),
            "sample_id": sample_id,
            "batch_id": batch_id,
            "condition_id": condition.condition_id,
            "timepoint_months": months,
            "conclusion": "COMPLIES" if not breaches else "DOES NOT COMPLY",
            "issued_on": (pulled + timedelta(days=4)).isoformat(),
        }
    )

    if breaches and stability.shelf_life.investigate_out_of_specification:
        # A result outside specification is investigated, not simply used to
        # truncate the shelf life. Phase I asks whether the laboratory made an
        # error; only if it did not does the result stand as a product failure.
        oos_id = ids.next("OOS", width=5)
        out.out_of_specification.append(
            {
                "oos_id": oos_id,
                "sample_id": sample_id,
                "batch_id": batch_id,
                "condition_id": condition.condition_id,
                "timepoint_months": months,
                "finding": "; ".join(breaches),
                "phase_1_conclusion": "No laboratory error identified",
                "phase_2_conclusion": "Confirmed product-related; attributable to "
                                      "degradation at the storage condition",
                "outcome": "CONFIRMED",
                "opened_on": pulled.isoformat(),
                "closed_on": (pulled + timedelta(days=21)).isoformat(),
            }
        )
