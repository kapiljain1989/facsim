"""PostgreSQL, TimescaleDB and ClickHouse backends.

Skipped unless the services are reachable, so the default suite stays
Docker-free. Bring them up with ``docker compose up -d postgres clickhouse`` and
install the extras to run these.

The important test here is parity: the same seeded run through SQLite+Parquet and
through Postgres+ClickHouse must produce equivalent results. Three backends that
quietly disagree would be worse than one.
"""

from __future__ import annotations

import os
import socket
from datetime import datetime

import pytest

PG_DSN = os.environ.get("PHARMA_TEST_PG_DSN", "postgresql://pharma:pharma@localhost:5432/pharma")
CH_DSN = os.environ.get("PHARMA_TEST_CH_DSN", "clickhouse://default@localhost:9000/pharma_ts")
NOW = datetime(2026, 1, 1, 6, 0, 0)


def _reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _have_postgres() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return _reachable("localhost", 5432)


def _have_clickhouse() -> bool:
    try:
        import clickhouse_driver  # noqa: F401
    except ImportError:
        return False
    return _reachable("localhost", 9000)


postgres = pytest.mark.skipif(
    not _have_postgres(), reason="PostgreSQL not reachable or psycopg not installed"
)
clickhouse = pytest.mark.skipif(
    not _have_clickhouse(),
    reason="ClickHouse not reachable or clickhouse-driver not installed",
)


def _storage(transactional: str, timeseries: str, tmp_path, *, evaluation: str = "parquet"):
    from pharma_sim.config.models import (
        EvaluationStorage,
        StorageConfig,
        TimeseriesStorage,
        TransactionalStorage,
    )

    return StorageConfig(
        transactional=TransactionalStorage(
            backend=transactional,
            dsn=PG_DSN if transactional == "postgres" else str(tmp_path / "f.db"),
            schema_name="oltp_test",
            batch_size=500,
        ),
        timeseries=TimeseriesStorage(
            backend=timeseries,
            dsn=(
                CH_DSN
                if timeseries == "clickhouse"
                else PG_DSN
                if timeseries == "timescale"
                else str(tmp_path / "ts")
            ),
            database="pharma_ts_test",
            schema_name="ts_test",
            partition_by=["date"],
            batch_size=20_000,
        ),
        evaluation=EvaluationStorage(
            backend=evaluation,
            dsn=PG_DSN if evaluation == "postgres" else str(tmp_path / "eval"),
            schema_name="eval_test",
        ),
    )


def _run(tmp_path, storage_config, *, hours: float = 8.0, seed: int = 42):
    import shutil

    from pharma_sim.config.loader import load_config
    from pharma_sim.simulator import Simulator
    from tests.conftest import CONFIG_DIR, _apply_test_profile

    config_dir = tmp_path / "config"
    if not config_dir.exists():
        shutil.copytree(CONFIG_DIR, config_dir)
    config = load_config(config_dir)
    object.__setattr__(config, "storage", storage_config)
    _apply_test_profile(config)
    sim = Simulator(
        config_dir,
        config=config,
        seed=seed,
        reset_storage=True,
        configure_logs=False,
        log_level="ERROR",
    )
    sim.run(hours=hours)
    return sim


@postgres
class TestPostgres:
    def test_schema_is_created_and_populated(self, tmp_path):
        sim = _run(tmp_path, _storage("postgres", "parquet", tmp_path))
        try:
            store = sim.storage.relational
            assert store.count("machines") == sim.plant.machine_count
            assert store.count("sensors") > 0
            assert store.count("events") > 0
        finally:
            sim.close()

    def test_foreign_keys_are_enforced(self, tmp_path):
        import psycopg

        sim = _run(tmp_path, _storage("postgres", "parquet", tmp_path), hours=2)
        try:
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                sim.storage.relational.upsert(
                    "machines",
                    [
                        {
                            "machine_id": "GHOST",
                            "unit_id": "NO_SUCH_UNIT",
                            "plant_id": sim.plant.plant_id,
                            "equipment_class": "tablet_press",
                        }
                    ],
                )
        finally:
            try:
                sim.close()
            except Exception:
                pass  # the aborted transaction is expected

    def test_jsonb_payloads_round_trip(self, tmp_path):
        sim = _run(tmp_path, _storage("postgres", "parquet", tmp_path), hours=4)
        try:
            rows = sim.storage.relational.query(
                "SELECT payload FROM oltp_test.events "
                "WHERE payload IS NOT NULL LIMIT 5"
            )
            assert rows
            assert isinstance(rows[0]["payload"], dict)
        finally:
            sim.close()

    def test_evaluation_lands_in_its_own_schema(self, tmp_path):
        sim = _run(
            tmp_path,
            _storage("postgres", "parquet", tmp_path, evaluation="postgres"),
            hours=6,
        )
        try:
            rows = sim.storage.relational.query(
                "SELECT schemaname FROM pg_tables "
                "WHERE tablename = 'prediction_labels' AND schemaname = 'eval_test'"
            )
            assert rows, "labels were not written to the evaluation schema"
            # And the operational schema must not hold it.
            operational = sim.storage.relational.query(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'oltp_test' AND tablename = 'prediction_labels'"
            )
            assert not operational
        finally:
            sim.close()

    def test_reconciler_adds_a_column(self, tmp_path):
        from pharma_sim.storage.postgres_store import PostgresStore

        store = PostgresStore(PG_DSN, schema="recon_test", reset=True)
        store.initialise()
        try:
            with store._connect().cursor() as cursor:
                cursor.execute("ALTER TABLE recon_test.plants DROP COLUMN location")
            store._connect().commit()

            again = PostgresStore(PG_DSN, schema="recon_test", reset=False)
            again.initialise()
            try:
                columns = {
                    row["column_name"]
                    for row in again.query(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'recon_test' AND table_name = 'plants'"
                    )
                }
                assert "location" in columns
            finally:
                again.close()
        finally:
            store.close()


