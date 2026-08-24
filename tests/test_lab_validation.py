"""ICH Q2 validation as an executed study.

The headline claim is that the robustness study *discovers* which condition the
method is sensitive to. Nothing declares "organic content is the weak point" --
the analytes carry different sensitivity coefficients, the critical pair
therefore converges as organic rises, the resolution measured from the trace
falls below its limit, and the suitability set fails. These tests assert that
chain end to end, and that it is the chain rather than a coincidence.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from pharma_sim.engine.ids import IdFactory
from pharma_sim.engine.rng import RngRegistry
from pharma_sim.lab.config import Criterion, SuitabilityCriterion
from pharma_sim.lab.loader import load_lab_config
from pharma_sim.lab.validation import ValidationRunner, linear_fit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAB_CONFIG = PROJECT_ROOT / "config" / "lab"


@pytest.fixture(scope="module")
def lab_config():
    return load_lab_config(LAB_CONFIG)


@pytest.fixture(scope="module")
def executed(lab_config):
    """One full validation, reused: it is ~240 injections and a few seconds."""
    validation = lab_config.validations.validations[0]
    return ValidationRunner(
        lab_config, validation, RngRegistry(42), IdFactory(), keep_traces=False
    ).run()


class TestLinearFit:
    def test_recovers_a_known_line(self):
        points = [(x, 3.0 * x + 7.0) for x in (1.0, 2.0, 3.0, 4.0, 5.0)]
        fit = linear_fit(points)
        assert fit.slope == pytest.approx(3.0)
        assert fit.intercept == pytest.approx(7.0)
        assert fit.r_squared == pytest.approx(1.0)
        assert fit.residual_sd == pytest.approx(0.0, abs=1e-9)

    def test_residual_sd_grows_with_scatter(self):
        clean = linear_fit([(x, 2.0 * x) for x in range(1, 8)])
        noisy = linear_fit([(x, 2.0 * x + (1 if x % 2 else -1)) for x in range(1, 8)])
        assert noisy.residual_sd > clean.residual_sd
        assert noisy.r_squared < clean.r_squared

    def test_inverts_to_a_concentration(self):
        fit = linear_fit([(x, 4.0 * x + 1.0) for x in range(1, 6)])
        assert fit.concentration_for(4.0 * 3.0 + 1.0) == pytest.approx(3.0)

    def test_degenerate_input_does_not_raise(self):
        assert linear_fit([]).slope == 0.0
        assert linear_fit([(1.0, 1.0)]).slope == 0.0


class TestCriteria:
    def test_an_unmeasurable_value_fails_rather_than_being_skipped(self):
        """A critical pair too fused to have a resolution has not demonstrated
        resolution. Treating None as 'not evaluated' would pass a failing set."""
        criterion = SuitabilityCriterion(metric="RESOLUTION", operator="GE", limit=2.0)
        assert criterion.passes(None) is False
        assert criterion.passes(2.5) is True
        assert criterion.passes(1.5) is False

    def test_between_is_inclusive(self):
        criterion = Criterion(metric="RECOVERY_PERCENT", operator="BETWEEN", limit=[98.0, 102.0])
        assert criterion.passes(98.0) and criterion.passes(102.0)
        assert not criterion.passes(97.9)


class TestExecutedStudy:
    def test_produces_every_record_stream(self, executed):
        assert executed.sequences
        assert executed.injections
        assert executed.peaks
        assert executed.suitability
        assert executed.results
        assert executed.audit

    def test_every_experiment_in_the_protocol_reported(self, executed, lab_config):
        declared = {
            name
            for name in type(lab_config.validations.validations[0].experiments).model_fields
            if getattr(lab_config.validations.validations[0].experiments, name) is not None
        }
        reported = {row["experiment"] for row in executed.results}
        assert declared == reported

    def test_peaks_reference_real_injections(self, executed):
        injections = {row["injection_id"] for row in executed.injections}
        assert {row["injection_id"] for row in executed.peaks} <= injections

    def test_audit_events_are_all_declared_in_config(self, executed, lab_config):
        declared = {event.code for event in lab_config.cds.audit_trail.events}
        assert {row["event_code"] for row in executed.audit} <= declared

    def test_informational_results_do_not_count_as_failures(self, executed):
        """LOD and slope are numbers the protocol asks for without a limit."""
        informational = [r for r in executed.results if r["verdict"] == "INFORMATIONAL"]
        assert informational
        assert all(row not in executed.failures for row in informational)

    def test_precision_metrics_are_plausible_for_an_assay(self, executed):
        """Repeatability under 2% and intermediate precision above it in spread
        is what a real HPLC assay gives; wildly better would mean the
        variability model was not being exercised."""
        by_metric = {
            (row["experiment"], row["metric"]): row["measured"] for row in executed.results
        }
        repeatability = by_metric[("repeatability", "RSD_PERCENT")]
        combined = by_metric[("intermediate_precision", "COMBINED_RSD_PERCENT")]
        assert 0.1 < repeatability < 2.0
        assert 0.1 < combined < 3.0


class TestRobustnessDiscoversTheWeakPoint:
    """The chain: different sensitivities -> convergence -> failed resolution."""

    def _robustness(self, executed):
        return [
            row
            for row in executed.results
            if row["experiment"] == "robustness"
            and row["metric"].startswith("SUITABILITY_PASSES")
        ]

    def test_organic_content_is_identified_as_critical(self, executed):
        failing = [
            row["detail"] for row in self._robustness(executed) if row["verdict"] == "FAIL"
        ]
        assert any("organic_percent" in detail for detail in failing)

    def test_flow_rate_is_not_identified_as_critical(self, executed):
        """Flow shifts every retention time together, so selectivity — and
        therefore resolution — is unchanged. A model that failed flow too would
        be shifting peaks without any chromatography behind it."""
        failing = [
            row["detail"] for row in self._robustness(executed) if row["verdict"] == "FAIL"
        ]
        assert not any("flow_rate" in detail for detail in failing)

    def test_the_failure_is_a_resolution_failure(self, executed):
        """Not an area or tailing artefact: the pair genuinely co-elutes."""
        failed = [row for row in executed.suitability if row["verdict"] == "FAIL"]
        assert any("RESOLUTION" in row["failed_metrics"] for row in failed)

    def test_assay_result_survives_the_condition_that_breaks_resolution(self, executed):
        """Losing the critical pair does not move the assay value much, which is
        why suitability exists: the number looks fine while the method has
        stopped being able to see the impurity next to it."""
        differences = [
            row["measured"]
            for row in executed.results
            if row["metric"].startswith("ASSAY_DIFFERENCE_PERCENT")
            and "organic_percent +2" in row["metric"]
        ]
        assert differences and all(value < 2.0 for value in differences)


class TestSuitabilityRetry:
    def test_a_physical_failure_repeats_across_every_attempt(self, executed):
        """A bubble passes on the re-run; a co-eluting pair does not. Sequences
        that failed more than once must show near-identical measurements, since
        the cause is the conditions rather than chance."""
        by_sequence: dict[str, list[dict]] = {}
        for row in executed.suitability:
            by_sequence.setdefault(row["sequence_id"], []).append(row)

        repeated = [
            rows
            for rows in by_sequence.values()
            if len(rows) > 1 and all(row["verdict"] == "FAIL" for row in rows)
        ]
        assert repeated, "expected at least one condition to fail every attempt"
        for rows in repeated:
            values = [
                row["measured_resolution"]
                for row in rows
                if row.get("measured_resolution") is not None
            ]
            if len(values) > 1:
                assert max(values) - min(values) < 0.15

    def test_a_spurious_failure_is_followed_by_a_pass(self, executed):
        by_sequence: dict[str, list[dict]] = {}
        for row in executed.suitability:
            by_sequence.setdefault(row["sequence_id"], []).append(row)
        recovered = [
            rows
            for rows in by_sequence.values()
            if len(rows) > 1 and rows[-1]["verdict"] == "PASS"
        ]
        assert recovered, "expected at least one failed set to pass on re-run"


class TestReproducibility:
    def test_two_runs_at_one_seed_agree_exactly(self, lab_config):
        validation = lab_config.validations.validations[0]

        def run():
            output = ValidationRunner(
                lab_config, validation, RngRegistry(7), IdFactory(), keep_traces=False
            ).run()
            return [
                (row["metric"], row["measured"]) for row in output.results
            ]

        assert run() == run()

    def test_a_different_seed_moves_the_measurements(self, lab_config):
        validation = lab_config.validations.validations[0]

        def run(seed):
            output = ValidationRunner(
                lab_config, validation, RngRegistry(seed), IdFactory(), keep_traces=False
            ).run()
            return [row["measured"] for row in output.results]

        assert run(11) != run(12)
