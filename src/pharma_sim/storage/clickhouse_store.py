"""ClickHouse time-series backend — the production telemetry store.

Chosen for the shape of this data: narrow, append-only, and enormous. The table
below is tuned for how it will actually be read.

* ``ORDER BY (machine_id, sensor_id, ts)`` matches every real query — one tag on
  one machine over a window — and lets ClickHouse skip granules aggressively.
* Per-column codecs matter at this width: ``DoubleDelta`` on a slowly-varying
  sensor value compresses far better than generic ZSTD alone, and ``Delta`` suits
  a monotonic timestamp.
* ``LowCardinality`` on the id and enum columns replaces repeated strings with
  dictionary references, which is most of the on-disk win.
* A materialised view maintains per-minute rollups on insert, so a dashboard
  query does not scan raw rows.

Requires the ``clickhouse`` extra: ``uv pip install -e ".[clickhouse]"``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from clickhouse_driver import Client

from pharma_sim.storage.schema import EVAL_TABLES, TELEMETRY_TABLE, Table

__all__ = ["ClickHouseTelemetryStore", "ClickHouseEvaluationStore"]

logger = logging.getLogger(__name__)

_CH_TYPES = {
    "TEXT": "String",
    "INTEGER": "Int64",
    "REAL": "Float64",
    "BOOLEAN": "UInt8",
    "TIMESTAMP": "DateTime64(3)",
    "DATE": "Date",
    "JSON": "String",
}


def _parse_dsn(dsn: str) -> dict[str, Any]:
    """Accept ``clickhouse://user:pass@host:port/database`` or a bare host."""
    if "://" not in dsn:
        return {"host": dsn or "localhost", "port": 9000}
    parsed = urlparse(dsn)
    settings: dict[str, Any] = {
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 9000,
    }
    if parsed.username:
        settings["user"] = parsed.username
    if parsed.password:
        settings["password"] = parsed.password
    return settings


