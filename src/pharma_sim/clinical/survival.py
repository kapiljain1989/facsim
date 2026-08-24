"""Progression-free survival, derived from the assessment record.

PFS is where a tumour dataset usually stops being defensible. The event itself is
easy — progression or death — but most of the work is in the censoring rules, and
those depend on the *pattern of assessments* rather than on the disease. A subject
who progresses after missing two scans in a row is censored at their last
adequate assessment, not counted as an event at the scan that found it, because
nobody knows when in that gap it happened.

Everything here is a pure function of the assessments and the death date, so the
censoring reason on any subject can be reproduced from their visit data. That is
what makes the dataset survive a statistician recomputing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pharma_sim.clinical.recist import Assessment

__all__ = ["PfsOutcome", "CensorReason", "derive_pfs", "median_survival"]

CensorReason = Literal[
    "PROGRESSION",
    "DEATH",
    "NO_BASELINE",
    "NO_POST_BASELINE_ASSESSMENT",
    "MISSED_ASSESSMENTS_BEFORE_EVENT",
    "SUBSEQUENT_ANTICANCER_THERAPY",
    "NO_EVENT_AT_ANALYSIS",
]


@dataclass(frozen=True, slots=True)
class PfsOutcome:
    """One subject's PFS, with the reason it ended that way.

    ``event`` is 1 for progression or death and 0 for a censored observation,
    matching the ``CNSR`` convention in ADaM inverted: ADaM uses 1 for censored,
    so exporters flip it.
    """

    evaluator: str
    event: bool
    week: float
    reason: CensorReason
    #: Week of the last assessment that could be used, for traceability.
    last_adequate_week: float | None


def derive_pfs(
    assessments: list[Assessment],
    *,
    evaluator: str,
    death_week: float | None,
    analysis_week: float,
    subsequent_therapy_week: float | None = None,
    max_consecutive_missed: int = 2,
) -> PfsOutcome:
    """Derive PFS for one subject under one evaluator's reads.

    Args:
        assessments: this evaluator's assessments in chronological order.
        death_week: week of death from any cause, if it occurred.
        analysis_week: data cut-off. Nothing after it is known.
        subsequent_therapy_week: start of a new anticancer therapy, which stops
            the observation because progression after it is not attributable.
        max_consecutive_missed: how many missed assessments in a row invalidate a
            subsequent event. Two is the usual protocol rule.

    The order of these checks is the specification: an earlier rule wins over a
    later one, which is why they read top to bottom rather than as a table.
    """
    within = [a for a in assessments if a.week <= analysis_week]
    adequate = [a for a in within if not a.missed and a.response != "NE"]

    if not within:
        return PfsOutcome(evaluator, False, 0.0, "NO_POST_BASELINE_ASSESSMENT", None)
    if not adequate:
        return PfsOutcome(evaluator, False, 0.0, "NO_POST_BASELINE_ASSESSMENT", None)

    progression = next((a for a in within if a.response == "PD"), None)

    # A subject who died without documented progression has a PFS event at death,
    # provided death is within the window the assessments cover.
    death_within = death_week is not None and death_week <= analysis_week

    # Whichever comes first decides.
    progression_week = progression.week if progression is not None else None
    candidates = [
        (week, kind)
        for week, kind in (
            (progression_week, "PROGRESSION"),
            (death_week if death_within else None, "DEATH"),
        )
        if week is not None
    ]

    if not candidates:
        last = adequate[-1]
        if subsequent_therapy_week is not None and subsequent_therapy_week <= analysis_week:
            usable = [a for a in adequate if a.week <= subsequent_therapy_week]
            week = usable[-1].week if usable else 0.0
            return PfsOutcome(
                evaluator, False, week, "SUBSEQUENT_ANTICANCER_THERAPY", week
            )
        return PfsOutcome(evaluator, False, last.week, "NO_EVENT_AT_ANALYSIS", last.week)

    event_week, kind = min(candidates)

    # New anticancer therapy before the event stops the observation: progression
    # after a treatment change cannot be attributed to the randomised treatment.
    if subsequent_therapy_week is not None and subsequent_therapy_week < event_week:
        usable = [a for a in adequate if a.week <= subsequent_therapy_week]
        week = usable[-1].week if usable else 0.0
        return PfsOutcome(evaluator, False, week, "SUBSEQUENT_ANTICANCER_THERAPY", week)

    # Two or more consecutive missed assessments immediately before the event mean
    # the event date is unknowable, so the subject is censored at the last
    # assessment that was actually done.
    preceding = [a for a in within if a.week < event_week]
    trailing_missed = 0
    for assessment in reversed(preceding):
        if assessment.missed or assessment.response == "NE":
            trailing_missed += 1
        else:
            break
    if trailing_missed >= max_consecutive_missed:
        usable = [a for a in adequate if a.week < event_week]
        week = usable[-1].week if usable else 0.0
        return PfsOutcome(
            evaluator, False, week, "MISSED_ASSESSMENTS_BEFORE_EVENT", week
        )

    last_adequate = next(
        (a.week for a in reversed(adequate) if a.week <= event_week), None
    )
    return PfsOutcome(evaluator, True, event_week, kind, last_adequate)  # type: ignore[arg-type]


def median_survival(outcomes: list[PfsOutcome]) -> float | None:
    """Kaplan-Meier median, in the same units as the outcome weeks.

    Written out rather than imported: the project keeps four runtime
    dependencies, and the estimator is a dozen lines. Returns None when the
    survival curve never reaches 0.5, which is the honest answer for a dataset
    whose follow-up is too short — reporting the last observed time instead would
    understate the median.
    """
    if not outcomes:
        return None
    times = sorted({outcome.week for outcome in outcomes if outcome.event})
    at_risk_total = len(outcomes)
    survival = 1.0
    for time in times:
        at_risk = sum(1 for outcome in outcomes if outcome.week >= time)
        events = sum(1 for outcome in outcomes if outcome.event and outcome.week == time)
        if at_risk == 0:
            continue
        survival *= 1.0 - events / at_risk
        if survival <= 0.5:
            return time
    del at_risk_total
    return None
