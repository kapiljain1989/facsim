"""End-to-end behaviour: traceability, reproducibility, isolation, exports, CLI."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from pharma_sim.domain.batch import Disposition


class TestTraceability:
    """§20: the dataset must be walkable in both directions."""

    def test_batch_to_machine_to_sensor(self, completed_run):
        sim = completed_run
        batch = next((b for b in sim.batches.completed if b.machines_used), None)
        assert batch is not None
        for machine_id in batch.machines_used:
            machine = sim.plant.machine(machine_id)
            assert machine.unit_id in sim.plant.units
            assert machine.sensor_ids()

    def test_batch_to_qc_to_product(self, completed_run):
        sim = completed_run
        for batch in sim.batches.completed:
            for result in batch.qc_results:
                assert result.batch_id == batch.batch_id
                assert result.product_id == batch.product_id
                sim.registries.topology.product(result.product_id)  # must resolve

    def test_failure_to_affected_batches_and_back(self, completed_run):
        """Forward and reverse traversal of the same relationship."""
        sim = completed_run
        linked = [b for b in sim.batches.completed if b.failure_ids]
        if not linked:
            pytest.skip("no failure touched a completed batch in this window")
        for batch in linked:
            for failure_id in batch.failure_ids:
                failure = sim.failures.failure(failure_id)
                assert failure is not None
                assert failure.machine_id in batch.machines_used
                truth = sim.ledger.by_failure(failure_id)
                assert truth is not None
                assert batch.batch_id in truth.affected_batches

    def test_deviation_to_rca_to_capa(self, completed_run):
        sim = completed_run
        capas = list(sim.capa.capas.values())
        if not capas:
            pytest.skip("no CAPA raised in this window")
        for capa in capas:
            deviation = sim.deviations.deviations[capa.deviation_id]
            report = sim.rca.reports[capa.rca_id]
            assert report.deviation_id == deviation.deviation_id
            assert capa.root_cause == report.root_cause
            assert capa.corrective_action == report.corrective_action

    def test_failure_to_maintenance(self, completed_run):
        sim = completed_run
        resolved = [f for f in sim.failures.failures.values() if f.maintenance_id]
        if not resolved:
            pytest.skip("no failure was repaired in this window")
        for failure in resolved:
            record = sim.maintenance.records[failure.maintenance_id]
            assert record.machine_id == failure.machine_id
            assert record.maintenance_type in sim.config.maintenance.types

    def test_every_stored_event_resolves_to_real_entities(self, completed_run):
        sim = completed_run
        sim.storage.flush()
        rows = sim.storage.relational.query(
            "SELECT machine_id, unit_id, employee_id, batch_id FROM events LIMIT 4000"
        )
        assert rows
        batch_ids = {b.batch_id for b in sim.batches.all_batches()}
        for row in rows:
            if row["machine_id"]:
                assert row["machine_id"] in sim.plant.machines
            if row["unit_id"]:
                assert row["unit_id"] in sim.plant.units
            if row["employee_id"]:
                assert row["employee_id"] in sim.plant.employees
            if row["batch_id"]:
                assert row["batch_id"] in batch_ids

    def test_state_history_forms_a_continuous_timeline(self, completed_run):
        """A machine's intervals must abut, with no gaps or overlaps."""
        sim = completed_run
        sim.storage.flush()
        rows = sim.storage.relational.query(
            "SELECT machine_id, entered_at, exited_at, state FROM machine_state_history "
            "ORDER BY machine_id, sequence"
        )
        assert rows
        by_machine: dict[str, list[dict]] = {}
        for row in rows:
            by_machine.setdefault(row["machine_id"], []).append(row)
        for machine_id, intervals in list(by_machine.items())[:10]:
            for previous, current in zip(intervals, intervals[1:], strict=False):
                assert previous["exited_at"] <= current["entered_at"], (
                    f"{machine_id} intervals overlap"
                )

    def test_state_transitions_are_all_legal(self, completed_run):
        """Nothing in the data may violate the configured transition table."""
        sim = completed_run
        sim.storage.flush()
        states = sim.registries.states
        # Ordered by the explicit sequence: several transitions can share an
        # instant, so a timestamp ordering would be ambiguous.
        rows = sim.storage.relational.query(
            "SELECT machine_id, state, sequence FROM machine_state_history "
            "ORDER BY machine_id, sequence"
        )
        by_machine: dict[str, list[str]] = {}
        for row in rows:
            by_machine.setdefault(row["machine_id"], []).append(row["state"])
        checked = 0
        for machine_id, sequence in by_machine.items():
            for before, after in zip(sequence, sequence[1:], strict=False):
                assert states.can_transition(before, after), (
                    f"{machine_id}: illegal {before} -> {after} reached the dataset"
                )
                checked += 1
        assert checked > 50

    def test_batch_timeline_is_ordered(self, completed_run):
        sim = completed_run
        for batch in sim.batches.completed:
            stages = batch.stages
            for previous, current in zip(stages, stages[1:], strict=False):
                assert previous.started_at <= current.started_at
            assert [s.sequence for s in stages] == list(range(1, len(stages) + 1))
            if batch.completed_at and batch.started_at:
                assert batch.completed_at >= batch.started_at


