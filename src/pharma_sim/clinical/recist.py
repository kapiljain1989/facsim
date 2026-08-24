"""RECIST 1.1 response derivation.

Pure functions over *measured* lesion diameters. Nothing here knows anything
about how the measurements were produced, which is what makes the central
invariant testable: every response record must be reproducible from the tumour
measurement records it was derived from, for any evaluator.

Reference: Eisenhauer et al., "New response evaluation criteria in solid
tumours: revised RECIST guideline (version 1.1)", Eur J Cancer 2009;45:228-247.

Three details account for most incorrect implementations of this, so they are
called out where they happen:

1. **Partial response is measured against baseline; progression against the
   nadir.** A subject who shrinks 40% and then regrows 25% from their smallest
   sum has progressed, even though the sum is still well below where it started.
   Comparing progression to baseline instead of the nadir silently converts real
   progressions into stable disease.
2. **Progression needs both a relative and an absolute increase.** A 20% rise on
   a 12 mm sum is 2.4 mm, which is inside measurement error; RECIST requires at
   least 5 mm as well.
3. **Complete response is not "the sum reached zero".** Nodal target lesions
   never disappear, they return to normal size, so a node still measurable at
   9 mm short axis is compatible with CR while one at 11 mm is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "RecistRules",
    "LesionMeasurement",
    "NonTargetState",
    "Timepoint",
    "TargetOutcome",
    "Response",
    "sum_of_diameters",
    "target_response",
    "overall_response",
    "best_overall_response",
    "evaluate_course",
]

#: RECIST response categories, plus not-evaluable.
Response = Literal["CR", "PR", "SD", "PD", "NE"]

#: Non-target disease is assessed categorically, never measured.
NonTargetState = Literal["CR", "NON_CR_NON_PD", "PD", "NE", "ABSENT"]

#: Preference order for best-overall-response. Lower is better.
_RANK: dict[str, int] = {"CR": 0, "PR": 1, "SD": 2, "PD": 3, "NE": 4}


@dataclass(frozen=True, slots=True)
class RecistRules:
    """The thresholds, declared rather than embedded in the logic."""

    measurable_min_mm: float = 10.0
    nodal_measurable_min_mm: float = 15.0
    nodal_normal_max_mm: float = 10.0
    max_target_lesions: int = 5
    max_target_per_organ: int = 2
    partial_response_decrease: float = 0.30
    progression_increase: float = 0.20
    progression_min_absolute_mm: float = 5.0
    confirmation_required: bool = False
    confirmation_min_weeks: float = 4.0
    stable_disease_min_weeks: float = 6.0


@dataclass(frozen=True, slots=True)
class LesionMeasurement:
    """One target lesion at one timepoint, as a reader recorded it.

    ``diameter_mm`` is the longest diameter for a non-nodal lesion and the short
    axis for a node — the two are not interchangeable and the sum mixes them by
    design.
    """

    lesion_id: str
    organ: str
    nodal: bool
    diameter_mm: float
    #: Recorded as present but below the size a radiologist will measure. RECIST
    #: allows a default of 5 mm to be assigned in that case.
    too_small_to_measure: bool = False
    #: No longer visible.
    absent: bool = False
    #: Present but not assessable, e.g. the scan did not cover this site.
    not_evaluable: bool = False

    @property
    def contribution_mm(self) -> float:
        """What this lesion adds to the sum of diameters."""
        if self.absent:
            return 0.0
        return self.diameter_mm

    @property
    def resolved(self) -> bool:
        """Whether this lesion satisfies the complete-response condition.

        A non-nodal lesion has to be gone. A node only has to be back under the
        normal threshold, because a normal node is still a visible node.
        """
        if self.absent:
            return True
        if self.nodal:
            return self.diameter_mm < 10.0
        return False


@dataclass(frozen=True, slots=True)
class Timepoint:
    """Everything one reader recorded at one assessment."""

    week: float
    target: tuple[LesionMeasurement, ...] = ()
    non_target: NonTargetState = "ABSENT"
    new_lesion: bool = False
    #: The assessment did not happen.
    missed: bool = False

    @property
    def evaluable(self) -> bool:
        if self.missed or not self.target:
            return False
        return not any(lesion.not_evaluable for lesion in self.target)


@dataclass(frozen=True, slots=True)
class TargetOutcome:
    """The target-lesion assessment, with the numbers it came from."""

    response: Response
    sum_of_diameters_mm: float
    change_from_baseline: float | None
    change_from_nadir: float | None
    absolute_change_from_nadir_mm: float | None
    nadir_mm: float


def sum_of_diameters(measurements: tuple[LesionMeasurement, ...] | list[LesionMeasurement]) -> float:
    """Sum of target-lesion diameters.

    Long axis for non-nodal lesions, short axis for nodes, absent lesions
    contributing nothing.
    """
    return sum(measurement.contribution_mm for measurement in measurements)


def _nodal_targets_normalised(
    measurements: tuple[LesionMeasurement, ...], rules: RecistRules
) -> bool:
    for measurement in measurements:
        if not measurement.nodal or measurement.absent:
            continue
        if measurement.diameter_mm >= rules.nodal_normal_max_mm:
            return False
    return True


def target_response(
    current: tuple[LesionMeasurement, ...],
    baseline_sum_mm: float,
    nadir_sum_mm: float,
    rules: RecistRules,
) -> TargetOutcome:
    """Response of the target lesions at one timepoint.

    Args:
        current: this timepoint's target-lesion measurements.
        baseline_sum_mm: the sum at baseline. Partial response is relative to it.
        nadir_sum_mm: the smallest sum recorded so far, *including baseline*.
            Progression is relative to it.
    """
    if not current or any(lesion.not_evaluable for lesion in current):
        return TargetOutcome("NE", sum_of_diameters(current), None, None, None, nadir_sum_mm)

    current_sum = sum_of_diameters(current)
    nadir = min(nadir_sum_mm, current_sum)

    from_baseline = (
        (current_sum - baseline_sum_mm) / baseline_sum_mm if baseline_sum_mm > 0 else None
    )
    from_nadir = (
        (current_sum - nadir_sum_mm) / nadir_sum_mm if nadir_sum_mm > 0 else None
    )
    absolute_from_nadir = current_sum - nadir_sum_mm

    # Complete response: every non-nodal target gone, every nodal target back
    # under the normal threshold.
    non_nodal_gone = all(
        lesion.absent for lesion in current if not lesion.nodal
    )
    if non_nodal_gone and _nodal_targets_normalised(current, rules):
        return TargetOutcome(
            "CR", current_sum, from_baseline, from_nadir, absolute_from_nadir, nadir
        )

    # Progression: BOTH a relative rise from the nadir and a minimum absolute
    # rise. The absolute floor is what stops measurement noise on a small sum
    # from reading as progression.
    if (
        from_nadir is not None
        and from_nadir >= rules.progression_increase
        and absolute_from_nadir >= rules.progression_min_absolute_mm
    ):
        return TargetOutcome(
            "PD", current_sum, from_baseline, from_nadir, absolute_from_nadir, nadir
        )

    # Partial response: relative to BASELINE, not to the nadir.
    if from_baseline is not None and from_baseline <= -rules.partial_response_decrease:
        return TargetOutcome(
            "PR", current_sum, from_baseline, from_nadir, absolute_from_nadir, nadir
        )

    return TargetOutcome(
        "SD", current_sum, from_baseline, from_nadir, absolute_from_nadir, nadir
    )


#: RECIST 1.1 Table 3, for subjects who have target disease at baseline. Keyed by
#: (target response, non-target state) and read only when there is no new lesion,
#: since a new lesion is progression outright.
_OVERALL: dict[tuple[str, str], Response] = {
    ("CR", "CR"): "CR",
    ("CR", "ABSENT"): "CR",
    # A complete response in the target lesions while non-target disease persists
    # is a PARTIAL response overall. This line surprises people, and it is in the
    # guideline.
    ("CR", "NON_CR_NON_PD"): "PR",
    ("CR", "NE"): "PR",
    ("PR", "CR"): "PR",
    ("PR", "ABSENT"): "PR",
    ("PR", "NON_CR_NON_PD"): "PR",
    ("PR", "NE"): "PR",
    ("SD", "CR"): "SD",
    ("SD", "ABSENT"): "SD",
    ("SD", "NON_CR_NON_PD"): "SD",
    ("SD", "NE"): "SD",
    ("NE", "CR"): "NE",
    ("NE", "ABSENT"): "NE",
    ("NE", "NON_CR_NON_PD"): "NE",
    ("NE", "NE"): "NE",
}


def overall_response(
    target: Response, non_target: NonTargetState, new_lesion: bool
) -> Response:
    """Combine the target and non-target assessments into one response.

    Progression anywhere wins: progressive target disease, unequivocal
    progression of non-target disease, or any new lesion.
    """
    if new_lesion or target == "PD" or non_target == "PD":
        return "PD"
    return _OVERALL.get((target, non_target), "NE")


@dataclass(frozen=True, slots=True)
class Assessment:
    """One evaluated timepoint: what was measured and what it means."""

    week: float
    evaluator: str
    response: Response
    target: TargetOutcome
    non_target: NonTargetState
    new_lesion: bool
    missed: bool = False


def evaluate_course(
    timepoints: list[Timepoint],
    baseline: Timepoint,
    rules: RecistRules,
    evaluator: str,
) -> list[Assessment]:
    """Evaluate a whole course of assessments in order.

    The nadir is carried forward, which is the only piece of state RECIST needs
    and the piece a per-timepoint implementation gets wrong.
    """
    baseline_sum = sum_of_diameters(baseline.target)
    nadir = baseline_sum
    assessments: list[Assessment] = []

    for timepoint in timepoints:
        if timepoint.missed:
            assessments.append(
                Assessment(
                    week=timepoint.week,
                    evaluator=evaluator,
                    response="NE",
                    target=TargetOutcome("NE", 0.0, None, None, None, nadir),
                    non_target="NE",
                    new_lesion=False,
                    missed=True,
                )
            )
            continue

        outcome = target_response(timepoint.target, baseline_sum, nadir, rules)
        response = overall_response(outcome.response, timepoint.non_target, timepoint.new_lesion)
        assessments.append(
            Assessment(
                week=timepoint.week,
                evaluator=evaluator,
                response=response,
                target=outcome,
                non_target=timepoint.non_target,
                new_lesion=timepoint.new_lesion,
            )
        )
        # The nadir only ever falls, and only on an evaluable timepoint.
        if outcome.response != "NE":
            nadir = min(nadir, outcome.sum_of_diameters_mm)

    return assessments


def best_overall_response(
    assessments: list[Assessment], rules: RecistRules
) -> tuple[Response, float | None]:
    """Best response recorded up to and including progression.

    Returns ``(response, week_first_achieved)``.

    Assessments after progression do not count, which is why this cannot be a
    simple minimum over the whole list. Where the protocol requires confirmation,
    a complete or partial response is only counted if a later assessment at least
    ``confirmation_min_weeks`` away shows the same or better; and stable disease
    has to have lasted a minimum duration to be claimed at all.
    """
    considered: list[Assessment] = []
    for assessment in assessments:
        considered.append(assessment)
        if assessment.response == "PD":
            break

    if not considered:
        return "NE", None

    best: Response = "NE"
    best_week: float | None = None

    for index, assessment in enumerate(considered):
        candidate = assessment.response
        if candidate in {"CR", "PR"} and rules.confirmation_required:
            later = [
                other
                for other in considered[index + 1 :]
                if other.week - assessment.week >= rules.confirmation_min_weeks
            ]
            if not any(_RANK[other.response] <= _RANK[candidate] for other in later):
                continue
        if candidate == "SD":
            # Stable disease claimed at the first assessment a fortnight in is
            # not stable disease; the guideline requires a minimum interval.
            if assessment.week < rules.stable_disease_min_weeks:
                continue
        if _RANK[candidate] < _RANK[best]:
            best, best_week = candidate, assessment.week

    return best, best_week
