"""RECIST 1.1 derivation.

Response is arithmetic over measured lesion diameters, so it can be checked
against the guideline directly. These tests are written around the three places
implementations go wrong: comparing progression to baseline rather than the
nadir, omitting the absolute-increase floor, and treating complete response as
"the sum reached zero".

Reference: Eisenhauer et al., Eur J Cancer 2009;45:228-247.
"""

from __future__ import annotations

import pytest

from pharma_sim.clinical.recist import (
    Assessment,
    LesionMeasurement,
    RecistRules,
    Timepoint,
    best_overall_response,
    evaluate_course,
    overall_response,
    sum_of_diameters,
    target_response,
)

RULES = RecistRules()


def lesion(diameter, *, nodal=False, absent=False, ne=False, lesion_id="L1", organ="LUNG"):
    return LesionMeasurement(
        lesion_id=lesion_id,
        organ=organ,
        nodal=nodal,
        diameter_mm=diameter,
        absent=absent,
        not_evaluable=ne,
    )


class TestSumOfDiameters:
    def test_mixes_long_axis_and_nodal_short_axis(self):
        """The sum deliberately adds unlike quantities: longest diameter for a
        mass, short axis for a node."""
        measurements = (lesion(30.0), lesion(18.0, nodal=True, lesion_id="L2"))
        assert sum_of_diameters(measurements) == 48.0

    def test_an_absent_lesion_contributes_nothing(self):
        assert sum_of_diameters((lesion(30.0), lesion(0.0, absent=True, lesion_id="L2"))) == 30.0


class TestPartialResponse:
    def test_exactly_thirty_percent_is_a_partial_response(self):
        """The threshold is inclusive: 'at least a 30% decrease'."""
        outcome = target_response((lesion(70.0),), baseline_sum_mm=100.0,
                                  nadir_sum_mm=100.0, rules=RULES)
        assert outcome.response == "PR"

    def test_just_under_thirty_percent_is_stable(self):
        outcome = target_response((lesion(70.1),), baseline_sum_mm=100.0,
                                  nadir_sum_mm=100.0, rules=RULES)
        assert outcome.response == "SD"

    def test_measured_against_baseline_not_the_nadir(self):
        """A subject who has regrown from a deep response is not a PR again just
        because they are above their nadir."""
        outcome = target_response((lesion(65.0),), baseline_sum_mm=100.0,
                                  nadir_sum_mm=50.0, rules=RULES)
        assert outcome.change_from_baseline == pytest.approx(-0.35)
        # 65 vs nadir 50 is +30%, and 15 mm absolute, so progression wins.
        assert outcome.response == "PD"


class TestProgression:
    def test_twenty_percent_from_nadir_with_enough_absolute_increase(self):
        outcome = target_response((lesion(60.0),), baseline_sum_mm=100.0,
                                  nadir_sum_mm=50.0, rules=RULES)
        assert outcome.response == "PD"
        assert outcome.change_from_nadir == pytest.approx(0.20)
        assert outcome.absolute_change_from_nadir_mm == pytest.approx(10.0)

    def test_relative_rise_without_five_millimetres_is_not_progression(self):
        """A 20% rise on a 12 mm sum is 2.4 mm, inside measurement error. This is
        the floor that stops small lesions progressing on noise alone."""
        outcome = target_response((lesion(14.4),), baseline_sum_mm=20.0,
                                  nadir_sum_mm=12.0, rules=RULES)
        assert outcome.change_from_nadir == pytest.approx(0.20)
        assert outcome.absolute_change_from_nadir_mm == pytest.approx(2.4)
        assert outcome.response == "SD"

    def test_progression_is_possible_while_still_below_baseline(self):
        """The single most important case.

        Baseline 100, shrank to 50 (a partial response), regrown to 61. That is
        +22% and +11 mm from the nadir, so it is progression -- even though the
        sum is 39% below where it started. An implementation comparing to
        baseline reports this as a continuing partial response and never
        progresses the subject at all.
        """
        outcome = target_response((lesion(61.0),), baseline_sum_mm=100.0,
                                  nadir_sum_mm=50.0, rules=RULES)
        assert outcome.change_from_baseline == pytest.approx(-0.39)
        assert outcome.response == "PD"


class TestCompleteResponse:
    def test_requires_nodal_targets_below_the_normal_threshold(self):
        """A node cannot disappear, it can only return to normal size."""
        measurements = (
            lesion(0.0, absent=True),
            lesion(11.0, nodal=True, lesion_id="N1", organ="LYMPH_NODE"),
        )
        assert target_response(measurements, 48.0, 48.0, RULES).response != "CR"

    def test_a_normalised_node_is_compatible_with_complete_response(self):
        measurements = (
            lesion(0.0, absent=True),
            lesion(9.0, nodal=True, lesion_id="N1", organ="LYMPH_NODE"),
        )
        assert target_response(measurements, 48.0, 48.0, RULES).response == "CR"

    def test_is_not_merely_a_zero_sum(self):
        """A 9 mm node leaves a non-zero sum and is still a complete response."""
        measurements = (
            lesion(0.0, absent=True),
            lesion(9.0, nodal=True, lesion_id="N1", organ="LYMPH_NODE"),
        )
        outcome = target_response(measurements, 48.0, 48.0, RULES)
        assert outcome.sum_of_diameters_mm == 9.0
        assert outcome.response == "CR"