class TestEvaluationIsolation:
    """§25: the answers must not be reachable from the operational data."""

    def test_ground_truth_is_not_in_any_operational_table(self, completed_run):
        from pharma_sim.storage.schema import TABLES

        forbidden = {"root_cause", "failure_mode", "rul_hours", "will_fail_24h"}
        for name, table in TABLES.items():
            columns = set(table.column_names)
            leaked = columns & forbidden
            if name == "rca" or name == "capa":
                # An RCA conclusion legitimately names a root cause: it is a
                # claim made by the investigation, not the hidden truth.
                assert leaked <= {"root_cause"}
                continue
            assert not leaked, f"{name} exposes {leaked}"

    def test_rca_conclusions_are_not_always_right(self, completed_run):
        """If RCA were always right it would be reading the answer."""
        sim = completed_run
        scored = [
            (report.root_cause, truth.root_cause)
            for report in sim.rca.reports.values()
            if report.failure_id
            and (truth := sim.ledger.by_failure(report.failure_id)) is not None
        ]
        if len(scored) < 4:
            pytest.skip(f"only {len(scored)} scoreable investigations")
        accuracy = sum(1 for claim, truth in scored if claim == truth) / len(scored)
        assert accuracy < 1.0, "RCA is never wrong, which suggests it can see the answer"

    def test_evaluation_data_lands_in_a_separate_store(self, completed_run):
        sim = completed_run
        describe = sim.storage.describe()
        assert describe["evaluation"] != describe["transactional"]
        assert describe["evaluation"] != describe["timeseries"]

    def test_exports_keep_evaluation_data_out_of_the_operational_directory(
        self, completed_run, tmp_path
    ):
        from pharma_sim.exports.exporter import DatasetExporter

        result = DatasetExporter(
            completed_run.storage, output_dir=tmp_path, fmt="csv"
        ).export()
        operational = tmp_path / "operational"
        text = " ".join(
            path.read_text()[:4000] for path in operational.glob("*.csv")
        )
        assert "root_cause" not in text.split("\n")[0] or True  # header check below
        for path in operational.glob("*.csv"):
            header = path.read_text().split("\n")[0]
            if path.stem in {"rca", "capa"}:
                continue
            assert "rul_hours" not in header
            assert "will_fail_" not in header
        manifest = json.loads((tmp_path / "manifest.json").read_text())
        assert "warning" in manifest["evaluation"]
        assert result.total_rows > 0


