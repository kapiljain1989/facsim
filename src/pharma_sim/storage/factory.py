"""Backend selection.

``storage.yaml`` names a backend per data shape and this module builds it. Driver
imports are deferred so the default SQLite + Parquet path needs neither psycopg
nor the ClickHouse driver installed.
"""

from __future__ import annotations

import logging

from pharma_sim.config.models import StorageConfig
from pharma_sim.storage.facade import StorageFacade
from pharma_sim.storage.parquet_store import ParquetEvaluationStore, ParquetTelemetryStore
from pharma_sim.storage.protocols import EvaluationStore, RelationalStore, TelemetryStore
from pharma_sim.storage.sqlite_store import SqliteStore

__all__ = ["build_storage", "MissingDriver"]

logger = logging.getLogger(__name__)


class MissingDriver(Exception):
    """Raised when a configured backend's driver is not installed."""

    def __init__(self, backend: str, package: str, extra: str) -> None:
        super().__init__(
            f"storage backend {backend!r} needs the {package!r} package. "
            f"Install it with:  uv pip install -e \".[{extra}]\""
        )


def _build_relational(config: StorageConfig, *, reset: bool) -> RelationalStore:
    backend = config.transactional.backend
    if backend == "sqlite":
        if reset:
            from pathlib import Path

            path = Path(config.transactional.dsn)
            if path.exists():
                path.unlink()
            for suffix in ("-wal", "-shm"):
                sidecar = path.with_name(path.name + suffix)
                if sidecar.exists():
                    sidecar.unlink()
        return SqliteStore(config.transactional.dsn)
    if backend == "postgres":
        try:
            from pharma_sim.storage.postgres_store import PostgresStore
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise MissingDriver("postgres", "psycopg", "postgres") from exc
        return PostgresStore(
            config.transactional.dsn,
            schema=config.transactional.schema_name,
            reset=reset,
        )
    raise ValueError(f"unsupported transactional backend {backend!r}")


def _build_telemetry(config: StorageConfig, *, reset: bool) -> TelemetryStore:
    backend = config.timeseries.backend
    if backend == "parquet":
        return ParquetTelemetryStore(
            config.timeseries.dsn,
            partition_by=tuple(config.timeseries.partition_by),
            batch_size=config.timeseries.batch_size,
            reset=reset,
        )
    if backend == "clickhouse":
        try:
            from pharma_sim.storage.clickhouse_store import ClickHouseTelemetryStore
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise MissingDriver("clickhouse", "clickhouse-driver", "clickhouse") from exc
        return ClickHouseTelemetryStore(
            config.timeseries.dsn,
            database=config.timeseries.database,
            table=config.timeseries.table,
            batch_size=config.timeseries.batch_size,
            reset=reset,
        )
    if backend == "timescale":
        try:
            from pharma_sim.storage.timescale_store import TimescaleTelemetryStore
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise MissingDriver("timescale", "psycopg", "postgres") from exc
        return TimescaleTelemetryStore(
            config.timeseries.dsn,
            schema=config.timeseries.schema_name,
            table=config.timeseries.table,
            batch_size=config.timeseries.batch_size,
            reset=reset,
        )
    raise ValueError(f"unsupported timeseries backend {backend!r}")


def _build_evaluation(config: StorageConfig, *, reset: bool) -> EvaluationStore:
    backend = config.evaluation.backend
    if backend == "parquet":
        return ParquetEvaluationStore(
            config.evaluation.dsn, batch_size=config.evaluation.batch_size, reset=reset
        )
    if backend == "postgres":
        try:
            from pharma_sim.storage.postgres_store import PostgresEvaluationStore
        except ImportError as exc:  # pragma: no cover
            raise MissingDriver("postgres", "psycopg", "postgres") from exc
        return PostgresEvaluationStore(
            config.evaluation.dsn, schema=config.evaluation.schema_name, reset=reset
        )
    if backend == "clickhouse":
        try:
            from pharma_sim.storage.clickhouse_store import ClickHouseEvaluationStore
        except ImportError as exc:  # pragma: no cover
            raise MissingDriver("clickhouse", "clickhouse-driver", "clickhouse") from exc
        return ClickHouseEvaluationStore(
            config.evaluation.dsn, database=config.evaluation.schema_name, reset=reset
        )
    raise ValueError(f"unsupported evaluation backend {backend!r}")


def build_storage(config: StorageConfig, *, reset: bool = False) -> StorageFacade:
    """Construct the three stores named in configuration and wrap them.

    Args:
        reset: drop existing data first. Used by ``init`` and by tests so a run
            starts from a known state.
    """
    facade = StorageFacade(
        _build_relational(config, reset=reset),
        _build_telemetry(config, reset=reset),
        _build_evaluation(config, reset=reset),
        flush_threshold=config.transactional.batch_size * 2,
        telemetry_threshold=max(1000, config.timeseries.batch_size // 2),
    )
    logger.info(
        "storage configured: transactional=%s timeseries=%s evaluation=%s",
        config.transactional.backend,
        config.timeseries.backend,
        config.evaluation.backend,
    )
    return facade
