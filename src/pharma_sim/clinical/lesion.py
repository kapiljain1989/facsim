"""Tumour truth, and what a radiologist records when they look at it.

``tumour.yaml`` declares how disease behaves. This module turns that into a
subject's actual lesions and their trajectory, then lets a reader measure them.
The reader never sees the truth — exactly as the chromatography layer works, and
for the same reason: response has to be a measurement so that two readers can
disagree about it.

Growth follows a biexponential tumour-growth-inhibition model:

    d(t) = d0 * [ p * exp(-ks * t) + (1 - p) * exp(kg * t) ]

with ``p`` the fraction of the lesion sensitive to treatment, ``ks`` its
shrinkage rate and ``kg`` the growth rate of what remains. One equation covers
every trajectory that matters. A high ``p`` with a decent ``ks`` is a responder;
a low ``p`` progresses through treatment; in between produces the shrink-then-
regrow curve that acquired resistance actually looks like. Response rate and
progression-free survival are consequences of these three numbers rather than
separate knobs, which is what makes the two arms differ coherently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from random import Random

from pharma_sim.clinical.config import ReaderConfig, TumourConfig
from pharma_sim.clinical.recist import LesionMeasurement, RecistRules, Timepoint

__all__ = [
    "Lesion",
    "DoseHistory",
    "GrowthParameters",
    "ReaderSelection",
    "SubjectTumour",
    "build_tumour",
    "select_targets",
    "assessment_weeks",
    "measure",
    "rules_from_config",
]


def rules_from_config(config: TumourConfig) -> RecistRules:
    """Lift the declared thresholds into the derivation module's rules."""
    recist = config.recist
    return RecistRules(
        measurable_min_mm=recist.measurable_min_mm,
        nodal_measurable_min_mm=recist.nodal_measurable_min_mm,
        nodal_normal_max_mm=recist.nodal_normal_max_mm,
        max_target_lesions=recist.max_target_lesions,
        max_target_per_organ=recist.max_target_per_organ,
        partial_response_decrease=recist.partial_response_decrease,
        progression_increase=recist.progression_increase,
        progression_min_absolute_mm=recist.progression_min_absolute_mm,
        confirmation_required=recist.confirmation_required,
        confirmation_min_weeks=recist.confirmation_min_weeks,
        stable_disease_min_weeks=recist.stable_disease_min_weeks,
    )


@dataclass(frozen=True, slots=True)
class Lesion:
    """One lesion's true baseline size and site."""

    lesion_id: str
    organ: str
    nodal: bool
    baseline_mm: float
    target: bool
    #: Week it appeared. Zero for lesions present at baseline.
    appeared_week: float = 0.0


@dataclass(frozen=True, slots=True)
class DoseHistory:
    """What fraction of the planned dose a subject was actually taking, over time.

    Segments are ``(start_week, end_week, fraction)``, where the fraction is the
    delivered dose over the planned dose — 1.0 at full dose, 0.5 at half a dose
    level, 0.0 while interrupted or after treatment stopped.
    """

    segments: tuple[tuple[float, float, float], ...] = ()

    def effective_weeks(self, weeks: float) -> float:
        """Dose-weighted time on treatment up to ``weeks``.

        This is the integral of the dose fraction, and it is what the treatment
        effect acts on. A subject at full dose for ten weeks and one at half dose
        for twenty have had the same exposure and should have had the same
        benefit — which is the relationship the whole idea rests on.
        """
        if not self.segments:
            # No history means no modelled interruption, so treatment is assumed
            # continuous at full dose. That keeps a tumour usable on its own.
            return max(0.0, weeks)
        total = 0.0
        for start, end, fraction in self.segments:
            if weeks <= start:
                break
            total += (min(weeks, end) - start) * fraction
        return max(0.0, total)