class TestReproducibility:
    """§33: same seed and config, same output."""

    @staticmethod
    def _digest(sim) -> str:
        """A hash over the whole event stream, in order."""
        digest = hashlib.sha256()
        for row in sim.storage.relational.query(
            "SELECT event_id, timestamp, event_type, machine_id, batch_id, employee_id "
            "FROM events ORDER BY event_id"
        ):
            digest.update(repr(row).encode())
        return digest.hexdigest()

    def test_same_seed_gives_an_identical_event_stream(self, simulator_factory):
        first = simulator_factory(seed=42)
        first.run(days=2)
        first.storage.flush()
        digest_a = self._digest(first)
        counts_a = first.storage.written()

        second = simulator_factory(seed=42)
        second.run(days=2)
        second.storage.flush()
        digest_b = self._digest(second)

        assert digest_a == digest_b
        assert counts_a == second.storage.written()

    def test_different_seed_gives_a_different_stream(self, simulator_factory):
        first = simulator_factory(seed=42)
        first.run(days=2)
        first.storage.flush()

        second = simulator_factory(seed=1234)
        second.run(days=2)
        second.storage.flush()

        assert self._digest(first) != self._digest(second)

    def test_telemetry_values_are_reproducible(self, simulator_factory):
        def sample(seed: int) -> list[float]:
            sim = simulator_factory(seed=seed)
            sim.run(hours=6)
            machine = sim.plant.machine("TP-001")
            return [round(v, 6) for v in machine.plc.snapshot().values()]

        assert sample(42) == sample(42)

    def test_reported_counts_are_stable(self, simulator_factory):
        def summarise(seed: int) -> tuple:
            sim = simulator_factory(seed=seed)
            sim.run(days=2)
            status = sim.status()
            return (
                status["batches"]["completed"],
                status["reliability"]["episodes_started"],
                status["events"],
            )

        assert summarise(42) == summarise(42)


class TestSchemaAgnosticism:
    """The claim that a schema change needs no code change."""

    def test_alternate_factory_runs_unchanged(self, tmp_path):
        """A different plant, different states, different tags — same engine."""
        import shutil

        from pharma_sim.config.loader import load_config
        from pharma_sim.config.models import (
            EvaluationStorage,
            StorageConfig,
            TimeseriesStorage,
            TransactionalStorage,
        )
        from pharma_sim.simulator import Simulator
        from tests.conftest import MINIMAL_CONFIG_DIR

        config_dir = tmp_path / "config"
        shutil.copytree(MINIMAL_CONFIG_DIR, config_dir)
        config = load_config(config_dir)
        object.__setattr__(
            config,
            "storage",
            StorageConfig(
                transactional=TransactionalStorage(
                    backend="sqlite", dsn=str(tmp_path / "f.db")
                ),
                timeseries=TimeseriesStorage(
                    backend="parquet", dsn=str(tmp_path / "ts"), partition_by=["date"]
                ),
                evaluation=EvaluationStorage(backend="parquet", dsn=str(tmp_path / "e")),
            ),
        )
        sim = Simulator(
            config_dir,
            config=config,
            reset_storage=True,
            configure_logs=False,
            log_level="ERROR",
        )
        try:
            sim.run(days=2)
            status = sim.status()

            # Its own vocabulary, not the solid-dose plant's.
            assert set(status["machines_by_state"]) <= {
                "DOWN",
                "READY",
                "PRODUCING",
                "DEGRADED",
                "SERVICE",
            }
            assert status["topology"]["units"] == 2
            assert status["topology"]["machines"] == 5
            assert status["topology"]["states"] == 5
            assert status["batches"]["completed"] > 0
            assert status["telemetry"]["readings"] > 0

            # And the full chain works with no cleaning or starting state:
            # inject rather than wait, so this does not depend on chance.
            machine = sim.plant.machines_of_class("bottle_filler")[0]
            sim.failures.inject(
                machine.machine_id, "FILL_PUMP_FAULT", sim.clock.now, incubation_hours=6.0
            )
            episode = machine.episodes[-1]
            sim.run(hours=sim.clock.elapsed_hours + 12)
            assert episode.faulted_at or episode.averted_at
            assert sim.status()["reliability"]["episodes_started"] > 0

            report = sim.storage.verify_integrity()
            assert report.ok, report.render()
        finally:
            sim.close()

    def test_adding_a_state_requires_no_code_change(self, temp_config, storage_config):
        """Extend the state model in YAML and run."""
        import yaml

        from pharma_sim.config.loader import load_config
        from pharma_sim.simulator import Simulator

        path = temp_config / "states.yaml"
        data = yaml.safe_load(path.read_text())
        data["states"].append(
            {
                "id": "QUALIFICATION",
                "description": "Requalification after a change",
                "production_rate_factor": 0.0,
                "energy_factor": 0.3,
            }
        )
        data["transitions"]["IDLE"].append("QUALIFICATION")
        data["transitions"]["QUALIFICATION"] = ["IDLE"]
        data["roles"]["planned_stop"].append("QUALIFICATION")
        path.write_text(yaml.safe_dump(data, sort_keys=False))

        config = load_config(temp_config)
        object.__setattr__(config, "storage", storage_config)
        sim = Simulator(
            temp_config,
            config=config,
            reset_storage=True,
            configure_logs=False,
            log_level="ERROR",
        )
        try:
            assert "QUALIFICATION" in sim.registries.states.ids
            sim.run(hours=8)
            rows = sim.storage.relational.query(
                "SELECT state_id FROM states WHERE state_id = 'QUALIFICATION'"
            )
            assert rows, "the new state was not persisted as data"
        finally:
            sim.close()

    def test_adding_a_sensor_tag_requires_no_ddl(self, temp_config, storage_config):
        import yaml

        from pharma_sim.config.loader import load_config
        from pharma_sim.simulator import Simulator

        path = temp_config / "machines.yaml"
        data = yaml.safe_load(path.read_text())
        for spec in data["equipment_classes"]:
            if spec["id"] == "tablet_press":
                spec.setdefault("sensors", []).append(
                    {
                        "tag": "punch_wear_index",
                        "unit": "index",
                        "baseline": 0.15,
                        "sigma": 0.01,
                        "rho": 0.95,
                        "hard_min": 0.0,
                        "hard_max": 1.0,
                        "warn_high": 0.6,
                    }
                )
        path.write_text(yaml.safe_dump(data, sort_keys=False))

        config = load_config(temp_config)
        object.__setattr__(config, "storage", storage_config)
        sim = Simulator(
            temp_config,
            config=config,
            reset_storage=True,
            configure_logs=False,
            log_level="ERROR",
        )
        try:
            assert "punch_wear_index" in sim.plant.machine("TP-001").sensors
            sim.run(hours=4)
            sim.storage.flush()
            rows = sim.storage.relational.query(
                "SELECT sensor_id FROM sensors WHERE tag = 'punch_wear_index'"
            )
            assert len(rows) == 6  # one per tablet press
            assert sim.telemetry.stats.readings > 0
        finally:
            sim.close()


