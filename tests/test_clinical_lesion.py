"""Tumour truth, reader selection and measurement.

The interesting claims are about *where disagreement comes from*. Two readers
looking at the same scans disagree mostly because they follow different lesions,
not because they measure a shared lesion differently, and the model has to
reproduce that ordering or the discordance it generates is the wrong shape.
"""

from __future__ import annotations

import math
from pathlib import Path
from random import Random

import pytest

from pharma_sim.clinical.config import ReaderConfig
from pharma_sim.clinical.lesion import (
    GrowthParameters,
    assessment_weeks,
    build_tumour,
    measure,
    rules_from_config,
    select_targets,
)
from pharma_sim.clinical.loader import load_clinical_config
from pharma_sim.clinical.recist import evaluate_course, sum_of_diameters

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "clinical"


@pytest.fixture(scope="module")
def tumour_config():
    return load_clinical_config(CONFIG_DIR).tumour


@pytest.fixture(scope="module")
def readers(tumour_config):
    return tumour_config.measurement.readers


class TestGrowth:
    def test_starts_at_baseline(self):
        growth = GrowthParameters(0.7, 0.05, 0.02)
        assert growth.scale_at(0.0) == pytest.approx(1.0)

    def test_a_sensitive_tumour_shrinks_then_regrows(self):
        """The trajectory acquired resistance actually produces."""
        growth = GrowthParameters(0.80, 0.06, 0.02)
        trajectory = [growth.scale_at(week) for week in range(0, 105, 3)]
        nadir = min(trajectory)
        assert nadir < 0.70, "should reach a partial response"
        assert trajectory[-1] > nadir * 1.20, "should regrow past the progression threshold"

    def test_a_resistant_tumour_only_grows(self):
        growth = GrowthParameters(0.10, 0.05, 0.03)
        trajectory = [growth.scale_at(week) for week in range(0, 60, 3)]
        assert trajectory == sorted(trajectory), "should be monotonically increasing"

    def test_never_negative(self):
        growth = GrowthParameters(1.0, 0.5, 0.0)
        assert growth.scale_at(500.0) >= 0.0


class TestTumourConstruction:
    def test_is_deterministic_for_a_seed(self, tumour_config):
        def build():
            tumour = build_tumour("S-001", "ARM-A", tumour_config, Random("seed"))
            return [(l.lesion_id, l.organ, round(l.baseline_mm, 6)) for l in tumour.lesions]

        assert build() == build()

    def test_every_lesion_is_measurable_at_baseline(self, tumour_config):
        """A target lesion has to be measurable to be selected, so the lesions the
        subject has must all clear the relevant threshold."""
        recist = tumour_config.recist
        for index in range(40):
            tumour = build_tumour(f"S-{index}", "ARM-A", tumour_config, Random(index))
            for lesion in tumour.lesions:
                floor = (
                    recist.nodal_measurable_min_mm if lesion.nodal else recist.measurable_min_mm
                )
                assert lesion.baseline_mm >= floor

    def test_rejects_an_undeclared_arm(self, tumour_config):
        with pytest.raises(KeyError):
            build_tumour("S-001", "ARM-Z", tumour_config, Random(1))

    def test_burden_uses_all_disease_not_one_readers_selection(self, tumour_config):
        """Hazards read the subject's whole disease. A subject does not become
        less likely to die because a reader chose to measure fewer lesions."""
        tumour = build_tumour("S-001", "ARM-B", tumour_config, Random(3))
        total = tumour.true_total_sum_mm(0.0)
        assert total == pytest.approx(sum(l.baseline_mm for l in tumour.lesions))
        assert tumour.burden_ratio(0.0) == pytest.approx(1.0)