@dataclass(frozen=True, slots=True)
class GrowthParameters:
    sensitive_fraction: float
    shrinkage_rate_per_week: float
    growth_rate_per_week: float
    #: How much faster the resistant fraction grows off treatment.
    growth_suppression: float = 0.0

    def scale_at(self, weeks: float, dose: DoseHistory | None = None) -> float:
        """Multiplier on baseline size at ``weeks``, never negative.

        The sensitive fraction shrinks with *dose-weighted* time and the
        resistant fraction grows with calendar time, because the disease does not
        pause while a subject is off treatment. That asymmetry is the whole
        exposure-response relationship: interrupt or reduce the dose and the
        shrinkage slows while the growth does not, so the tumour turns around
        sooner and progression arrives earlier.

        Without it, a subject who spent half the study dose-interrupted responded
        exactly as well as one who took every tablet, and relative dose intensity
        predicted nothing.
        """
        if weeks <= 0.0:
            return 1.0
        treated = weeks if dose is None else dose.effective_weeks(weeks)
        untreated = max(0.0, weeks - treated)

        sensitive = self.sensitive_fraction * math.exp(
            -self.shrinkage_rate_per_week * treated
        )
        # Treatment also holds the resistant fraction back, so time off it
        # accelerates growth as well as slowing kill. Both effects are needed:
        # with only the shrinkage term, a reduced dose gives a shallower nadir,
        # and a shallower nadir takes longer to rise 20% above -- so less drug
        # produced *better* progression-free survival, which is backwards.
        resistant = (1.0 - self.sensitive_fraction) * math.exp(
            self.growth_rate_per_week * (weeks + self.growth_suppression * untreated)
        )
        return max(sensitive + resistant, 0.0)


@dataclass(frozen=True, slots=True)
class ReaderSelection:
    """The target lesions one reader chose to follow, and what they left behind.

    Two readers working from the same scans do not have to select the same
    lesions, and in practice they often do not. That divergence -- not
    measurement error on a shared lesion -- is the main reason investigator and
    central assessments disagree, so it is modelled explicitly.
    """

    reader_id: str
    target: tuple[Lesion, ...]
    non_target: tuple[Lesion, ...]
    #: Whether this reader accepts a new finding as malignant. Decided once per
    #: subject, not per timepoint: a reader who calls a new lesion at week 12
    #: does not un-call it at week 18, and rolling the decision at every
    #: assessment would make progression flicker on and off.
    accepts_new_lesion: bool = True


@dataclass(slots=True)
class SubjectTumour:
    """A subject's disease: every measurable lesion, its trajectory, its events."""

    subject_id: str
    arm: str
    growth: GrowthParameters
    #: Every measurable lesion the subject has. Readers select targets from here.
    lesions: list[Lesion] = field(default_factory=list)
    non_target: list[Lesion] = field(default_factory=list)
    #: Week a new lesion appears, if one ever does.
    new_lesion_week: float | None = None
    #: Week non-target disease progresses unequivocally, if it ever does.
    non_target_progression_week: float | None = None
    #: Week of death, if it occurs within the horizon.
    death_week: float | None = None
    #: What the subject was actually taking, over time. Set once the safety and
    #: exposure model has run; absent means continuous full dose.
    dose_history: DoseHistory | None = None

    def true_diameter_mm(self, lesion: Lesion, weeks: float) -> float:
        """True size of one lesion, before anybody measures it."""
        elapsed = max(0.0, weeks - lesion.appeared_week)
        return lesion.baseline_mm * self.growth.scale_at(elapsed, self.dose_history)

    def true_total_sum_mm(self, weeks: float) -> float:
        """True burden across every measurable lesion, whoever selected what.

        The hazards read this rather than any reader's selected sum: a subject's
        disease does not depend on which lesions somebody chose to measure.
        """
        return sum(self.true_diameter_mm(lesion, weeks) for lesion in self.lesions)

    def burden_ratio(self, weeks: float) -> float:
        """Disease burden relative to baseline, which drives the hazards."""
        baseline = self.true_total_sum_mm(0.0)
        if baseline <= 0.0:
            return 1.0
        return self.true_total_sum_mm(weeks) / baseline


def _triangular_int(rng: Random, low: int, high: int, mode: int) -> int:
    if high <= low:
        return low
    return int(round(rng.triangular(low, high, mode)))


def _lognormal(rng: Random, median: float, log_sd: float, low: float, high: float) -> float:
    value = median * math.exp(rng.gauss(0.0, log_sd))
    return min(max(value, low), high)


def _pick_organ(rng: Random, config: TumourConfig) -> tuple[str, bool]:
    """Choose a site by its declared weight.

    No per-organ cap: that is a rule about what a reader may select as a target,
    not about where a subject may have disease.
    """
    available = list(config.organs)
    total = sum(organ.weight for organ in available)
    threshold = rng.random() * total
    running = 0.0
    for organ in available:
        running += organ.weight
        if threshold <= running:
            return organ.organ, organ.nodal
    return available[-1].organ, available[-1].nodal