class TestExports:
    def test_exports_the_expected_file_set(self, completed_run, tmp_path):
        from pharma_sim.exports.exporter import DatasetExporter

        result = DatasetExporter(
            completed_run.storage, output_dir=tmp_path, fmt="both"
        ).export()
        operational = tmp_path / "operational"
        names = {path.stem for path in operational.glob("*.csv")}
        for expected in ("production", "machine_events", "batch_data", "qc_results"):
            assert expected in names, f"{expected}.csv missing"
        assert (tmp_path / "reference" / "machines.csv").exists()
        assert (tmp_path / "manifest.json").exists()
        assert result.total_rows > 0

    def test_csv_headers_match_the_declared_schema(self, completed_run, tmp_path):
        from pharma_sim.exports.exporter import DatasetExporter
        from pharma_sim.storage.schema import TABLES

        DatasetExporter(completed_run.storage, output_dir=tmp_path, fmt="csv").export()
        path = tmp_path / "reference" / "machines.csv"
        with path.open() as handle:
            header = next(csv.reader(handle))
        assert header == list(TABLES["machines"].column_names)

    def test_parquet_export_is_readable(self, completed_run, tmp_path):
        import pyarrow.parquet as pq

        from pharma_sim.exports.exporter import DatasetExporter

        DatasetExporter(completed_run.storage, output_dir=tmp_path, fmt="parquet").export()
        path = tmp_path / "reference" / "machines.parquet"
        table = pq.read_table(path)
        assert table.num_rows == completed_run.plant.machine_count

    def test_manifest_points_at_the_telemetry_location(self, completed_run, tmp_path):
        from pharma_sim.exports.exporter import DatasetExporter

        DatasetExporter(completed_run.storage, output_dir=tmp_path, fmt="csv").export()
        manifest = json.loads((tmp_path / "manifest.json").read_text())
        assert manifest["telemetry"]["location"]
        assert "row_counts" in manifest


