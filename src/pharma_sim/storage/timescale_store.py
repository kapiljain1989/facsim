"""TimescaleDB time-series backend.

The alternative to ClickHouse when you would rather keep one database. Because
Postgres is already deployed for the transactional store, this costs no extra
infrastructure, and it buys something ClickHouse cannot: a telemetry row can be
joined to a batch or a machine in ordinary SQL, with a real foreign key.

The trade is scan speed at very high row counts, which is why ClickHouse is the
default production choice for the time-series shape.

Requires the ``postgres`` extra: ``uv pip install -e ".[postgres]"``.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

__all__ = ["TimescaleTelemetryStore"]

logger = logging.getLogger(__name__)


class TimescaleTelemetryStore:
    """Telemetry as a compressed hypertable with continuous aggregates."""

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "ts",
        table: str = "sensor_readings",
        batch_size: int = 50_000,
        reset: bool = False,
        chunk_interval: str = "1 day",
    ) -> None:
        self._dsn = dsn
        self._schema = schema
        self._table = table
        self._batch_size = batch_size
        self._reset = reset
        self._chunk_interval = chunk_interval
        self._conn: psycopg.Connection | None = None
        self._buffer: list[tuple] = []
        self._rows = 0
        self._machines: set[str] = set()
        self._sensors: set[str] = set()
        self._hypertable = False

    @property
    def describe(self) -> str:
        kind = "hypertable" if self._hypertable else "plain table (timescaledb absent)"
        return f"timescale:{self._schema}.{self._table} [{kind}]"

    def _connect(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._dsn, autocommit=False)
        return self._conn

    # ------------------------------------------------------------------ schema
    def initialise(self) -> None:
        connection = self._connect()
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {self._schema}")
            if self._reset:
                cursor.execute(f"DROP TABLE IF EXISTS {self._schema}.{self._table} CASCADE")
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._schema}.{self._table} (
                  ts          TIMESTAMPTZ       NOT NULL,
                  machine_id  TEXT              NOT NULL,
                  sensor_id   TEXT              NOT NULL,
                  tag         TEXT              NOT NULL,
                  value       DOUBLE PRECISION,
                  unit        TEXT,
                  quality     SMALLINT,
                  unit_id     TEXT,
                  run_id      TEXT
                )
                """
            )
            self._hypertable = self._try_hypertable(cursor)
            cursor.execute(
                f"CREATE INDEX IF NOT EXISTS ix_{self._table}_sensor_ts "
                f"ON {self._schema}.{self._table} (sensor_id, ts DESC)"
            )
        connection.commit()

    def _try_hypertable(self, cursor) -> bool:
        """Convert to a hypertable, tolerating a plain Postgres without the extension.

        Falling back keeps the backend usable against stock Postgres rather than
        failing outright; ``describe`` reports which mode is in force so the
        difference is never silent.
        """
        try:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            cursor.execute(
                "SELECT create_hypertable(%s, 'ts', chunk_time_interval => %s::interval, "
                "if_not_exists => TRUE, migrate_data => TRUE)",
                (f"{self._schema}.{self._table}", self._chunk_interval),
            )
            cursor.execute(
                f"ALTER TABLE {self._schema}.{self._table} SET ("
                f"timescaledb.compress, "
                f"timescaledb.compress_segmentby = 'sensor_id', "
                f"timescaledb.compress_orderby = 'ts DESC')"
            )
            cursor.execute(
                "SELECT add_compression_policy(%s, INTERVAL '7 days', if_not_exists => TRUE)",
                (f"{self._schema}.{self._table}",),
            )
            self._create_continuous_aggregate(cursor)
            return True
        except Exception as exc:
            logger.warning(
                "timescaledb unavailable (%s); using a plain indexed table instead", exc
            )
            cursor.connection.rollback()
            return False

    def _create_continuous_aggregate(self, cursor) -> None:
        """Per-minute rollups maintained by the database."""
        cursor.execute(
            f"""
            CREATE MATERIALIZED VIEW IF NOT EXISTS {self._schema}.{self._table}_1m
            WITH (timescaledb.continuous) AS
            SELECT time_bucket('1 minute', ts) AS bucket,
                   machine_id, sensor_id, tag,
                   avg(value)    AS value_avg,
                   min(value)    AS value_min,
                   max(value)    AS value_max,
                   stddev(value) AS value_stddev,
                   count(*)      AS sample_count
            FROM {self._schema}.{self._table}
            GROUP BY bucket, machine_id, sensor_id, tag
            WITH NO DATA
            """
        )

    # ------------------------------------------------------------------ writes
    _QUALITY = {"GOOD": 1, "UNCERTAIN": 2, "BAD": 3}

    def append(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        for row in rows:
            self._machines.add(row["machine_id"])
            self._sensors.add(row["sensor_id"])
            self._buffer.append(
                (
                    row["timestamp"],
                    row["machine_id"],
                    row["sensor_id"],
                    row["tag"],
                    float(row["value"]),
                    row.get("unit") or "",
                    self._QUALITY.get(row.get("quality") or "GOOD", 1),
                    row.get("unit_id") or "",
                    row.get("run_id") or "",
                )
            )
        self._rows += len(rows)
        if len(self._buffer) >= self._batch_size:
            self.flush()
        return len(rows)

    def flush(self) -> None:
        if not self._buffer:
            return
        connection = self._connect()
        # COPY rather than INSERT: at tens of millions of rows the difference is
        # the difference between minutes and hours.
        with connection.cursor() as cursor:
            with cursor.copy(
                f"COPY {self._schema}.{self._table} "
                f"(ts, machine_id, sensor_id, tag, value, unit, quality, unit_id, run_id) "
                f"FROM STDIN"
            ) as copy:
                for record in self._buffer:
                    copy.write_row(record)
        connection.commit()
        self._buffer.clear()

    def close(self) -> None:
        self.flush()
        if self._conn is not None and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def count(self) -> int:
        return self._rows

    def stored_count(self) -> int:
        connection = self._connect()
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT count(*) FROM {self._schema}.{self._table}")
            row = cursor.fetchone()
        return int(row[0]) if row else 0

    def distinct(self, column: str) -> set[str]:
        if column == "machine_id":
            return set(self._machines)
        if column == "sensor_id":
            return set(self._sensors)
        raise KeyError(f"distinct() supports machine_id and sensor_id, not {column!r}")