def _first_event_week(
    rng: Random,
    tumour: SubjectTumour,
    base_hazard: float,
    burden_exponent: float,
    horizon_weeks: float,
    step_weeks: float = 1.0,
) -> float | None:
    """First event time under a burden-dependent hazard.

    Discrete-time thinning in weekly steps rather than an analytic inversion: the
    hazard depends on a burden that has no closed-form integral, and a week is
    finer than any assessment interval so nothing is lost.
    """
    if base_hazard <= 0.0:
        return None
    week = step_weeks
    while week <= horizon_weeks:
        burden = max(tumour.burden_ratio(week), 1e-6)
        hazard = base_hazard * (burden**burden_exponent) * step_weeks
        if rng.random() < min(hazard, 1.0):
            return week
        week += step_weeks
    return None


def build_tumour(
    subject_id: str,
    arm: str,
    config: TumourConfig,
    rng: Random,
    *,
    horizon_weeks: float = 156.0,
) -> SubjectTumour:
    """Create one subject's disease from the declared distributions."""
    arm_growth = config.growth.arms.get(arm)
    if arm_growth is None:
        raise KeyError(f"no growth parameters declared for arm {arm}")

    growth = GrowthParameters(
        sensitive_fraction=min(
            max(rng.gauss(arm_growth.sensitive_fraction.mean, arm_growth.sensitive_fraction.sd), 0.0),
            1.0,
        ),
        shrinkage_rate_per_week=max(
            rng.gauss(
                arm_growth.shrinkage_rate_per_week.mean, arm_growth.shrinkage_rate_per_week.sd
            ),
            0.0,
        ),
        growth_rate_per_week=max(
            rng.gauss(arm_growth.growth_rate_per_week.mean, arm_growth.growth_rate_per_week.sd),
            0.0,
        ),
        growth_suppression=config.growth.growth_suppression,
    )

    tumour = SubjectTumour(subject_id=subject_id, arm=arm, growth=growth)
    baseline = config.baseline

    lesion_count = _triangular_int(
        rng,
        baseline.measurable_lesion_count.minimum,
        baseline.measurable_lesion_count.maximum,
        baseline.measurable_lesion_count.mode,
    )
    for index in range(1, lesion_count + 1):
        # No per-organ cap here: the cap is a rule about what a reader may
        # SELECT, not about where a subject may have disease.
        organ, nodal = _pick_organ(rng, config)
        size = baseline.nodal_short_axis_mm if nodal else baseline.diameter_mm
        tumour.lesions.append(
            Lesion(
                lesion_id=f"{subject_id}-L{index}",
                organ=organ,
                nodal=nodal,
                baseline_mm=_lognormal(rng, size.median, size.log_sd, size.minimum, size.maximum),
                target=False,
            )
        )

    non_target_count = _triangular_int(
        rng,
        baseline.non_target_lesion_count.minimum,
        baseline.non_target_lesion_count.maximum,
        baseline.non_target_lesion_count.mode,
    )
    for index in range(1, non_target_count + 1):
        organ, nodal = _pick_organ(rng, config)
        tumour.non_target.append(
            Lesion(
                lesion_id=f"{subject_id}-NT{index}",
                organ=organ,
                nodal=nodal,
                baseline_mm=0.0,  # never measured
                target=False,
            )
        )

    # Events are drawn after the trajectory exists, because each hazard depends
    # on the burden that trajectory produces.
    tumour.new_lesion_week = _first_event_week(
        rng, tumour, config.new_lesion.base_hazard_per_week,
        config.new_lesion.burden_exponent, horizon_weeks,
    )
    if tumour.non_target:
        tumour.non_target_progression_week = _first_event_week(
            rng, tumour, config.non_target_progression.base_hazard_per_week,
            config.non_target_progression.burden_exponent, horizon_weeks,
        )
    tumour.death_week = _first_event_week(
        rng, tumour, config.death.base_hazard_per_week,
        config.death.burden_exponent, horizon_weeks,
    )
    return tumour