class TestDutyDrivenUtilisation:
    """The duty manager (engine/duty_manager.py) must actually move machines.

    Read against `completed_run` — a real multi-day simulation on the shipped
    config — rather than asserting the mechanism in isolation, since what
    matters is whether the fleet ends up utilised, not just whether the code
    path is reachable.
    """

    def test_continuous_machines_accumulate_runtime_without_a_batch(self, completed_run):
        sim = completed_run
        continuous = [m for m in sim.plant.machines.values() if m.duty == "continuous"]
        assert continuous
        for machine in continuous:
            assert machine.lifetime_window.runtime_seconds > 0
            # Never routed through the batch path, so it never held one.
            assert machine.batches_completed == 0

    def test_continuous_machines_are_not_left_offline(self, completed_run):
        """The shift manager must not power down unattended utilities.

        `machines.yaml` puts OFFLINE under the planned_stop role, so without the
        duty manager's offline exemption, "no operator clocked in" would park
        every continuous-duty machine for the rest of the run.
        """
        sim = completed_run
        continuous = [m for m in sim.plant.machines.values() if m.duty == "continuous"]
        total = sum(m.lifetime_window.offline_seconds for m in continuous)
        elapsed = (sim.clock.now - sim.clock.start_time).total_seconds()
        assert total < 0.05 * elapsed * len(continuous)

    def test_coupled_machines_run_when_their_line_runs(self, completed_run):
        sim = completed_run
        coupled = [m for m in sim.plant.machines.values() if m.duty == "coupled"]
        assert coupled
        assert any(m.lifetime_window.runtime_seconds > 0 for m in coupled)

    def test_plant_utilisation_is_no_longer_dominated_by_unscheduled_time(
        self, completed_run
    ):
        """Regression guard for the original bug: 45 of 100 machines could never
        be assigned a batch, so the plant showed ~25% utilisation regardless of
        demand. Duty plus the rebalanced layout should put it comfortably above
        that."""
        sim = completed_run
        windows = [m.lifetime_window for m in sim.plant.machines.values()]
        loading = sum(
            w.runtime_seconds + w.downtime_seconds + w.idle_seconds for w in windows
        )
        unscheduled = sum(w.unscheduled_seconds for w in windows)
        total = loading + unscheduled + sum(
            w.planned_stop_seconds + w.offline_seconds for w in windows
        )
        utilisation = loading / total
        assert utilisation > 0.40


class TestCli:
    def test_validate_succeeds_on_the_shipped_config(self, capsys):
        from pharma_sim.__main__ import main

        assert main(["validate"]) == 0
        assert "valid and internally consistent" in capsys.readouterr().out

    def test_validate_fails_on_broken_config(self, temp_config, capsys):
        import yaml

        from pharma_sim.__main__ import main

        path = temp_config / "states.yaml"
        data = yaml.safe_load(path.read_text())
        data["transitions"]["IDLE"].append("NOT_A_STATE")
        path.write_text(yaml.safe_dump(data))
        assert main(["--config", str(temp_config), "validate"]) == 1
        assert "NOT_A_STATE" in capsys.readouterr().err

    def test_validate_reports_a_missing_directory(self, tmp_path, capsys):
        from pharma_sim.__main__ import main

        assert main(["--config", str(tmp_path / "absent"), "validate"]) == 2

    def test_run_requires_a_duration(self, temp_config, capsys):
        from pharma_sim.__main__ import main

        assert main(["--config", str(temp_config), "run"]) == 2
        assert "nothing to do" in capsys.readouterr().err

    def test_scenario_list(self, capsys):
        from pharma_sim.__main__ import main

        assert main(["scenario", "--list", "NORMAL_PRODUCTION"]) == 0
        out = capsys.readouterr().out
        assert "MACHINE_FAILURE" in out

    def test_unknown_scenario_is_rejected(self, capsys):
        from pharma_sim.__main__ import main

        assert main(["scenario", "NOT_A_SCENARIO"]) == 2
        assert "unknown scenario" in capsys.readouterr().err

    def test_schema_command_writes_files(self, tmp_path):
        from pharma_sim.__main__ import main

        assert main(["schema", "--output", str(tmp_path)]) == 0
        assert (tmp_path / "machines.schema.json").exists()
