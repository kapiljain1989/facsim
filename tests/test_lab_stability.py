"""ICH Q1A stability and the shelf life fitted from it.

The claim is that shelf life is an answer rather than a setting. Degradation runs
on Arrhenius kinetics, the pulls are injected on the assay method and read back
off chromatograms, and the shelf life is where the confidence bound on the
limiting attribute meets its specification.

The test that matters most is the drift one. Detector response walks with time,
and over a three-year study that walk is larger than the degradation being
measured -- so an assay referenced to a nominal response factor trends the wrong
way and the product appears to gain active as it ages. Referencing a standard
injected in the same sequence is what removes it, and that is the real
methodology rather than a modelling convenience.
"""

from __future__ import annotations

import math
import statistics as stats
from datetime import date
from pathlib import Path

import pytest

from pharma_sim.engine.ids import IdFactory
from pharma_sim.engine.rng import RngRegistry
from pharma_sim.lab.loader import load_lab_config
from pharma_sim.lab.stability import (
    acceleration_factor,
    degraded_percent,
    fit_shelf_life,
    run_stability,
)

CONFIG = Path(__file__).resolve().parents[1] / "config" / "lab"
MADE_ON = date(2026, 1, 15)


@pytest.fixture(scope="module")
def config():
    return load_lab_config(CONFIG)


@pytest.fixture(scope="module")
def executed(config):
    """One full programme, reused: 45 samples and 180 injections."""
    batches = [(f"BATCH-2026-{index:06d}", MADE_ON) for index in (22, 23, 32)]
    return run_stability(
        config, config.stability.protocols[0], batches, RngRegistry(42), IdFactory()
    )


class TestKinetics:
    def test_reference_condition_has_no_acceleration(self, config):
        long_term = config.stability.condition("LONG_TERM")
        assert acceleration_factor(long_term, config.stability.kinetics) == pytest.approx(
            1.0, abs=1e-9
        )

    def test_accelerated_matches_arrhenius_by_hand(self, config):
        """Computed independently, so a sign error in the exponent cannot pass."""
        kinetics = config.stability.kinetics
        condition = config.stability.condition("ACCELERATED")
        energy = kinetics.activation_energy_kj_mol * 1000.0
        arrhenius = math.exp(
            -(energy / 8.314)
            * (1.0 / (condition.temperature_c + 273.15)
               - 1.0 / (kinetics.reference_temperature_c + 273.15))
        )
        humidity = (condition.humidity_pct / kinetics.reference_humidity_pct) ** (
            kinetics.humidity_exponent
        )
        assert acceleration_factor(condition, kinetics) == pytest.approx(
            arrhenius * humidity, rel=1e-9
        )

    def test_hotter_and_wetter_degrades_faster(self, config):
        kinetics = config.stability.kinetics
        factors = [
            acceleration_factor(config.stability.condition(name), kinetics)
            for name in ("LONG_TERM", "INTERMEDIATE", "ACCELERATED")
        ]
        assert factors == sorted(factors)

    def test_degradation_is_zero_at_time_zero(self, config):
        for name in ("LONG_TERM", "ACCELERATED"):
            assert degraded_percent(0.0, config.stability.condition(name),
                                    config.stability.kinetics) == 0.0

    def test_degradation_is_proportional_to_time(self, config):
        condition = config.stability.condition("LONG_TERM")
        kinetics = config.stability.kinetics
        assert degraded_percent(24.0, condition, kinetics) == pytest.approx(
            2.0 * degraded_percent(12.0, condition, kinetics)
        )