def select_targets(
    tumour: SubjectTumour,
    reader: ReaderConfig,
    config: TumourConfig,
    rng: Random,
) -> ReaderSelection:
    """Choose this reader's target lesions from the subject's measurable disease.

    Readers prefer larger, more reproducibly measurable lesions, and RECIST caps
    them at five in total and two per organ. ``selection_size_preference``
    controls how reliably the larger lesion actually wins: at 1.0 every reader
    ranks purely by size and they all select identically, which would leave
    measurement noise as the only source of disagreement and produce concordance
    far higher than any real trial reports.

    Lesions not selected as targets become non-target disease, which is exactly
    what happens on a real case report form.
    """
    preference = config.measurement.selection_size_preference
    ranked = sorted(
        tumour.lesions,
        key=lambda lesion: -(
            lesion.baseline_mm * (preference + (1.0 - preference) * rng.random() * 2.0)
        ),
    )

    chosen: list[Lesion] = []
    per_organ: dict[str, int] = {}
    for lesion in ranked:
        if len(chosen) >= config.recist.max_target_lesions:
            break
        if per_organ.get(lesion.organ, 0) >= config.recist.max_target_per_organ:
            continue
        per_organ[lesion.organ] = per_organ.get(lesion.organ, 0) + 1
        chosen.append(lesion)

    selected = {lesion.lesion_id for lesion in chosen}
    remainder = tuple(
        lesion for lesion in tumour.lesions if lesion.lesion_id not in selected
    )
    return ReaderSelection(
        reader_id=reader.reader_id,
        target=tuple(chosen),
        non_target=tuple(tumour.non_target) + remainder,
        accepts_new_lesion=rng.random() < config.measurement.new_lesion_concurrence,
    )


def assessment_weeks(
    config: TumourConfig, rng: Random, horizon_weeks: float
) -> list[tuple[float, bool]]:
    """Scheduled tumour assessments as ``(week, missed)``.

    Calendar-driven from randomisation, and independent of the dosing schedule —
    which is what puts an assessment out of window when a cycle is delayed. Scans
    slip by a few days, and some never happen at all.
    """
    schedule = config.assessment_schedule
    weeks: list[tuple[float, bool]] = []
    nominal = schedule.first_week
    while nominal <= horizon_weeks:
        slip = rng.gauss(0.0, schedule.slip_days_sd) / 7.0 if schedule.slip_days_sd else 0.0
        missed = rng.random() < schedule.missed_probability
        weeks.append((round(max(nominal + slip, 0.1), 4), missed))
        interval = (
            schedule.interval_weeks
            if nominal < schedule.switch_week
            else schedule.later_interval_weeks
        )
        nominal += interval
    return weeks


def measure(
    tumour: SubjectTumour,
    selection: ReaderSelection,
    week: float,
    reader: ReaderConfig,
    config: TumourConfig,
    rng: Random,
    *,
    missed: bool = False,
) -> Timepoint:
    """What ``reader`` records for their own selected lesions at ``week``.

    Each lesion is measured independently, so reader error does not cancel across
    the sum — it accumulates, which is why a five-lesion subject shows more
    reader disagreement than a one-lesion subject.
    """
    if missed:
        return Timepoint(week=week, missed=True)

    measurements: list[LesionMeasurement] = []
    for lesion in selection.target:
        truth = tumour.true_diameter_mm(lesion, week)
        observed = truth * (1.0 + reader.bias + rng.gauss(0.0, reader.cv))
        observed = max(observed, 0.0)

        # Radiology reports to the millimetre. Quantisation is a real source of
        # disagreement, not a display detail.
        step = config.measurement.quantisation_mm
        quantised = round(observed / step) * step

        absent = False
        too_small = False
        if not lesion.nodal:
            if quantised < 1.5:
                absent, quantised = True, 0.0
            elif quantised < config.measurement.too_small_to_measure_mm:
                # RECIST allows a default value where a lesion is present but
                # below the size anyone will commit a number to.
                too_small = True
                quantised = config.measurement.too_small_to_measure_mm

        measurements.append(
            LesionMeasurement(
                lesion_id=lesion.lesion_id,
                organ=lesion.organ,
                nodal=lesion.nodal,
                diameter_mm=quantised,
                too_small_to_measure=too_small,
                absent=absent,
            )
        )

    # A new finding is only progression if this reader accepts it as malignant.
    # One reader calling it equivocal while the other calls progression is a
    # documented source of investigator-versus-central discordance. The judgement
    # is the reader's, made once, and carried by the selection.
    new_lesion = (
        selection.accepts_new_lesion
        and tumour.new_lesion_week is not None
        and week >= tumour.new_lesion_week
    )

    if not selection.non_target:
        non_target = "ABSENT"
    elif (
        tumour.non_target_progression_week is not None
        and week >= tumour.non_target_progression_week
    ):
        non_target = "PD"
    else:
        # Non-target disease resolves alongside the target lesions it shares a
        # trajectory with.
        non_target = "CR" if tumour.burden_ratio(week) < 0.10 else "NON_CR_NON_PD"

    return Timepoint(
        week=week,
        target=tuple(measurements),
        non_target=non_target,  # type: ignore[arg-type]
        new_lesion=new_lesion,
    )
