"""Storage: schema, foreign keys, reconciliation, telemetry and isolation."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from pharma_sim.storage.facade import StorageFacade
from pharma_sim.storage.parquet_store import ParquetEvaluationStore, ParquetTelemetryStore
from pharma_sim.storage.schema import EVAL_TABLES, TABLE_ORDER, TABLES, TELEMETRY_TABLE
from pharma_sim.storage.sqlite_store import SqliteStore

NOW = datetime(2026, 1, 1, 6, 0, 0)


@pytest.fixture
def store(tmp_path) -> SqliteStore:
    store = SqliteStore(str(tmp_path / "f.db"))
    store.initialise()
    yield store
    store.close()


def _seed_minimum(store: SqliteStore) -> None:
    """Just enough dimension rows to satisfy the foreign keys."""
    store.upsert("config_versions", [{"fingerprint": "abc", "created_at": NOW}])
    store.upsert(
        "runs", [{"run_id": "RUN-1", "config_fingerprint": "abc", "seed": 42}]
    )
    store.upsert("plants", [{"plant_id": "P1", "name": "Test"}])
    store.upsert("units", [{"unit_id": "U1", "plant_id": "P1", "name": "Unit"}])
    store.upsert(
        "equipment_classes", [{"equipment_class": "press", "name": "Press"}]
    )
    store.upsert(
        "machines",
        [{"machine_id": "M1", "unit_id": "U1", "plant_id": "P1", "equipment_class": "press"}],
    )
    store.upsert("products", [{"product_id": "PR1", "product_name": "Product"}])


class TestSchema:
    def test_every_table_in_the_order_exists(self):
        for name in TABLE_ORDER:
            assert name in TABLES, f"{name} is ordered but not declared"

    def test_every_declared_table_is_ordered(self):
        for name in TABLES:
            assert name in TABLE_ORDER, f"{name} is declared but not ordered"

    def test_foreign_keys_point_at_real_columns(self):
        for table in TABLES.values():
            for column in table.columns:
                if not column.references:
                    continue
                target, target_column = column.references.split(".")
                assert target in TABLES, f"{table.name}.{column.name} -> {target}"
                assert TABLES[target].column(target_column) is not None

    def test_dimensions_precede_the_facts_that_reference_them(self):
        """Buffered writes flush in this order, so it must be topological."""
        position = {name: index for index, name in enumerate(TABLE_ORDER)}
        for table in TABLES.values():
            for column in table.columns:
                if not column.references:
                    continue
                target = column.references.split(".")[0]
                if target == table.name:
                    continue
                assert position[target] < position[table.name], (
                    f"{table.name} is flushed before {target}, which it references"
                )

    def test_every_table_has_a_key(self):
        for table in TABLES.values():
            assert table.key_columns, f"{table.name} has no primary key"

    def test_failures_table_hides_the_answer(self):
        """§25: the operational record must not carry the mode or root cause."""
        columns = set(TABLES["failures"].column_names)
        assert "failure_mode" not in columns
        assert "root_cause" not in columns
        assert {"category", "symptom", "severity"} <= columns

    def test_ground_truth_carries_the_answer(self):
        columns = set(EVAL_TABLES["ground_truth_events"].column_names)
        assert {"failure_mode", "root_cause"} <= columns

    def test_telemetry_is_narrow_and_key_free(self):
        """A new sensor tag must never require DDL."""
        assert set(TELEMETRY_TABLE.column_names) == {
            "timestamp",
            "machine_id",
            "sensor_id",
            "tag",
            "value",
            "unit",
            "quality",
            "unit_id",
            "run_id",
        }
        assert all(column.references is None for column in TELEMETRY_TABLE.columns)


class TestSqliteStore:
    def test_creates_every_table(self, store):
        names = {
            row["name"]
            for row in store.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert set(TABLES) <= names

    def test_round_trip(self, store):
        _seed_minimum(store)
        assert store.count("machines") == 1
        rows = store.query("SELECT * FROM machines")
        assert rows[0]["machine_id"] == "M1"

    def test_upsert_replaces_by_primary_key(self, store):
        _seed_minimum(store)
        store.upsert(
            "machines",
            [{"machine_id": "M1", "unit_id": "U1", "plant_id": "P1",
              "equipment_class": "press", "name": "Renamed"}],
        )
        assert store.count("machines") == 1
        assert store.query("SELECT name FROM machines")[0]["name"] == "Renamed"

    def test_foreign_keys_are_enforced(self, store):
        """§40 has to be a guarantee, not a convention."""
        _seed_minimum(store)
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert(
                "machines",
                [{"machine_id": "M2", "unit_id": "GHOST", "plant_id": "P1",
                  "equipment_class": "press"}],
            )

    def test_orphan_qc_result_is_rejected(self, store):
        _seed_minimum(store)
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert(
                "qc_results",
                [{"test_id": "T1", "batch_id": "NO_SUCH_BATCH", "product_id": "PR1",
                  "parameter": "assay"}],
            )

    def test_orphan_rca_is_rejected(self, store):
        _seed_minimum(store)
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert(
                "rca", [{"rca_id": "R1", "deviation_id": "NO_SUCH_DEVIATION"}]
            )

    def test_unknown_table_is_rejected(self, store):
        with pytest.raises(KeyError):
            store.upsert("not_a_table", [{"x": 1}])

    def test_types_are_encoded(self, store):
        """Datetimes, booleans and nested payloads all become storable values."""
        _seed_minimum(store)
        store.upsert("event_types", [{"event_type": "MACHINE_STARTED", "category": "MACHINE"}])
        store.upsert(
            "events",
            [{"event_id": "E1", "timestamp": NOW, "event_type": "MACHINE_STARTED",
              "payload": {"nested": [1, 2]}, "run_id": "RUN-1", "plant_id": "P1"}],
        )
        row = store.query("SELECT * FROM events")[0]
        assert "2026-01-01" in row["timestamp"]
        assert '"nested"' in row["payload"]

        store.upsert(
            "sensors",
            [{"sensor_id": "M1:t", "machine_id": "M1", "unit_id": "U1", "plant_id": "P1",
              "tag": "t", "is_process_parameter": True}],
        )
        assert store.query("SELECT is_process_parameter FROM sensors")[0][
            "is_process_parameter"
        ] == 1

    def test_not_null_columns_are_enforced(self, store):
        _seed_minimum(store)
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert(
                "events",
                [{"event_id": "E2", "timestamp": NOW, "event_type": None,
                  "run_id": "RUN-1", "plant_id": "P1"}],
            )

    def test_schema_reconciler_adds_a_missing_column(self, tmp_path):
        """A config change that adds a field must not require dropping the DB."""
        path = tmp_path / "f.db"
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE plants (plant_id TEXT PRIMARY KEY, name TEXT)")
        connection.commit()
        connection.close()

        store = SqliteStore(str(path))
        store.initialise()
        try:
            columns = {row["name"] for row in store.query("PRAGMA table_info(plants)")}
            assert "location" in columns  # added by reconciliation
            assert "timezone" in columns
        finally:
            store.close()

    def test_existing_data_survives_reconciliation(self, tmp_path):
        path = tmp_path / "f.db"
        first = SqliteStore(str(path))
        first.initialise()
        first.upsert("plants", [{"plant_id": "P1", "name": "Keep me"}])
        first.close()

        second = SqliteStore(str(path))
        second.initialise()
        try:
            assert second.count("plants") == 1
        finally:
            second.close()


class TestParquetStores:
    def test_telemetry_round_trip(self, tmp_path):
        import pyarrow.parquet as pq

        store = ParquetTelemetryStore(str(tmp_path / "ts"), batch_size=10)
        store.initialise()
        rows = [
            {
                "timestamp": NOW,
                "machine_id": "M1",
                "sensor_id": "M1:vibration",
                "tag": "vibration",
                "value": 2.0 + index / 100,
                "unit": "mm/s",
                "quality": "GOOD",
                "unit_id": "U1",
                "run_id": "RUN-1",
            }
            for index in range(55)
        ]
        assert store.append(rows) == 55
        store.close()

        files = store.files()
        assert files
        table = pq.read_table(files[0])
        assert table.num_rows == 55
        assert set(table.column_names) == set(TELEMETRY_TABLE.column_names)

    def test_partitions_by_date(self, tmp_path):
        store = ParquetTelemetryStore(str(tmp_path / "ts"), partition_by=("date",))
        store.initialise()
        for day in (1, 2, 3):
            store.append(
                [
                    {
                        "timestamp": datetime(2026, 1, day, 8, 0),
                        "machine_id": "M1",
                        "sensor_id": "M1:t",
                        "tag": "t",
                        "value": 1.0,
                        "unit": "",
                        "quality": "GOOD",
                        "unit_id": "U1",
                        "run_id": "R",
                    }
                ]
            )
        store.close()
        partitions = {p.parent.name for p in store.files()}
        assert partitions == {"date=2026-01-01", "date=2026-01-02", "date=2026-01-03"}

    def test_distinct_tracks_identities_for_the_integrity_check(self, tmp_path):
        store = ParquetTelemetryStore(str(tmp_path / "ts"))
        store.initialise()
        store.append(
            [
                {
                    "timestamp": NOW,
                    "machine_id": mid,
                    "sensor_id": f"{mid}:t",
                    "tag": "t",
                    "value": 1.0,
                    "unit": "",
                    "quality": "GOOD",
                    "unit_id": "U1",
                    "run_id": "R",
                }
                for mid in ("M1", "M2", "M1")
            ]
        )
        assert store.distinct("machine_id") == {"M1", "M2"}
        assert store.distinct("sensor_id") == {"M1:t", "M2:t"}
        store.close()

    def test_memory_stays_bounded_across_many_flushes(self, tmp_path):
        """Chunked row groups: a long run must not accumulate rows in memory."""
        store = ParquetTelemetryStore(str(tmp_path / "ts"), batch_size=100)
        store.initialise()
        for chunk in range(20):
            store.append(
                [
                    {
                        "timestamp": NOW,
                        "machine_id": "M1",
                        "sensor_id": "M1:t",
                        "tag": "t",
                        "value": float(chunk),
                        "unit": "",
                        "quality": "GOOD",
                        "unit_id": "U1",
                        "run_id": "R",
                    }
                ]
                * 100
            )
        assert store.count() == 2000
        store.close()

    def test_evaluation_store_writes_both_tables(self, tmp_path):
        store = ParquetEvaluationStore(str(tmp_path / "eval"))
        store.initialise()
        store.append(
            "ground_truth_events",
            [{"ground_truth_id": "GT-1", "episode_id": "EP-1", "machine_id": "M1",
              "failure_mode": "BEARING_FAILURE", "root_cause": "INSUFFICIENT_LUBRICATION"}],
        )
        store.append(
            "prediction_labels",
            [{"machine_id": "M1", "timestamp": NOW, "rul_hours": 12.0,
              "will_fail_24h": True}],
        )
        store.close()
        names = {p.parent.name for p in store.files()}
        assert names == {"ground_truth_events", "prediction_labels"}

    def test_unknown_evaluation_table_is_rejected(self, tmp_path):
        store = ParquetEvaluationStore(str(tmp_path / "eval"))
        store.initialise()
        with pytest.raises(KeyError):
            store.append("not_a_table", [{"x": 1}])
        store.close()


class TestFacade:
    @pytest.fixture
    def facade(self, tmp_path):
        facade = StorageFacade(
            SqliteStore(str(tmp_path / "f.db")),
            ParquetTelemetryStore(str(tmp_path / "ts")),
            ParquetEvaluationStore(str(tmp_path / "eval")),
            flush_threshold=1000,
        )
        facade.initialise()
        yield facade
        facade.close()

    def test_routes_each_table_to_the_right_store(self, facade):
        _seed_minimum(facade.relational)
        facade.write("plants", {"plant_id": "P2", "name": "Second"})
        facade.write(
            "sensor_readings",
            {"timestamp": NOW, "machine_id": "M1", "sensor_id": "M1:t", "tag": "t",
             "value": 1.0, "unit": "", "quality": "GOOD", "unit_id": "U1", "run_id": "RUN-1"},
        )
        facade.write(
            "ground_truth_events",
            {"ground_truth_id": "GT-1", "episode_id": "EP-1", "machine_id": "M1"},
        )
        facade.flush()
        assert facade.relational.count("plants") == 2
        assert facade.telemetry.count() == 1
        assert facade.evaluation.count("ground_truth_events") == 1

    def test_flushes_in_dependency_order(self, facade):
        """A fact and its dimension buffered together must still insert cleanly."""
        facade.write("config_versions", {"fingerprint": "abc", "created_at": NOW})
        facade.write("runs", {"run_id": "R1", "config_fingerprint": "abc", "seed": 1})
        facade.write("plants", {"plant_id": "P1", "name": "P"})
        facade.write("units", {"unit_id": "U1", "plant_id": "P1"})
        facade.write("equipment_classes", {"equipment_class": "c"})
        facade.write(
            "machines",
            {"machine_id": "M1", "unit_id": "U1", "plant_id": "P1", "equipment_class": "c"},
        )
        # Written last but flushed after its dimensions.
        facade.write(
            "machine_events",
            {"event_id": "E1", "timestamp": NOW, "machine_id": "M1",
             "event_type": "MACHINE_STARTED", "run_id": "R1"},
        )
        facade.flush()
        assert facade.relational.count("machine_events") == 1

    def test_unknown_table_is_rejected(self, facade):
        with pytest.raises(KeyError):
            facade.write("nonsense", {"x": 1})

    def test_counts_are_reported(self, facade):
        _seed_minimum(facade.relational)
        facade.write("plants", {"plant_id": "P3", "name": "x"})
        assert facade.written()["plants"] == 1
        assert facade.total_written() >= 1

    def test_integrity_report_passes_on_consistent_data(self, facade):
        _seed_minimum(facade.relational)
        facade.write(
            "sensor_readings",
            {"timestamp": NOW, "machine_id": "M1", "sensor_id": "M1:t", "tag": "t",
             "value": 1.0, "unit": "", "quality": "GOOD", "unit_id": "U1", "run_id": "RUN-1"},
        )
        facade.relational.upsert(
            "sensors",
            [{"sensor_id": "M1:t", "machine_id": "M1", "unit_id": "U1", "plant_id": "P1",
              "tag": "t"}],
        )
        report = facade.verify_integrity()
        assert report.ok, report.render()

    def test_integrity_report_catches_a_cross_store_orphan(self, facade):
        """The check that makes §40 true across a polyglot boundary."""
        _seed_minimum(facade.relational)
        facade.write(
            "sensor_readings",
            {"timestamp": NOW, "machine_id": "GHOST_MACHINE", "sensor_id": "GHOST:t",
             "tag": "t", "value": 1.0, "unit": "", "quality": "GOOD", "unit_id": "U1",
             "run_id": "RUN-1"},
        )
        report = facade.verify_integrity()
        assert not report.ok
        assert any("machine_id" in name for name, ok, _ in report.failures)