@clickhouse
class TestClickHouse:
    def test_telemetry_is_written_and_queryable(self, tmp_path):
        sim = _run(tmp_path, _storage("sqlite", "clickhouse", tmp_path))
        try:
            store = sim.storage.telemetry
            store.flush()
            assert store.stored_count() == sim.telemetry.stats.readings
            client = store._connect()
            rows = client.execute(
                "SELECT tag, count() FROM pharma_ts_test.sensor_readings "
                "GROUP BY tag ORDER BY tag LIMIT 5"
            )
            assert rows
        finally:
            sim.close()

    def test_compression_is_effective(self, tmp_path):
        """The reason to pick a columnar store for this shape."""
        sim = _run(tmp_path, _storage("sqlite", "clickhouse", tmp_path), hours=12)
        try:
            store = sim.storage.telemetry
            store.flush()
            rows = store.stored_count()
            size = store.compressed_bytes()
            assert rows > 10_000
            assert size > 0
            per_row = size / rows
            assert per_row < 20.0, f"{per_row:.1f} bytes/reading is worse than expected"
        finally:
            sim.close()

    def test_rollup_view_is_maintained(self, tmp_path):
        sim = _run(tmp_path, _storage("sqlite", "clickhouse", tmp_path), hours=6)
        try:
            sim.storage.telemetry.flush()
            client = sim.storage.telemetry._connect()
            rows = client.execute("SELECT count() FROM pharma_ts_test.sensor_readings_1m")
            assert rows[0][0] > 0
        finally:
            sim.close()

    def test_cross_store_integrity_holds(self, tmp_path):
        sim = _run(tmp_path, _storage("sqlite", "clickhouse", tmp_path), hours=6)
        try:
            report = sim.storage.verify_integrity()
            assert report.ok, report.render()
        finally:
            sim.close()


@postgres
@clickhouse
class TestPolyglot:
    def test_full_production_shape_runs(self, tmp_path):
        sim = _run(
            tmp_path,
            _storage("postgres", "clickhouse", tmp_path, evaluation="postgres"),
            hours=8,
        )
        try:
            assert sim.storage.relational.count("machines") == sim.plant.machine_count
            sim.storage.telemetry.flush()
            assert sim.storage.telemetry.stored_count() > 0
            report = sim.storage.verify_integrity()
            assert report.ok, report.render()
        finally:
            sim.close()

    def test_backends_do_not_diverge(self, tmp_path):
        """Parity: one seeded run must mean the same thing in either backend."""
        default_path = tmp_path / "default"
        default_path.mkdir()
        polyglot_path = tmp_path / "polyglot"
        polyglot_path.mkdir()

        first = _run(default_path, _storage("sqlite", "parquet", default_path), hours=8)
        second = _run(
            polyglot_path, _storage("postgres", "clickhouse", polyglot_path), hours=8
        )
        try:
            for table in ("machines", "sensors", "batches", "qc_results", "events"):
                assert first.storage.relational.count(table) == (
                    second.storage.relational.count(table)
                ), f"{table} row count differs between backends"

            first.storage.telemetry.flush()
            second.storage.telemetry.flush()
            assert first.telemetry.stats.readings == second.telemetry.stats.readings

            assert first.status()["batches"] == second.status()["batches"]
            assert first.status()["reliability"] == second.status()["reliability"]
        finally:
            first.close()
            second.close()


@postgres
class TestTimescale:
    def test_hypertable_is_created_and_written(self, tmp_path):
        sim = _run(tmp_path, _storage("sqlite", "timescale", tmp_path), hours=6)
        try:
            store = sim.storage.telemetry
            store.flush()
            assert store.stored_count() == sim.telemetry.stats.readings
            assert "timescale" in store.describe
        finally:
            sim.close()

    def test_integrity_holds_for_timescale(self, tmp_path):
        sim = _run(tmp_path, _storage("sqlite", "timescale", tmp_path), hours=4)
        try:
            report = sim.storage.verify_integrity()
            assert report.ok, report.render()
        finally:
            sim.close()
