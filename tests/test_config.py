"""Configuration loading, validation and linting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from pharma_sim.config.errors import ConfigError
from pharma_sim.config.linter import lint_config
from pharma_sim.config.loader import (
    config_fingerprint,
    diff_fingerprints,
    load_config,
)
from pharma_sim.config.models import FactoryConfig, Term, Transfer


def _rewrite(path: Path, mutate) -> None:
    data = yaml.safe_load(path.read_text())
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


class TestLoading:
    def test_default_config_loads_and_is_consistent(self, config):
        assert isinstance(config, FactoryConfig)
        assert lint_config(config) == []

    def test_missing_directory_raises(self, tmp_path):
        with pytest.raises(ConfigError):
            load_config(tmp_path / "nope")

    def test_missing_required_file_is_reported_with_the_filename(self, temp_config):
        (temp_config / "states.yaml").unlink()
        with pytest.raises(ConfigError) as excinfo:
            load_config(temp_config)
        assert any("states.yaml" in issue.file for issue in excinfo.value.issues)

    def test_optional_files_fall_back_to_defaults(self, temp_config):
        (temp_config / "sinks.yaml").unlink()
        config = load_config(temp_config)
        assert config.sinks.sinks == []

    def test_unknown_key_is_rejected_with_a_hint(self, temp_config):
        _rewrite(temp_config / "plant.yaml", lambda d: d.update(nonsense_key=1))
        with pytest.raises(ConfigError) as excinfo:
            load_config(temp_config)
        issue = next(i for i in excinfo.value.issues if "nonsense_key" in i.path)
        assert "unknown key" in issue.hint

    def test_all_problems_are_reported_together(self, temp_config):
        _rewrite(temp_config / "plant.yaml", lambda d: d.update(bogus=1))
        _rewrite(temp_config / "shifts.yaml", lambda d: d.update(also_bogus=2))
        with pytest.raises(ConfigError) as excinfo:
            load_config(temp_config)
        files = {issue.file for issue in excinfo.value.issues}
        assert {"plant.yaml", "shifts.yaml"} <= files

    def test_unparseable_yaml_is_reported(self, temp_config):
        (temp_config / "plant.yaml").write_text("plant_id: [unclosed\n")
        with pytest.raises(ConfigError) as excinfo:
            load_config(temp_config)
        assert any("not parseable" in i.message for i in excinfo.value.issues)


class TestFingerprint:
    def test_identical_config_gives_identical_fingerprint(self, temp_config):
        assert config_fingerprint(load_config(temp_config)) == config_fingerprint(
            load_config(temp_config)
        )

    def test_changed_config_changes_the_fingerprint(self, temp_config):
        before = load_config(temp_config)
        _rewrite(
            temp_config / "plant.yaml",
            lambda d: d.__setitem__("location", "Somewhere else"),
        )
        after = load_config(temp_config)
        assert config_fingerprint(before) != config_fingerprint(after)
        changes = diff_fingerprints(before, after)
        assert any("location" in change for change in changes)


class TestLinter:
    """Each check exists because the type system cannot catch it."""

    def test_undeclared_transition_target(self, temp_config):
        _rewrite(
            temp_config / "states.yaml",
            lambda d: d["transitions"]["IDLE"].append("NOT_A_STATE"),
        )
        issues = lint_config(load_config(temp_config))
        assert any("NOT_A_STATE" in i.message for i in issues)

    def test_role_referencing_undeclared_state(self, temp_config):
        _rewrite(
            temp_config / "states.yaml",
            lambda d: d["roles"]["productive"].append("GHOST"),
        )
        issues = lint_config(load_config(temp_config))
        assert any("GHOST" in i.message for i in issues)

    def test_empty_required_role_is_flagged(self, temp_config):
        _rewrite(temp_config / "states.yaml", lambda d: d["roles"].__setitem__("fault", []))
        issues = lint_config(load_config(temp_config))
        assert any("fault" in i.path and "empty" in i.message for i in issues)

    def test_dangling_sensor_profile(self, temp_config):
        def mutate(data):
            data["equipment_classes"][0]["sensor_profile"] = "no_such_profile"

        _rewrite(temp_config / "machines.yaml", mutate)
        issues = lint_config(load_config(temp_config))
        assert any("no_such_profile" in i.message for i in issues)

    def test_layout_referencing_unknown_unit(self, temp_config):
        def mutate(data):
            data["layout"]["UNIT-99"] = [
                {"equipment_class": "tablet_press", "count": 1, "id_prefix": "XX"}
            ]

        _rewrite(temp_config / "machines.yaml", mutate)
        issues = lint_config(load_config(temp_config))
        assert any("UNIT-99" in i.message for i in issues)

    def test_removing_a_tag_that_was_never_inherited(self, temp_config):
        def mutate(data):
            for spec in data["equipment_classes"]:
                if spec["id"] == "tablet_press":
                    spec.setdefault("sensors", []).append(
                        {"tag": "not_inherited", "remove": True}
                    )

        _rewrite(temp_config / "machines.yaml", mutate)
        issues = lint_config(load_config(temp_config))
        assert any("cannot remove sensor" in i.message for i in issues)

    def test_qc_transfer_input_that_does_not_exist(self, temp_config):
        def mutate(data):
            data["parameters"][0]["transfer"]["terms"].append(
                {"input": "imaginary_parameter", "coef": 1.0}
            )

        _rewrite(temp_config / "qc_rules.yaml", mutate)
        issues = lint_config(load_config(temp_config))
        assert any("imaginary_parameter" in i.message for i in issues)

    def test_qc_dependency_cycle_is_detected(self, temp_config):
        def mutate(data):
            by_id = {spec["id"]: spec for spec in data["parameters"]}
            # tablet_hardness already feeds disintegration_time; close the loop.
            by_id["tablet_hardness"]["transfer"]["terms"].append(
                {"input": "disintegration_time", "coef": 0.1}
            )

        _rewrite(temp_config / "qc_rules.yaml", mutate)
        issues = lint_config(load_config(temp_config))
        assert any("dependency cycle" in i.message for i in issues)

    def test_precursor_tag_absent_from_applicable_equipment(self, temp_config):
        def mutate(data):
            for spec in data["failure_modes"]:
                if spec["id"] == "BEARING_FAILURE":
                    spec["precursors"].append(
                        {"tag": "room_humidity", "delta_fraction": 0.2}
                    )

        _rewrite(temp_config / "failures.yaml", mutate)
        issues = lint_config(load_config(temp_config))
        assert any("room_humidity" in i.message for i in issues)

    def test_root_cause_no_rca_rule_can_reach(self, temp_config):
        def mutate(data):
            data["rules"] = [
                rule for rule in data["rules"] if rule["id"] != "RCA-LUBRICATION"
            ]

        _rewrite(temp_config / "rca_rules.yaml", mutate)
        issues = lint_config(load_config(temp_config))
        assert any("INSUFFICIENT_LUBRICATION" in i.message for i in issues)

    def test_deviation_trigger_must_be_a_declared_event_type(self, temp_config):
        def mutate(data):
            data["rules"][0]["trigger_event"] = "NOT_AN_EVENT"

        _rewrite(temp_config / "deviations.yaml", mutate)
        issues = lint_config(load_config(temp_config))
        assert any("NOT_AN_EVENT" in i.message for i in issues)

    def test_product_qc_spec_from_a_stage_not_in_its_route(self, temp_config):
        def mutate(data):
            for spec in data["products"]:
                if spec["product_id"] == "VITC-500":
                    spec["qc_specifications"].append("moisture_content")

        _rewrite(temp_config / "products.yaml", mutate)
        issues = lint_config(load_config(temp_config))
        assert any(
            "moisture_content" in i.message and "manufacturing_process" in i.message
            for i in issues
        )

    def test_duplicate_ids_are_flagged(self, temp_config):
        def mutate(data):
            data["units"].append(dict(data["units"][0]))

        _rewrite(temp_config / "units.yaml", mutate)
        issues = lint_config(load_config(temp_config))
        assert any("duplicate" in i.message for i in issues)

    def test_unknown_mqtt_topic_placeholder(self, temp_config):
        def mutate(data):
            for sink in data["sinks"]:
                if sink["type"] == "mqtt":
                    sink["mqtt"]["telemetry_topic"] = "pharma/{not_a_field}/x"

        _rewrite(temp_config / "sinks.yaml", mutate)
        issues = lint_config(load_config(temp_config))
        assert any("not_a_field" in i.message for i in issues)

    def test_unknown_scenario_action_type(self, temp_config):
        def mutate(data):
            data["scenarios"][0]["actions"] = [{"type": "teleport", "at_hours": 1.0}]

        _rewrite(temp_config / "scenarios.yaml", mutate)
        issues = lint_config(load_config(temp_config))
        assert any("teleport" in i.message for i in issues)


class TestTransfer:
    def test_evaluates_a_polynomial(self):
        transfer = Transfer(
            intercept=1.0, terms=[Term(input="x", coef=2.0), Term(input="y", coef=-0.5)]
        )
        assert transfer.evaluate({"x": 3.0, "y": 4.0}) == pytest.approx(1.0 + 6.0 - 2.0)

    def test_missing_input_falls_back_to_its_reference(self):
        transfer = Transfer(intercept=0.0, terms=[Term(input="x", coef=2.0)])
        assert transfer.evaluate({}, {"x": 5.0}) == pytest.approx(10.0)

    def test_missing_input_with_no_reference_is_skipped(self):
        transfer = Transfer(intercept=7.0, terms=[Term(input="x", coef=2.0)])
        assert transfer.evaluate({}) == pytest.approx(7.0)

    def test_clipping_is_applied(self):
        transfer = Transfer(
            intercept=100.0, terms=[Term(input="x", coef=1.0)], clip_max=50.0
        )
        assert transfer.evaluate({"x": 10.0}) == pytest.approx(50.0)

    def test_powers_are_honoured(self):
        transfer = Transfer(terms=[Term(input="x", coef=1.0, power=2.0)])
        assert transfer.evaluate({"x": 4.0}) == pytest.approx(16.0)


class TestJsonSchema:
    def test_schema_is_emitted_for_every_config_file(self, tmp_path):
        from pharma_sim.__main__ import main

        code = main(["schema", "--output", str(tmp_path)])
        assert code == 0
        files = sorted(p.name for p in tmp_path.glob("*.schema.json"))
        assert "plant.schema.json" in files
        assert "sensors.schema.json" in files
        # Emitted schemas must be valid JSON with properties.
        payload = json.loads((tmp_path / "plant.schema.json").read_text())
        assert "properties" in payload
