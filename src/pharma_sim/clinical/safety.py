"""Adverse events, CTCAE grading, and the exposure they produce.

The point of this module is not the adverse-event table. It is that the grade
drives the dose: a Grade 3 non-haematological event interrupts dosing and the
subject resumes a level down, so a subject who has a bad time on treatment
receives less drug. Because the investigational arm has more Grade 3 events, its
relative dose intensity comes out lower — and that is a consequence of
``safety.yaml`` and ``dose_modification.yaml`` rather than a separate setting.

Three details that a safety reviewer checks first:

* **Seriousness is not severity.** A Grade 2 event requiring hospitalisation is
  serious; a Grade 3 one managed at home is not. Seriousness is drawn against a
  regulatory criterion, with grade only shifting the probability.
* **Attribution is not arm.** Anaemia and neutropenia come from the
  chemotherapy backbone and appear in both arms at similar rates. A profile where
  every event is higher on the active arm has been scaled rather than modelled.
* **Grade 5 is never drawn from the grade weights.** A fatal event is decided by
  the survival model. Letting an adverse-event table kill subjects would double
  count mortality and break the relationship between death and disease burden.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from random import Random

from pharma_sim.clinical.config import (
    AdverseEventSpec,
    ClinicalConfig,
    DoseModificationConfig,
    DoseRule,
)

__all__ = [
    "AdverseEvent",
    "DoseModification",
    "Exposure",
    "generate_events",
    "apply_dose_modifications",
]


@dataclass(frozen=True, slots=True)
class AdverseEvent:
    """One adverse event, graded and assessed."""

    ae_id: str
    subject_id: str
    pt_code: str
    pt: str
    soc: str
    soc_code: str
    category: str
    attribution: str
    special_interest: str | None
    grade: int
    onset_week: float
    end_week: float
    serious: bool
    seriousness_criterion: str | None
    related: bool
    unexpected: bool
    #: Serious, related and unexpected. The combination that starts a clock.
    susar: bool
    site_awareness_week: float
    sponsor_notified_week: float
    reporting_due_week: float | None
    reported_within_timeline: bool | None

    @property
    def duration_days(self) -> float:
        return (self.end_week - self.onset_week) * 7.0


@dataclass(frozen=True, slots=True)
class DoseModification:
    """A dose action, and the event and rule that caused it."""

    subject_id: str
    ae_id: str
    rule_id: str
    action: str
    reason: str
    week: float
    cycle: int
    dose_before_mg: float
    dose_after_mg: float
    interruption_days: float


@dataclass
class Exposure:
    """A subject's delivered treatment, and how it compares to plan."""

    subject_id: str
    #: Dose in milligrams per cycle number.
    dose_by_cycle: dict[int, float] = field(default_factory=dict)
    #: Days actually dosed in each cycle, after interruptions.
    days_by_cycle: dict[int, float] = field(default_factory=dict)
    modifications: list[DoseModification] = field(default_factory=list)
    discontinued_week: float | None = None
    discontinuation_reason: str | None = None

    def relative_dose_intensity(self, starting_dose: float, cycle_days: float) -> float:
        """Delivered dose over planned dose across the cycles received.

        Both a reduction and an interruption lower it, which is why it is a better
        summary of what a subject actually received than either alone.
        """
        if not self.dose_by_cycle:
            return 0.0
        delivered = sum(
            self.dose_by_cycle[cycle] * self.days_by_cycle.get(cycle, cycle_days)
            for cycle in self.dose_by_cycle
        )
        planned = starting_dose * cycle_days * len(self.dose_by_cycle)
        return delivered / planned if planned else 0.0


def _weighted_grade(weights: dict[int, float], rng: Random) -> int:
    total = sum(weights.values())
    threshold = rng.random() * total
    running = 0.0
    for grade, weight in sorted(weights.items()):
        running += weight
        if threshold <= running:
            return grade
    return max(weights)