class TestTargetSelection:
    def test_respects_the_five_lesion_limit(self, tumour_config, readers):
        for index in range(30):
            tumour = build_tumour(f"S-{index}", "ARM-A", tumour_config, Random(index))
            selection = select_targets(tumour, readers[0], tumour_config, Random(index))
            assert len(selection.target) <= tumour_config.recist.max_target_lesions

    def test_respects_the_two_per_organ_limit(self, tumour_config, readers):
        for index in range(30):
            tumour = build_tumour(f"S-{index}", "ARM-A", tumour_config, Random(index))
            selection = select_targets(tumour, readers[0], tumour_config, Random(index))
            per_organ: dict[str, int] = {}
            for lesion in selection.target:
                per_organ[lesion.organ] = per_organ.get(lesion.organ, 0) + 1
            assert max(per_organ.values(), default=0) <= tumour_config.recist.max_target_per_organ

    def test_unselected_lesions_become_non_target(self, tumour_config, readers):
        tumour = build_tumour("S-001", "ARM-A", tumour_config, Random(7))
        selection = select_targets(tumour, readers[0], tumour_config, Random(7))
        selected = {lesion.lesion_id for lesion in selection.target}
        remainder = {lesion.lesion_id for lesion in selection.non_target}
        assert selected.isdisjoint(remainder)
        assert selected | remainder >= {lesion.lesion_id for lesion in tumour.lesions}

    def test_full_size_preference_makes_readers_agree(self, tumour_config, readers):
        """The control for the mechanism.

        With selection driven purely by size, every reader picks the same lesions
        and the only disagreement left is measurement noise. That the shipped
        configuration produces divergence is therefore a consequence of
        ``selection_size_preference``, not an accident of the RNG.
        """
        deterministic = tumour_config.model_copy(
            update={
                "measurement": tumour_config.measurement.model_copy(
                    update={"selection_size_preference": 1.0}
                )
            }
        )
        diverged = 0
        for index in range(25):
            tumour = build_tumour(f"S-{index}", "ARM-A", deterministic, Random(index))
            first = select_targets(tumour, readers[0], deterministic, Random(1))
            second = select_targets(tumour, readers[1], deterministic, Random(2))
            if {l.lesion_id for l in first.target} != {l.lesion_id for l in second.target}:
                diverged += 1
        assert diverged == 0

    def test_the_shipped_configuration_lets_readers_diverge(self, tumour_config, readers):
        diverged = 0
        for index in range(40):
            tumour = build_tumour(f"S-{index}", "ARM-A", tumour_config, Random(index))
            first = select_targets(tumour, readers[0], tumour_config, Random(index * 2))
            second = select_targets(tumour, readers[1], tumour_config, Random(index * 2 + 1))
            if {l.lesion_id for l in first.target} != {l.lesion_id for l in second.target}:
                diverged += 1
        assert diverged > 0


class TestMeasurement:
    def test_is_reported_to_the_millimetre(self, tumour_config, readers):
        tumour = build_tumour("S-001", "ARM-A", tumour_config, Random(5))
        selection = select_targets(tumour, readers[0], tumour_config, Random(5))
        timepoint = measure(tumour, selection, 12.0, readers[0], tumour_config, Random(5))
        step = tumour_config.measurement.quantisation_mm
        for lesion in timepoint.target:
            assert lesion.diameter_mm == pytest.approx(
                round(lesion.diameter_mm / step) * step
            )

    def test_a_missed_assessment_records_nothing(self, tumour_config, readers):
        tumour = build_tumour("S-001", "ARM-A", tumour_config, Random(5))
        selection = select_targets(tumour, readers[0], tumour_config, Random(5))
        timepoint = measure(
            tumour, selection, 12.0, readers[0], tumour_config, Random(5), missed=True
        )
        assert timepoint.missed
        assert timepoint.target == ()
        assert not timepoint.evaluable

    def test_a_biased_reader_reads_systematically_lower(self, tumour_config):
        """Averaged over many lesions, a negative bias has to show up."""
        neutral = ReaderConfig(reader_id="N", role="INVESTIGATOR", bias=0.0, cv=0.0)
        low = ReaderConfig(reader_id="L", role="INDEPENDENT", bias=-0.10, cv=0.0)
        tumour = build_tumour("S-001", "ARM-A", tumour_config, Random(9))
        selection = select_targets(tumour, neutral, tumour_config, Random(9))
        neutral_sum = sum_of_diameters(
            measure(tumour, selection, 6.0, neutral, tumour_config, Random(1)).target
        )
        low_sum = sum_of_diameters(
            measure(tumour, selection, 6.0, low, tumour_config, Random(1)).target
        )
        assert low_sum < neutral_sum

    def test_new_lesion_judgement_is_stable_across_the_course(self, tumour_config, readers):
        """A reader who calls a new lesion does not un-call it.

        The decision belongs to the reader and is taken once. Rolling it at every
        assessment made progression flicker on and off, which no radiologist
        would ever produce.
        """
        for index in range(60):
            tumour = build_tumour(f"S-{index}", "ARM-B", tumour_config, Random(index))
            if tumour.new_lesion_week is None:
                continue
            selection = select_targets(tumour, readers[0], tumour_config, Random(index))
            after = [
                measure(
                    tumour, selection, week, readers[0], tumour_config, Random(week)
                ).new_lesion
                for week in (
                    tumour.new_lesion_week,
                    tumour.new_lesion_week + 6,
                    tumour.new_lesion_week + 12,
                )
            ]
            assert len(set(after)) == 1, "new-lesion status changed mid-course"


class TestAssessmentSchedule:
    def test_intervals_widen_after_the_switch(self, tumour_config):
        schedule = tumour_config.assessment_schedule
        # No slip and no misses, to see the nominal grid.
        clean = tumour_config.model_copy(
            update={
                "assessment_schedule": schedule.model_copy(
                    update={"slip_days_sd": 0.0, "missed_probability": 0.0}
                )
            }
        )
        weeks = [week for week, _ in assessment_weeks(clean, Random(1), 120.0)]
        early = [b - a for a, b in zip(weeks, weeks[1:]) if b <= schedule.switch_week]
        late = [b - a for a, b in zip(weeks, weeks[1:]) if a >= schedule.switch_week]
        assert early and late
        assert max(early) == pytest.approx(schedule.interval_weeks)
        assert min(late) == pytest.approx(schedule.later_interval_weeks)

    def test_some_assessments_are_missed(self, tumour_config):
        missed = sum(
            1
            for index in range(40)
            for _, flag in assessment_weeks(tumour_config, Random(index), 104.0)
            if flag
        )
        assert missed > 0