class TestShelfLifeFit:
    def _line(self, slope, intercept, n=8):
        return [(month * 3.0, intercept + slope * month * 3.0) for month in range(n)]

    def test_recovers_a_clean_intersection(self):
        # 0.3 at time zero rising 0.02 a month meets 1.0 at 35 months.
        rules = _rules(maximum=48, step=3)
        fit = fit_shelf_life(
            self._line(0.02, 0.3), attribute="total_impurities", limit=1.0,
            upper=True, rules=rules,
        )
        assert fit.slope_per_month == pytest.approx(0.02, rel=1e-6)
        assert fit.intersection_months == pytest.approx(35.0, abs=0.5)
        assert fit.months == 33.0  # rounded down to a whole quarter

    def test_the_bound_is_shorter_than_the_mean_line(self):
        """The point of using a confidence bound at all.

        A shelf life set where the mean line crosses the limit is one half the
        batches fail. Scatter has to shorten it.
        """
        rules = _rules(maximum=60, step=3)
        clean = fit_shelf_life(
            self._line(0.02, 0.3), attribute="x", limit=1.0, upper=True, rules=rules
        )
        noisy_points = [
            (month, value + (0.05 if index % 2 else -0.05))
            for index, (month, value) in enumerate(self._line(0.02, 0.3))
        ]
        noisy = fit_shelf_life(
            noisy_points, attribute="x", limit=1.0, upper=True, rules=rules
        )
        assert noisy.residual_sd > clean.residual_sd
        assert noisy.intersection_months < clean.intersection_months

    def test_a_declining_attribute_uses_a_lower_bound(self):
        rules = _rules(maximum=48, step=3)
        fit = fit_shelf_life(
            self._line(-0.2, 100.0), attribute="assay", limit=95.0,
            upper=False, rules=rules,
        )
        assert fit.intersection_months == pytest.approx(25.0, abs=1.0)

    def test_reports_when_the_study_was_not_long_enough(self):
        rules = _rules(maximum=12, step=3)
        fit = fit_shelf_life(
            self._line(0.001, 0.3), attribute="x", limit=1.0, upper=True, rules=rules
        )
        assert fit.limited_by_study_length
        assert fit.months == 12.0

    def test_too_few_points_to_fit(self):
        rules = _rules(maximum=36, step=3)
        fit = fit_shelf_life([(0.0, 0.3)], attribute="x", limit=1.0,
                             upper=True, rules=rules)
        assert fit.months == 0.0


class TestExecutedProgramme:
    def test_pulls_every_declared_timepoint_for_every_batch(self, executed, config):
        protocol = config.stability.protocols[0]
        tested = {row["condition_id"] for row in executed.samples}
        for condition in config.stability.conditions:
            if condition.condition_id not in tested:
                continue
            for months in condition.timepoints_months:
                matching = [
                    row for row in executed.samples
                    if row["condition_id"] == condition.condition_id
                    and row["timepoint_months"] == months
                ]
                assert len(matching) == protocol.batches

    def test_significant_change_is_detected_at_the_accelerated_condition(self, executed):
        assert executed.significant_change
        assert executed.significant_change[0]["condition_id"] == "ACCELERATED"

    def test_the_intermediate_condition_is_only_tested_because_of_it(self, executed):
        """ICH Q1A asks for the intermediate condition when the accelerated one
        shows significant change, not as a matter of course."""
        tested = {row["condition_id"] for row in executed.samples}
        assert "INTERMEDIATE" in tested
        assert executed.significant_change

    def test_impurities_are_the_limiting_attribute(self, executed):
        """The usual case, and worth reproducing: a tablet runs out of impurity
        headroom long before it runs out of active."""
        assert executed.limiting_attribute == "total_impurities"
        assert 0 < executed.shelf_life_months <= 36

    def test_the_impurity_slope_is_recovered_from_the_chromatograms(self, executed, config):
        """The trend is measured, not declared. Recovering the configured rate
        from integrated peak areas is what makes that claim checkable."""
        declared = config.stability.kinetics.reference_rate_percent_per_month
        fitted = next(
            life for life in executed.shelf_lives if life.attribute == "total_impurities"
        )
        assert fitted.slope_per_month == pytest.approx(declared, rel=0.15)

    def test_assay_does_not_inherit_detector_drift(self, executed, config):
        """Detector response drifts up over three years, and that drift is larger
        than the degradation being measured.

        Referenced to the method's nominal response factor the assay slope came
        out at +0.163 per month -- six times the true degradation rate and the
        wrong sign, so the product appeared to gain active as it aged.
        Referencing a standard injected in the same sequence removes it.

        The bound is deliberately loose. At three batches the assay trend is not
        resolvable at all: the true change is under one percent across the whole
        study and the scatter is comparable, so the slope estimate is dominated
        by noise. What this asserts is that the *artefact* is gone, which is a
        difference of an order of magnitude and is resolvable.
        """
        fitted = next(life for life in executed.shelf_lives if life.attribute == "assay")
        declared = config.stability.kinetics.reference_rate_percent_per_month
        assert abs(fitted.slope_per_month) < 5.0 * declared

    def test_the_assay_trend_is_not_resolvable_at_three_batches(self, executed):
        """Stated as a property rather than left as a surprise.

        ICH asks for three primary batches. At three, the standard error on the
        assay slope is about half the slope itself, so assay cannot set a shelf
        life for this product -- and that is exactly why impurities do.
        """
        fitted = next(life for life in executed.shelf_lives if life.attribute == "assay")
        span_months = 36.0
        # Rough standard error of a slope fitted over this span.
        standard_error = fitted.residual_sd / (span_months / 3.0)
        assert standard_error > abs(fitted.slope_per_month), (
            "if the assay slope became resolvable at three batches, the scatter "
            "model has been made unrealistically tight"
        )

    def test_assay_declines_once_the_study_is_large_enough(self, config):
        """With enough batches the noise averages down and the true direction
        appears. This is the check that the degradation is actually happening
        rather than being invisible for the wrong reason."""
        batches = [(f"BATCH-{index:04d}", MADE_ON) for index in range(12)]
        out = run_stability(
            config, config.stability.protocols[0].model_copy(update={"batches": 12}),
            batches, RngRegistry(7), IdFactory(),
        )
        fitted = next(life for life in out.shelf_lives if life.attribute == "assay")
        assert fitted.slope_per_month < 0.0

    def test_every_result_traces_to_an_injection(self, executed):
        samples = {row["sample_id"] for row in executed.samples}
        assert {row["sample_id"] for row in executed.injections} <= samples
        assert {row["sample_id"] for row in executed.results} <= samples

    def test_a_standard_is_injected_alongside_every_sample(self, executed):
        purposes = {row.get("purpose") for row in executed.injections}
        assert "STANDARD" in purposes and "STABILITY" in purposes
        by_sample: dict[str, set[str]] = {}
        for row in executed.injections:
            by_sample.setdefault(row["sample_id"], set()).add(row.get("purpose"))
        assert all(
            purposes == {"STANDARD", "STABILITY"} for purposes in by_sample.values()
        )