class TestNotEvaluable:
    def test_an_unassessable_lesion_makes_the_timepoint_not_evaluable(self):
        measurements = (lesion(30.0), lesion(0.0, ne=True, lesion_id="L2"))
        assert target_response(measurements, 60.0, 60.0, RULES).response == "NE"


class TestOverallResponseTable:
    """RECIST 1.1 Table 3."""

    @pytest.mark.parametrize(
        "target,non_target,expected",
        [
            ("CR", "CR", "CR"),
            ("CR", "ABSENT", "CR"),
            # The row that surprises people: target CR with non-target disease
            # still present is a PARTIAL response overall.
            ("CR", "NON_CR_NON_PD", "PR"),
            ("CR", "NE", "PR"),
            ("PR", "NON_CR_NON_PD", "PR"),
            ("PR", "NE", "PR"),
            ("SD", "NON_CR_NON_PD", "SD"),
            ("SD", "NE", "SD"),
            ("NE", "NON_CR_NON_PD", "NE"),
        ],
    )
    def test_matches_the_published_table(self, target, non_target, expected):
        assert overall_response(target, non_target, new_lesion=False) == expected

    def test_a_new_lesion_is_progression_whatever_else_happened(self):
        assert overall_response("CR", "CR", new_lesion=True) == "PD"
        assert overall_response("PR", "ABSENT", new_lesion=True) == "PD"

    def test_non_target_progression_is_progression(self):
        assert overall_response("PR", "PD", new_lesion=False) == "PD"
        assert overall_response("CR", "PD", new_lesion=False) == "PD"

    def test_target_progression_is_progression(self):
        assert overall_response("PD", "CR", new_lesion=False) == "PD"


class TestCourseEvaluation:
    def _course(self, sums, **kwargs):
        baseline = Timepoint(week=0.0, target=(lesion(sums[0]),))
        timepoints = [
            Timepoint(week=6.0 * (index + 1), target=(lesion(value),), **kwargs)
            for index, value in enumerate(sums[1:])
        ]
        return baseline, timepoints

    def test_carries_the_nadir_forward(self):
        """100 -> 60 (PR) -> 50 (PR, new nadir) -> 61 (PD from 50).

        The last timepoint is only progression if the nadir of 50 was retained.
        Against baseline it is a 39% decrease and would read as a partial
        response for ever.
        """
        baseline, timepoints = self._course([100.0, 60.0, 50.0, 61.0])
        course = evaluate_course(timepoints, baseline, RULES, "INV")
        assert [a.response for a in course] == ["PR", "PR", "PD"]
        assert course[-1].target.nadir_mm == pytest.approx(50.0)

    def test_a_missed_assessment_is_not_evaluable_and_does_not_move_the_nadir(self):
        baseline = Timepoint(week=0.0, target=(lesion(100.0),))
        timepoints = [
            Timepoint(week=6.0, target=(lesion(50.0),)),
            Timepoint(week=12.0, missed=True),
            Timepoint(week=18.0, target=(lesion(61.0),)),
        ]
        course = evaluate_course(timepoints, baseline, RULES, "INV")
        assert [a.response for a in course] == ["PR", "NE", "PD"]

    def test_evaluator_is_recorded_on_every_assessment(self):
        baseline, timepoints = self._course([100.0, 80.0])
        for assessment in evaluate_course(timepoints, baseline, RULES, "BICR"):
            assert assessment.evaluator == "BICR"


class TestBestOverallResponse:
    def _assessments(self, pairs):
        from pharma_sim.clinical.recist import TargetOutcome

        return [
            Assessment(
                week=week,
                evaluator="INV",
                response=response,
                target=TargetOutcome(response, 0.0, None, None, None, 0.0),
                non_target="ABSENT",
                new_lesion=False,
            )
            for week, response in pairs
        ]

    def test_takes_the_best_before_progression(self):
        best, week = best_overall_response(
            self._assessments([(6.0, "SD"), (12.0, "PR"), (18.0, "PD")]), RULES
        )
        assert best == "PR"
        assert week == 12.0

    def test_ignores_anything_after_progression(self):
        """A response recorded after progression is not a response to this
        treatment, and counting it would inflate the response rate."""
        best, _ = best_overall_response(
            self._assessments([(6.0, "SD"), (12.0, "PD"), (18.0, "CR")]), RULES
        )
        assert best == "SD"

    def test_stable_disease_needs_the_minimum_interval(self):
        """Stable disease claimed at week 2 is not stable disease."""
        best, _ = best_overall_response(self._assessments([(2.0, "SD")]), RULES)
        assert best == "NE"

    def test_confirmation_when_the_protocol_requires_it(self):
        rules = RecistRules(confirmation_required=True, confirmation_min_weeks=4.0)
        unconfirmed = self._assessments([(6.0, "PR"), (8.0, "SD")])
        assert best_overall_response(unconfirmed, rules)[0] == "SD"

        confirmed = self._assessments([(6.0, "PR"), (12.0, "PR")])
        assert best_overall_response(confirmed, rules)[0] == "PR"
