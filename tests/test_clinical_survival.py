"""PFS derivation, and the invariant the whole slice exists to establish.

The censoring rules are most of the work and all of the risk. Each one is tested
directly, because a dataset whose censoring cannot be reproduced from the visit
data is a dataset a statistician will reject.

The last class is the important one: it takes a cohort, writes the measurements
out as flat records the way an export would, then reconstructs the response from
*only those records* and requires it to match. That is what makes response
arithmetic over lesion data rather than a label with numbers next to it.
"""

from __future__ import annotations

from pathlib import Path
from random import Random

import pytest

from pharma_sim.clinical.lesion import (
    assessment_weeks,
    build_tumour,
    measure,
    rules_from_config,
    select_targets,
)
from pharma_sim.clinical.loader import load_clinical_config
from pharma_sim.clinical.recist import (
    Assessment,
    LesionMeasurement,
    TargetOutcome,
    Timepoint,
    best_overall_response,
    evaluate_course,
)
from pharma_sim.clinical.survival import derive_pfs, median_survival

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "clinical"


@pytest.fixture(scope="module")
def tumour_config():
    return load_clinical_config(CONFIG_DIR).tumour


def _assessments(pairs):
    return [
        Assessment(
            week=week,
            evaluator="INV",
            response=response,
            target=TargetOutcome(response, 0.0, None, None, None, 0.0),
            non_target="ABSENT",
            new_lesion=False,
            missed=(response == "NE"),
        )
        for week, response in pairs
    ]


class TestPfsEvents:
    def test_progression_is_an_event_at_the_assessment_that_found_it(self):
        outcome = derive_pfs(
            _assessments([(6.0, "SD"), (12.0, "PD")]),
            evaluator="INV", death_week=None, analysis_week=52.0,
        )
        assert outcome.event and outcome.week == 12.0 and outcome.reason == "PROGRESSION"

    def test_death_without_progression_is_an_event(self):
        outcome = derive_pfs(
            _assessments([(6.0, "SD"), (12.0, "SD")]),
            evaluator="INV", death_week=15.0, analysis_week=52.0,
        )
        assert outcome.event and outcome.week == 15.0 and outcome.reason == "DEATH"

    def test_whichever_comes_first_wins(self):
        outcome = derive_pfs(
            _assessments([(6.0, "SD"), (12.0, "PD")]),
            evaluator="INV", death_week=20.0, analysis_week=52.0,
        )
        assert outcome.reason == "PROGRESSION" and outcome.week == 12.0

    def test_death_after_the_cut_off_is_not_known(self):
        outcome = derive_pfs(
            _assessments([(6.0, "SD")]),
            evaluator="INV", death_week=60.0, analysis_week=52.0,
        )
        assert not outcome.event and outcome.reason == "NO_EVENT_AT_ANALYSIS"


class TestPfsCensoring:
    def test_no_evaluable_assessment_is_censored_at_randomisation(self):
        outcome = derive_pfs(
            _assessments([(6.0, "NE")]),
            evaluator="INV", death_week=None, analysis_week=52.0,
        )
        assert not outcome.event
        assert outcome.week == 0.0
        assert outcome.reason == "NO_POST_BASELINE_ASSESSMENT"

    def test_two_missed_assessments_before_progression_censors_at_the_last_good_one(self):
        """Nobody knows when progression happened inside a gap that long, so the
        event cannot be dated and the subject is censored instead."""
        outcome = derive_pfs(
            _assessments([(6.0, "SD"), (12.0, "NE"), (18.0, "NE"), (24.0, "PD")]),
            evaluator="INV", death_week=None, analysis_week=52.0,
        )
        assert not outcome.event
        assert outcome.week == 6.0
        assert outcome.reason == "MISSED_ASSESSMENTS_BEFORE_EVENT"

    def test_a_single_missed_assessment_does_not_invalidate_the_event(self):
        outcome = derive_pfs(
            _assessments([(6.0, "SD"), (12.0, "NE"), (18.0, "PD")]),
            evaluator="INV", death_week=None, analysis_week=52.0,
        )
        assert outcome.event and outcome.week == 18.0

    def test_new_therapy_before_progression_stops_the_observation(self):
        """Progression after a treatment change cannot be attributed to the
        randomised treatment."""
        outcome = derive_pfs(
            _assessments([(6.0, "SD"), (12.0, "SD"), (18.0, "PD")]),
            evaluator="INV", death_week=None, analysis_week=52.0,
            subsequent_therapy_week=14.0,
        )
        assert not outcome.event
        assert outcome.week == 12.0
        assert outcome.reason == "SUBSEQUENT_ANTICANCER_THERAPY"

    def test_no_event_is_censored_at_the_last_adequate_assessment(self):
        outcome = derive_pfs(
            _assessments([(6.0, "PR"), (12.0, "PR"), (18.0, "SD")]),
            evaluator="INV", death_week=None, analysis_week=52.0,
        )
        assert not outcome.event
        assert outcome.week == 18.0
        assert outcome.reason == "NO_EVENT_AT_ANALYSIS"

    def test_assessments_after_the_cut_off_are_ignored(self):
        outcome = derive_pfs(
            _assessments([(6.0, "SD"), (60.0, "PD")]),
            evaluator="INV", death_week=None, analysis_week=52.0,
        )
        assert not outcome.event and outcome.week == 6.0