def _weighted_choice(options, weight, rng: Random):
    total = sum(weight(option) for option in options)
    threshold = rng.random() * total
    running = 0.0
    for option in options:
        running += weight(option)
        if threshold <= running:
            return option
    return options[-1]


def generate_events(
    subject_id: str,
    arm_id: str,
    weeks_on_treatment: float,
    config: ClinicalConfig,
    rng: Random,
    ids,
) -> list[AdverseEvent]:
    """Draw a subject's adverse events over their time on treatment.

    Incidence is per arm and per event, so the arm difference comes from the
    declared profile rather than from scaling one list.
    """
    safety = config.safety
    reporting = safety.expedited_reporting
    events: list[AdverseEvent] = []

    for spec in safety.adverse_events:
        incidence = spec.incidence.get(arm_id, 0.0)
        if incidence <= 0.0 or rng.random() >= incidence:
            continue

        onset = max(0.1, rng.gauss(spec.onset_weeks.mean, spec.onset_weeks.sd))
        if onset > weeks_on_treatment:
            # It would have happened after the subject came off treatment, so it
            # did not happen on treatment.
            continue
        grade = _weighted_grade(spec.grade_weights, rng)
        duration = max(1.0, rng.gauss(spec.duration_days.mean, spec.duration_days.sd))
        end = onset + duration / 7.0

        serious = rng.random() < safety.seriousness.probability_by_grade.get(grade, 0.0)
        criterion = (
            _weighted_choice(
                safety.seriousness.criteria, lambda option: option.weight, rng
            ).criterion
            if serious
            else None
        )
        # A fatal criterion cannot be drawn here: death belongs to the survival
        # model, so it is replaced with the next most serious thing.
        if criterion == "DEATH":
            criterion = "LIFE_THREATENING"

        related = rng.random() < safety.causality.related_probability.get(
            spec.attribution, 0.0
        )
        unexpected = rng.random() < reporting.unexpected_probability
        susar = serious and related and unexpected

        awareness = onset + max(
            0.0, rng.gauss(reporting.site_awareness_days.mean,
                          reporting.site_awareness_days.sd)
        ) / 7.0
        notified = awareness + max(
            0.0, rng.gauss(reporting.sponsor_notification_days.mean,
                          reporting.sponsor_notification_days.sd)
        ) / 7.0

        due: float | None = None
        on_time: bool | None = None
        if susar:
            # The clock runs from sponsor awareness, and it is shorter for a
            # fatal or life-threatening event.
            window_days = (
                reporting.fatal_or_life_threatening_days
                if criterion == "LIFE_THREATENING"
                else reporting.other_susar_days
            )
            due = notified + window_days / 7.0
            # Reporting happens within the window unless awareness itself was
            # late, which is how a real timeline gets missed.
            on_time = (notified - onset) * 7.0 <= window_days

        events.append(
            AdverseEvent(
                ae_id=ids.next("AE", width=6),
                subject_id=subject_id,
                pt_code=spec.pt_code,
                pt=spec.pt,
                soc=spec.soc,
                soc_code=spec.soc_code,
                category=spec.category,
                attribution=spec.attribution,
                special_interest=spec.special_interest,
                grade=grade,
                onset_week=round(onset, 3),
                end_week=round(end, 3),
                serious=serious,
                seriousness_criterion=criterion,
                related=related,
                unexpected=unexpected,
                susar=susar,
                site_awareness_week=round(awareness, 3),
                sponsor_notified_week=round(notified, 3),
                reporting_due_week=None if due is None else round(due, 3),
                reported_within_timeline=on_time,
            )
        )

    events.sort(key=lambda event: event.onset_week)
    return events


def _matches(rule: DoseRule, event: AdverseEvent, occurrences: int) -> bool:
    trigger = rule.trigger
    if event.grade < trigger.grade_at_least:
        return False
    if trigger.category is not None and event.category != trigger.category:
        return False
    if trigger.special_interest is not None:
        if event.special_interest != trigger.special_interest:
            return False
    if occurrences < trigger.occurrence_at_least:
        return False
    return True