class ClickHouseTelemetryStore:
    """Columnar, compressed, partitioned telemetry."""

    def __init__(
        self,
        dsn: str,
        *,
        database: str = "pharma_ts",
        table: str = "sensor_readings",
        batch_size: int = 200_000,
        reset: bool = False,
        ttl_years: int = 2,
    ) -> None:
        self._settings = _parse_dsn(dsn)
        self._database = database
        self._table = table
        self._batch_size = batch_size
        self._reset = reset
        self._ttl_years = ttl_years
        self._client: Client | None = None
        self._buffer: list[tuple] = []
        self._rows = 0
        self._machines: set[str] = set()
        self._sensors: set[str] = set()

    @property
    def describe(self) -> str:
        return (
            f"clickhouse:{self._settings['host']}:{self._settings['port']}"
            f"/{self._database}.{self._table}"
        )

    def _connect(self) -> Client:
        if self._client is None:
            self._client = Client(**self._settings)
        return self._client

    # ------------------------------------------------------------------ schema
    def ddl(self) -> str:
        return f"""
CREATE TABLE IF NOT EXISTS {self._database}.{self._table} (
  ts          DateTime64(3)           CODEC(Delta, ZSTD(3)),
  machine_id  LowCardinality(String),
  sensor_id   LowCardinality(String),
  tag         LowCardinality(String),
  value       Float64                 CODEC(DoubleDelta, ZSTD(3)),
  unit        LowCardinality(String),
  quality     Enum8('GOOD' = 1, 'UNCERTAIN' = 2, 'BAD' = 3),
  unit_id     LowCardinality(String),
  run_id      LowCardinality(String)
) ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
ORDER BY (machine_id, sensor_id, ts)
TTL toDateTime(ts) + INTERVAL {self._ttl_years} YEAR
SETTINGS index_granularity = 8192
"""

    def rollup_ddl(self) -> str:
        """Per-minute aggregates, maintained on insert rather than on read."""
        return f"""
CREATE MATERIALIZED VIEW IF NOT EXISTS {self._database}.{self._table}_1m
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(minute)
ORDER BY (machine_id, sensor_id, minute)
AS SELECT
  toStartOfMinute(ts)      AS minute,
  machine_id,
  sensor_id,
  tag,
  avgState(value)          AS value_avg,
  minState(value)          AS value_min,
  maxState(value)          AS value_max,
  countState()             AS sample_count
FROM {self._database}.{self._table}
GROUP BY minute, machine_id, sensor_id, tag
"""

    def initialise(self) -> None:
        client = self._connect()
        client.execute(f"CREATE DATABASE IF NOT EXISTS {self._database}")
        if self._reset:
            client.execute(f"DROP TABLE IF EXISTS {self._database}.{self._table}_1m")
            client.execute(f"DROP TABLE IF EXISTS {self._database}.{self._table}")
        client.execute(self.ddl())
        client.execute(self.rollup_ddl())
        # Mirror the sensor dimension so telemetry is self-describing here too.
        client.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._database}.sensor_dimension (
              sensor_id  String,
              machine_id String,
              unit_id    String,
              plant_id   String,
              tag        String
            ) ENGINE = ReplacingMergeTree ORDER BY sensor_id
            """
        )

    def register_sensors(self, rows: list[dict[str, Any]]) -> None:
        """Replicate the sensor dimension, so a reading resolves locally."""
        if not rows:
            return
        self._connect().execute(
            f"INSERT INTO {self._database}.sensor_dimension "
            f"(sensor_id, machine_id, unit_id, plant_id, tag) VALUES",
            [
                (
                    row["sensor_id"],
                    row["machine_id"],
                    row.get("unit_id", ""),
                    row.get("plant_id", ""),
                    row.get("tag", ""),
                )
                for row in rows
            ],
        )

    # ------------------------------------------------------------------ writes
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
                    row.get("quality") or "GOOD",
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
        self._connect().execute(
            f"INSERT INTO {self._database}.{self._table} "
            f"(ts, machine_id, sensor_id, tag, value, unit, quality, unit_id, run_id) VALUES",
            self._buffer,
        )
        self._buffer.clear()

    def close(self) -> None:
        self.flush()
        if self._client is not None:
            self._client.disconnect()
            self._client = None

    def count(self) -> int:
        return self._rows

    def stored_count(self) -> int:
        """Row count as the server sees it, for verification after a run."""
        result = self._connect().execute(
            f"SELECT count() FROM {self._database}.{self._table}"
        )
        return int(result[0][0]) if result else 0

    def compressed_bytes(self) -> int:
        result = self._connect().execute(
            "SELECT sum(bytes_on_disk) FROM system.parts "
            "WHERE database = %(db)s AND table = %(tbl)s AND active",
            {"db": self._database, "tbl": self._table},
        )
        return int(result[0][0] or 0) if result else 0

    def distinct(self, column: str) -> set[str]:
        if column == "machine_id":
            return set(self._machines)
        if column == "sensor_id":
            return set(self._sensors)
        raise KeyError(f"distinct() supports machine_id and sensor_id, not {column!r}")


class ClickHouseEvaluationStore:
    """Ground truth and labels in a separate ClickHouse database."""

    def __init__(
        self, dsn: str, *, database: str = "pharma_eval", reset: bool = False
    ) -> None:
        self._settings = _parse_dsn(dsn)
        self._database = database
        self._reset = reset
        self._client: Client | None = None
        self._counts: dict[str, int] = {}

    @property
    def describe(self) -> str:
        return f"clickhouse:{self._settings['host']}/{self._database}"

    def _connect(self) -> Client:
        if self._client is None:
            self._client = Client(**self._settings)
        return self._client

    @staticmethod
    def _ddl(database: str, table: Table) -> str:
        columns = ",\n  ".join(
            f"{column.name} {'Nullable(' + _CH_TYPES[column.type] + ')' if column.nullable else _CH_TYPES[column.type]}"
            for column in table.columns
        )
        order = ", ".join(table.key_columns) or "tuple()"
        return (
            f"CREATE TABLE IF NOT EXISTS {database}.{table.name} (\n  {columns}\n) "
            f"ENGINE = MergeTree ORDER BY ({order})"
        )

    def initialise(self) -> None:
        client = self._connect()
        client.execute(f"CREATE DATABASE IF NOT EXISTS {self._database}")
        for name, table in EVAL_TABLES.items():
            if self._reset:
                client.execute(f"DROP TABLE IF EXISTS {self._database}.{name}")
            client.execute(self._ddl(self._database, table))

    def append(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        spec = EVAL_TABLES.get(table)
        if spec is None:
            raise KeyError(f"unknown evaluation table {table!r}")
        columns = spec.column_names
        payload = [tuple(row.get(name) for name in columns) for row in rows]
        self._connect().execute(
            f"INSERT INTO {self._database}.{table} ({', '.join(columns)}) VALUES", payload
        )
        self._counts[table] = self._counts.get(table, 0) + len(rows)
        return len(rows)

    def flush(self) -> None:
        pass  # inserts are synchronous

    def close(self) -> None:
        if self._client is not None:
            self._client.disconnect()
            self._client = None

    def count(self, table: str) -> int:
        return self._counts.get(table, 0)