class TestMedianSurvival:
    def test_simple_case(self):
        outcomes = [
            derive_pfs(_assessments([(week, "PD")]), evaluator="INV",
                       death_week=None, analysis_week=100.0)
            for week in (10.0, 20.0, 30.0, 40.0, 50.0)
        ]
        assert median_survival(outcomes) == 30.0

    def test_returns_none_when_the_curve_never_reaches_one_half(self):
        """The honest answer for follow-up that is too short. Reporting the last
        observed time instead would understate the median."""
        outcomes = [
            derive_pfs(_assessments([(6.0, "SD")]), evaluator="INV",
                       death_week=None, analysis_week=52.0)
            for _ in range(10)
        ]
        assert median_survival(outcomes) is None

    def test_empty_input(self):
        assert median_survival([]) is None


class TestResponseIsReproducibleFromTheRecords:
    """Spine invariant 7 from docs/LIFECYCLE_EXTENSION.md.

    Every response record must be derivable from the tumour measurement records
    it came from, for either evaluator. If it is not, the response column is a
    label rather than a result and nothing downstream of it can be trusted.
    """

    @staticmethod
    def _cohort(tumour_config, subjects=25):
        rules = rules_from_config(tumour_config)
        rows = []
        for index in range(subjects):
            arm = "ARM-A" if index % 3 else "ARM-B"
            subject = f"S-{index:03d}"
            tumour = build_tumour(subject, arm, tumour_config, Random(index))
            weeks = assessment_weeks(tumour_config, Random(index + 500), 104.0)
            for reader in tumour_config.measurement.readers:
                selection = select_targets(
                    tumour, reader, tumour_config, Random(f"{subject}{reader.reader_id}")
                )
                baseline = measure(
                    tumour, selection, 0.0, reader, tumour_config, Random(f"{subject}bl")
                )
                timepoints = [
                    measure(
                        tumour, selection, week, reader, tumour_config,
                        Random(f"{subject}{reader.reader_id}{week}"), missed=missed,
                    )
                    for week, missed in weeks
                ]
                course = evaluate_course(timepoints, baseline, rules, reader.reader_id)
                rows.append((subject, reader.reader_id, baseline, timepoints, course))
        return rules, rows

    @staticmethod
    def _to_records(subject, evaluator, baseline, timepoints):
        """Flatten to the shape an export would write: one row per lesion per
        visit, plus one row per visit for the categorical findings."""
        measurements = []
        visits = []
        for order, timepoint in enumerate([baseline, *timepoints]):
            visits.append(
                {
                    "subject": subject,
                    "evaluator": evaluator,
                    "order": order,
                    "week": timepoint.week,
                    "non_target": timepoint.non_target,
                    "new_lesion": timepoint.new_lesion,
                    "missed": timepoint.missed,
                }
            )
            for lesion in timepoint.target:
                measurements.append(
                    {
                        "subject": subject,
                        "evaluator": evaluator,
                        "order": order,
                        "lesion_id": lesion.lesion_id,
                        "organ": lesion.organ,
                        "nodal": lesion.nodal,
                        "diameter_mm": lesion.diameter_mm,
                        "too_small": lesion.too_small_to_measure,
                        "absent": lesion.absent,
                        "not_evaluable": lesion.not_evaluable,
                    }
                )
        return measurements, visits

    @staticmethod
    def _rebuild(measurements, visits):
        """Reconstruct timepoints from records alone, with no access to the
        objects that produced them."""
        by_order: dict[int, list[dict]] = {}
        for row in measurements:
            by_order.setdefault(row["order"], []).append(row)
        rebuilt = []
        for visit in sorted(visits, key=lambda row: row["order"]):
            lesions = tuple(
                LesionMeasurement(
                    lesion_id=row["lesion_id"],
                    organ=row["organ"],
                    nodal=row["nodal"],
                    diameter_mm=row["diameter_mm"],
                    too_small_to_measure=row["too_small"],
                    absent=row["absent"],
                    not_evaluable=row["not_evaluable"],
                )
                for row in sorted(
                    by_order.get(visit["order"], []), key=lambda row: row["lesion_id"]
                )
            )
            rebuilt.append(
                Timepoint(
                    week=visit["week"],
                    target=lesions,
                    non_target=visit["non_target"],
                    new_lesion=visit["new_lesion"],
                    missed=visit["missed"],
                )
            )
        return rebuilt[0], rebuilt[1:]

    def test_every_response_recomputes_from_the_measurement_records(self, tumour_config):
        rules, rows = self._cohort(tumour_config)
        assert rows, "cohort should not be empty"
        checked = 0
        for subject, evaluator, baseline, timepoints, course in rows:
            measurements, visits = self._to_records(
                subject, evaluator, baseline, timepoints
            )
            rebuilt_baseline, rebuilt_timepoints = self._rebuild(measurements, visits)
            recomputed = evaluate_course(
                rebuilt_timepoints, rebuilt_baseline, rules, evaluator
            )
            assert [a.response for a in recomputed] == [a.response for a in course], (
                f"{subject} / {evaluator}: response is not reproducible from records"
            )
            for original, again in zip(course, recomputed):
                assert again.target.sum_of_diameters_mm == pytest.approx(
                    original.target.sum_of_diameters_mm
                )
                assert again.target.nadir_mm == pytest.approx(original.target.nadir_mm)
            checked += 1
        assert checked >= 40

    def test_best_overall_response_recomputes_too(self, tumour_config):
        rules, rows = self._cohort(tumour_config, subjects=15)
        for subject, evaluator, baseline, timepoints, course in rows:
            measurements, visits = self._to_records(
                subject, evaluator, baseline, timepoints
            )
            rebuilt_baseline, rebuilt_timepoints = self._rebuild(measurements, visits)
            recomputed = evaluate_course(
                rebuilt_timepoints, rebuilt_baseline, rules, evaluator
            )
            assert best_overall_response(recomputed, rules) == best_overall_response(
                course, rules
            )

    def test_pfs_recomputes_from_the_recomputed_course(self, tumour_config):
        """The full chain: records -> response -> PFS event and censoring reason."""
        rules, rows = self._cohort(tumour_config, subjects=15)
        for subject, evaluator, baseline, timepoints, course in rows:
            measurements, visits = self._to_records(
                subject, evaluator, baseline, timepoints
            )
            rebuilt_baseline, rebuilt_timepoints = self._rebuild(measurements, visits)
            recomputed = evaluate_course(
                rebuilt_timepoints, rebuilt_baseline, rules, evaluator
            )
            original = derive_pfs(
                course, evaluator=evaluator, death_week=None, analysis_week=104.0
            )
            again = derive_pfs(
                recomputed, evaluator=evaluator, death_week=None, analysis_week=104.0
            )
            assert (original.event, original.week, original.reason) == (
                again.event,
                again.week,
                again.reason,
            )