def apply_dose_modifications(
    subject_id: str,
    events: list[AdverseEvent],
    planned_cycles: int,
    cycle_weeks: float,
    config: DoseModificationConfig,
) -> Exposure:
    """Walk the events in time order and work out what the subject received.

    Rules are first-match, and the discontinuations are declared before the
    interruptions so a subject who should come off treatment is not merely
    reduced. The linter checks that ordering, because getting it wrong keeps
    subjects on study who should have stopped and nothing downstream notices.
    """
    exposure = Exposure(subject_id=subject_id)
    dose = config.starting_dose_mg
    # Occurrence counts, so an "on second occurrence" rule can fire.
    seen_interest: dict[str, int] = {}
    seen_grade4: int = 0
    interruption_by_cycle: dict[int, float] = {}
    stopped_at: float | None = None

    for event in events:
        if stopped_at is not None:
            break
        cycle = min(planned_cycles, int(event.onset_week // cycle_weeks) + 1)

        if event.special_interest:
            seen_interest[event.special_interest] = (
                seen_interest.get(event.special_interest, 0) + 1
            )
        if event.grade >= 4 and event.category == "NON_HAEMATOLOGICAL":
            seen_grade4 += 1

        for rule in config.rules:
            if rule.trigger.special_interest:
                occurrences = seen_interest.get(rule.trigger.special_interest, 0)
            elif rule.trigger.grade_at_least >= 4 and rule.trigger.category == (
                "NON_HAEMATOLOGICAL"
            ):
                occurrences = seen_grade4
            else:
                occurrences = 1
            if not _matches(rule, event, occurrences):
                continue

            before = dose
            interruption = 0.0
            if rule.action == "PERMANENT_DISCONTINUATION":
                stopped_at = event.onset_week
                exposure.discontinued_week = round(event.onset_week, 3)
                exposure.discontinuation_reason = rule.reason
                after = 0.0
            else:
                # An interruption lasts as long as the event takes to settle.
                interruption = min(event.duration_days, cycle_weeks * 7.0)
                interruption_by_cycle[cycle] = (
                    interruption_by_cycle.get(cycle, 0.0) + interruption
                )
                if rule.action == "INTERRUPT_AND_REDUCE":
                    lower = config.next_lower(dose)
                    if lower is None and config.discontinue_below_lowest_level:
                        stopped_at = event.onset_week
                        exposure.discontinued_week = round(event.onset_week, 3)
                        exposure.discontinuation_reason = (
                            "Toxicity requiring reduction below the lowest dose level"
                        )
                        after = 0.0
                    else:
                        dose = lower if lower is not None else dose
                        after = dose
                else:
                    after = dose

            exposure.modifications.append(
                DoseModification(
                    subject_id=subject_id,
                    ae_id=event.ae_id,
                    rule_id=rule.rule_id,
                    action=rule.action,
                    reason=rule.reason,
                    week=round(event.onset_week, 3),
                    cycle=cycle,
                    dose_before_mg=before,
                    dose_after_mg=after,
                    interruption_days=round(interruption, 1),
                )
            )
            break  # first match wins

    # Build the delivered schedule. Dose steps down at the cycle the reduction
    # happened in and stays there; a discontinuation truncates the schedule.
    reductions = {
        modification.cycle: modification.dose_after_mg
        for modification in exposure.modifications
        if modification.action in {"INTERRUPT_AND_REDUCE"}
        and modification.dose_after_mg > 0.0
    }
    current = config.starting_dose_mg
    last_cycle = planned_cycles
    if stopped_at is not None:
        last_cycle = min(planned_cycles, int(stopped_at // cycle_weeks) + 1)

    cycle_days = cycle_weeks * 7.0
    for cycle in range(1, last_cycle + 1):
        if cycle in reductions:
            current = reductions[cycle]
        exposure.dose_by_cycle[cycle] = current
        dosed = max(0.0, cycle_days - interruption_by_cycle.get(cycle, 0.0))
        exposure.days_by_cycle[cycle] = round(dosed, 1)

    return exposure
