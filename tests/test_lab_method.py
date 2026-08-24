"""Laboratory configuration and the method observation layer.

The claims under test are modelling claims, not coverage:

* conditions act on retention the way chromatography does — flow as an inverse
  law, the rest locally linear;
* replicate injections of one preparation and separate preparations give
  *different* precision, which is the whole basis of repeatability versus
  intermediate precision meaning two different things;
* the cross-file linter catches a broken reference, since every identifier is a
  plain string by design and the type system cannot.
"""

from __future__ import annotations

import shutil
import statistics as stats
from pathlib import Path

import pytest

from pharma_sim.config.errors import ConfigError
from pharma_sim.engine.rng import RngRegistry
from pharma_sim.lab.config import Conditions
from pharma_sim.lab.loader import load_lab_config
from pharma_sim.lab.method import (
    ColumnState,
    InjectionRequest,
    MethodModel,
    Preparation,
    condition_multiplier,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAB_CONFIG = PROJECT_ROOT / "config" / "lab"


@pytest.fixture(scope="module")
def lab_config():
    return load_lab_config(LAB_CONFIG)


@pytest.fixture(scope="module")
def assay(lab_config):
    return lab_config.methods.by_id("MTH-0001")


@pytest.fixture
def model(lab_config, assay):
    return MethodModel(assay, lab_config, RngRegistry(42))


def _request(lab_config, assay, tag, conditions=None, concentrations=None, injections=1):
    return InjectionRequest(
        injection_id=f"INJ-{tag}",
        sequence_id="SEQ-TEST",
        injection_number=1,
        preparation=Preparation(
            f"PREP-{tag}",
            f"SMP-{tag}",
            concentrations
            if concentrations is not None
            else {"SUB-0001": assay.standard_concentration_ug_ml},
        ),
        conditions=conditions or assay.nominal_conditions,
        instrument=lab_config.instruments.instrument("INS-HPLC-01"),
        analyst=lab_config.instruments.analyst("ANL-01"),
        column=ColumnState("COL-0007", injections),
    )


class TestShippedConfiguration:
    def test_loads(self, lab_config):
        assert lab_config.methods.methods
        assert lab_config.validations.validations

    def test_every_method_analyte_is_a_declared_substance(self, lab_config):
        declared = {s.substance_id for s in lab_config.substances.substances}
        for method in lab_config.methods.methods:
            for analyte in method.analytes:
                assert analyte.analyte_id in declared

    def test_critical_pair_partners_are_adjacent_in_retention(self, assay):
        """A critical pair that is not actually the closest pair is a config
        error the linter cannot see, because both references resolve."""
        first, second = assay.critical_pair
        gap = abs(
            assay.analyte(first).retention_time_min - assay.analyte(second).retention_time_min
        )
        others = sorted(a.retention_time_min for a in assay.analytes)
        closest = min(b - a for a, b in zip(others, others[1:]))
        assert gap == pytest.approx(closest, abs=1e-9)


class TestLinter:
    """Cross-file references, which are strings by design."""

    def test_rejects_a_suitability_criterion_naming_an_unknown_analyte(self, tmp_path):
        target = tmp_path / "lab"
        shutil.copytree(LAB_CONFIG, target)
        cds = target / "cds.yaml"
        cds.write_text(cds.read_text().replace("analyte_id: SUB-0001", "analyte_id: SUB-9999", 1))
        with pytest.raises(ConfigError) as excinfo:
            load_lab_config(target)
        assert "SUB-9999" in str(excinfo.value)

    def test_rejects_a_robustness_factor_that_is_not_a_condition(self, tmp_path):
        target = tmp_path / "lab"
        shutil.copytree(LAB_CONFIG, target)
        validation = target / "validation.yaml"
        validation.write_text(
            validation.read_text().replace("factor: organic_percent", "factor: moon_phase", 1)
        )
        with pytest.raises(ConfigError) as excinfo:
            load_lab_config(target)
        assert "moon_phase" in str(excinfo.value)


class TestConditionEffects:
    def test_flow_acts_as_an_inverse_law(self, assay):
        """Retention is inversely proportional to flow, over the whole range —
        which is why flow is an exponent and not a per-unit fraction."""
        nominal = assay.nominal_conditions
        doubled = nominal.varied("flow_rate_ml_min", nominal.flow_rate_ml_min)
        multiplier = condition_multiplier({"flow_rate_ml_min": -1.0}, doubled, nominal)
        assert multiplier == pytest.approx(0.5, rel=1e-9)

    def test_other_factors_are_locally_linear(self, assay):
        nominal = assay.nominal_conditions
        warmer = nominal.varied("column_temperature_c", 5.0)
        multiplier = condition_multiplier({"column_temperature_c": -0.01}, warmer, nominal)
        assert multiplier == pytest.approx(0.95, rel=1e-9)

    def test_an_unlisted_factor_has_no_effect(self, assay):
        nominal = assay.nominal_conditions
        assert condition_multiplier({}, nominal.varied("mobile_phase_ph", 1.0), nominal) == 1.0

    def test_varied_rejects_an_unknown_factor(self, assay):
        with pytest.raises(KeyError):
            assay.nominal_conditions.varied("not_a_condition", 1.0)

    def test_raising_organic_shortens_retention(self, model, lab_config, assay):
        nominal = assay.nominal_conditions
        analyte = assay.analyte("SUB-0001")
        base = model.expected_retention(analyte, _request(lab_config, assay, "a"))
        raised = model.expected_retention(
            analyte, _request(lab_config, assay, "b", nominal.varied("organic_percent", 2.0))
        )
        assert raised < base

    def test_the_critical_pair_converges_as_organic_rises(self, model, lab_config, assay):
        """The declared sensitivity coefficients differ between the pair, so the
        gap between them closes. This is what makes the method's weak point a
        consequence of configuration rather than a hard-coded outcome."""
        nominal = assay.nominal_conditions
        first, second = assay.analyte("SUB-0004"), assay.analyte("SUB-0001")

        def gap(conditions):
            request = _request(lab_config, assay, "g", conditions)
            return abs(
                model.expected_retention(second, request)
                - model.expected_retention(first, request)
            )

        assert gap(nominal.varied("organic_percent", 2.0)) < gap(nominal)


class TestColumnAgeing:
    def test_plate_count_declines_monotonically_with_use(self, model, lab_config, assay):
        counts = []
        for used in (0, 400, 800, 1200):
            result = model.inject(
                _request(lab_config, assay, f"age{used}", injections=used), keep_trace=False
            )
            peak = result.peak_for("SUB-0001")
            assert peak is not None and peak.plate_count_usp is not None
            counts.append(peak.plate_count_usp)
        assert counts == sorted(counts, reverse=True)


class TestPrecisionStructure:
    """The distinction the whole precision story rests on."""

    def _areas(self, model, lab_config, assay, *, shared_preparation: bool):
        concentrations = {"SUB-0001": assay.standard_concentration_ug_ml}
        areas = []
        for replicate in range(1, 9):
            tag = "shared" if shared_preparation else f"sep{replicate}"
            request = InjectionRequest(
                injection_id=f"INJ-{shared_preparation}-{replicate}",
                sequence_id="SEQ-P",
                injection_number=replicate,
                preparation=Preparation(f"PREP-{tag}", f"SMP-{tag}", concentrations),
                conditions=assay.nominal_conditions,
                instrument=lab_config.instruments.instrument("INS-HPLC-01"),
                analyst=lab_config.instruments.analyst("ANL-01"),
                column=ColumnState("COL-0007", replicate),
            )
            area = model.inject(request, keep_trace=False).area_of("SUB-0001")
            if area is not None:
                areas.append(area)
        return areas

    def test_separate_preparations_scatter_more_than_repeat_injections(
        self, model, lab_config, assay
    ):
        """Five injections of one preparation measure the injector; six separate
        preparations measure the method. If these came out equal, repeatability
        and system suitability would be the same number, and they are not.
        """
        shared = self._areas(model, lab_config, assay, shared_preparation=True)
        separate = self._areas(model, lab_config, assay, shared_preparation=False)
        shared_rsd = 100 * stats.stdev(shared) / stats.fmean(shared)
        separate_rsd = 100 * stats.stdev(separate) / stats.fmean(separate)
        assert separate_rsd > shared_rsd

    def test_repeat_injection_scatter_is_near_the_declared_injection_rsd(
        self, model, lab_config, assay
    ):
        shared = self._areas(model, lab_config, assay, shared_preparation=True)
        observed = stats.stdev(shared) / stats.fmean(shared)
        declared = assay.variability.injection_rsd
        assert observed == pytest.approx(declared, abs=0.004)


class TestPeakAssignment:
    def test_labels_every_declared_analyte_in_a_sample(self, model, lab_config, assay):
        concentrations = {"SUB-0001": assay.standard_concentration_ug_ml}
        for analyte in assay.analytes:
            if analyte.analyte_id == "SUB-0001":
                continue
            share = (analyte.specification_percent or 0.1) * 0.5 / 100.0
            concentrations[analyte.analyte_id] = (
                assay.standard_concentration_ug_ml * share
            )
        result = model.inject(
            _request(lab_config, assay, "sample", concentrations=concentrations),
            keep_trace=False,
        )
        assigned = {peak.analyte_id for peak in result.peaks if peak.analyte_id}
        assert assigned == {a.analyte_id for a in assay.analytes}

    def test_an_undeclared_peak_stays_unknown(self, model, lab_config, assay):
        """A peak nobody declared must not be labelled as something else."""
        result = model.inject(
            _request(lab_config, assay, "std"), keep_trace=False
        )
        for peak in result.peaks:
            if peak.analyte_id is None:
                continue
            expected = model.expected_retention(
                assay.analyte(peak.analyte_id), _request(lab_config, assay, "std")
            )
            assert abs(peak.retention_time_min - expected) <= max(0.12, 0.03 * expected)


class TestReproducibility:
    def test_same_seed_gives_identical_areas(self, lab_config, assay):
        def run():
            model = MethodModel(assay, lab_config, RngRegistry(42))
            return model.inject(
                _request(lab_config, assay, "repro"), keep_trace=False
            ).area_of("SUB-0001")

        assert run() == run()

    def test_different_seed_gives_different_areas(self, lab_config, assay):
        first = MethodModel(assay, lab_config, RngRegistry(1)).inject(
            _request(lab_config, assay, "repro"), keep_trace=False
        ).area_of("SUB-0001")
        second = MethodModel(assay, lab_config, RngRegistry(2)).inject(
            _request(lab_config, assay, "repro"), keep_trace=False
        ).area_of("SUB-0001")
        assert first != second