class TestExposureResponse:
    """Dose received has to change the disease, or exposure means nothing.

    Two effects are needed and neither is sufficient alone. Reducing the dose
    slows the shrinkage, and it also lets the resistant fraction grow faster.
    With only the first, a lower dose gives a shallower nadir — and because RECIST
    judges progression as a relative rise from the nadir, a shallower nadir takes
    *longer* to progress from. The relationship comes out backwards: less drug
    appears to improve progression-free survival.
    """

    @staticmethod
    def _growth(suppression: float):
        from pharma_sim.clinical.lesion import GrowthParameters

        return GrowthParameters(0.72, 0.055, 0.018, growth_suppression=suppression)

    @staticmethod
    def _progression_week(growth, dose, horizon=300.0):
        """First week the sum rises 20% above its running nadir."""
        nadir = 1.0
        step = 0.5
        week = step
        while week <= horizon:
            value = growth.scale_at(week, dose)
            nadir = min(nadir, value)
            if value >= nadir * 1.20:
                return week
            week += step
        return None

    def test_dose_weighted_time_is_what_the_treatment_acts_on(self):
        from pharma_sim.clinical.lesion import DoseHistory

        full = DoseHistory(((0.0, 20.0, 1.0),))
        half = DoseHistory(((0.0, 40.0, 0.5),))
        assert full.effective_weeks(20.0) == pytest.approx(20.0)
        assert half.effective_weeks(40.0) == pytest.approx(20.0)

    def test_an_absent_history_means_continuous_full_dose(self):
        from pharma_sim.clinical.lesion import DoseHistory

        assert DoseHistory().effective_weeks(30.0) == pytest.approx(30.0)

    def test_a_lower_dose_gives_a_shallower_nadir(self):
        from pharma_sim.clinical.lesion import DoseHistory

        growth = self._growth(0.6)
        full = DoseHistory(((0.0, 300.0, 1.0),))
        half = DoseHistory(((0.0, 300.0, 0.5),))
        deepest = lambda dose: min(
            growth.scale_at(week * 0.5, dose) for week in range(1, 200)
        )
        assert deepest(half) > deepest(full)

    def test_a_lower_dose_progresses_sooner(self):
        """The property the whole coupling exists for."""
        from pharma_sim.clinical.lesion import DoseHistory

        growth = self._growth(0.6)
        weeks = [
            self._progression_week(growth, DoseHistory(((0.0, 300.0, fraction),)))
            for fraction in (1.0, 0.75, 0.5)
        ]
        assert all(week is not None for week in weeks)
        assert weeks == sorted(weeks, reverse=True), weeks

    def test_without_growth_suppression_the_relationship_inverts(self):
        """The control that shows the second effect is load-bearing.

        This is the model that was written first, and it is wrong in a way that
        looks reasonable: with only the shrinkage term, a reduced dose delays
        progression instead of hastening it.
        """
        from pharma_sim.clinical.lesion import DoseHistory

        growth = self._growth(0.0)
        full = self._progression_week(growth, DoseHistory(((0.0, 300.0, 1.0),)))
        half = self._progression_week(growth, DoseHistory(((0.0, 300.0, 0.5),)))
        assert full is not None and half is not None
        assert half > full, (
            "if this no longer inverts, growth_suppression is no longer the thing "
            "carrying the exposure-response relationship"
        )

    def test_stopping_treatment_brings_progression_forward_sharply(self):
        from pharma_sim.clinical.lesion import DoseHistory

        growth = self._growth(0.6)
        throughout = self._progression_week(growth, DoseHistory(((0.0, 300.0, 1.0),)))
        stopped = self._progression_week(
            growth, DoseHistory(((0.0, 20.0, 1.0), (20.0, 300.0, 0.0)))
        )
        assert stopped < throughout

    def test_an_interruption_costs_less_than_a_permanent_reduction(self):
        """A twelve-week gap and a permanent halving deliver similar total dose,
        but the gap is recovered from and the halving is not."""
        from pharma_sim.clinical.lesion import DoseHistory

        growth = self._growth(0.6)
        gap = self._progression_week(
            growth,
            DoseHistory(((0.0, 12.0, 1.0), (12.0, 24.0, 0.0), (24.0, 300.0, 1.0))),
        )
        halved = self._progression_week(growth, DoseHistory(((0.0, 300.0, 0.5),)))
        assert gap > halved