class TestOutOfSpecification:
    def test_breaches_are_investigated_not_silently_used(self, executed):
        """A result outside specification gets an investigation. Letting it
        merely truncate the shelf life would lose the reason."""
        assert executed.out_of_specification
        for row in executed.out_of_specification:
            assert row["finding"]
            assert row["phase_1_conclusion"]
            assert row["outcome"] == "CONFIRMED"

    def test_every_investigation_matches_a_failing_certificate(self, executed):
        failing = {
            row["sample_id"] for row in executed.certificates
            if row["conclusion"] != "COMPLIES"
        }
        assert {row["sample_id"] for row in executed.out_of_specification} == failing

    def test_review_is_by_someone_other_than_the_analyst(self, executed):
        assert executed.reviews
        for row in executed.reviews:
            assert row["reviewed_by"] != row["performed_by"]

    def test_a_failing_sample_is_referred_rather_than_approved(self, executed):
        failing = {row["sample_id"] for row in executed.out_of_specification}
        for row in executed.reviews:
            expected = "REFERRED_TO_INVESTIGATION" if row["sample_id"] in failing else "APPROVED"
            assert row["outcome"] == expected


class TestReproducibility:
    def test_same_seed_agrees(self, config):
        batches = [(f"B-{i}", MADE_ON) for i in range(3)]

        def run():
            out = run_stability(
                config, config.stability.protocols[0], batches,
                RngRegistry(11), IdFactory(),
            )
            return [row["value"] for row in out.results]

        assert run() == run()

    def test_a_different_seed_moves_the_results(self, config):
        batches = [(f"B-{i}", MADE_ON) for i in range(3)]

        def run(seed):
            out = run_stability(
                config, config.stability.protocols[0], batches,
                RngRegistry(seed), IdFactory(),
            )
            return [row["value"] for row in out.results]

        assert run(1) != run(2)


def _rules(*, maximum: int, step: int):
    from pharma_sim.lab.config import ShelfLifeRules

    return ShelfLifeRules(
        fit_condition="LONG_TERM",
        confidence=0.95,
        maximum_months=maximum,
        round_down_to_months=step,
    )
