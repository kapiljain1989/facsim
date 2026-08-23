"""Parquet backends for telemetry and evaluation data.

Telemetry is written in chunked row groups so memory stays bounded no matter how
long the run is: a 30-day dense run is tens of millions of readings, and holding
them to write one file at the end is not an option.

Partitioning by date (and optionally unit) keeps a day's slice readable without
scanning the whole dataset, which is how any downstream training job will use it.
"""

from __future__ import annotations

import logging
import shutil
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from pharma_sim.storage.schema import EVAL_TABLES, TELEMETRY_TABLE, Table

__all__ = ["ParquetTelemetryStore", "ParquetEvaluationStore"]

logger = logging.getLogger(__name__)

_ARROW_TYPES = {
    "TEXT": pa.string(),
    "INTEGER": pa.int64(),
    "REAL": pa.float64(),
    "BOOLEAN": pa.bool_(),
    "TIMESTAMP": pa.timestamp("us"),
    "DATE": pa.date32(),
    "JSON": pa.string(),
}


def _arrow_schema(table: Table, extra: tuple[tuple[str, pa.DataType], ...] = ()) -> pa.Schema:
    fields = [pa.field(c.name, _ARROW_TYPES[c.type]) for c in table.columns]
    fields.extend(pa.field(name, dtype) for name, dtype in extra)
    return pa.schema(fields)


def _coerce(value: Any, column_type: str) -> Any:
    if value is None:
        return None
    if column_type == "TIMESTAMP":
        return value if isinstance(value, datetime) else None
    if column_type == "DATE":
        if isinstance(value, datetime):
            return value.date()
        return value if isinstance(value, date) else None
    if column_type == "BOOLEAN":
        return bool(value)
    if column_type == "INTEGER":
        return int(value)
    if column_type == "REAL":
        return float(value)
    if isinstance(value, (dict, list, tuple)):
        import json

        return json.dumps(value, default=str, sort_keys=True)
    return str(value)


class _ChunkedParquetWriter:
    """Buffers rows and writes them as row groups into one file per partition."""

    def __init__(self, root: Path, schema: pa.Schema, batch_size: int) -> None:
        self._root = root
        self._schema = schema
        self._batch_size = batch_size
        self._writers: dict[str, pq.ParquetWriter] = {}
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._rows = 0

    def add(self, partition: str, row: dict[str, Any]) -> None:
        buffer = self._buffers.setdefault(partition, [])
        buffer.append(row)
        self._rows += 1
        if len(buffer) >= self._batch_size:
            self._write(partition)

    def extend(self, partition: str, rows: list[dict[str, Any]]) -> None:
        """Bulk variant, used on the telemetry hot path."""
        buffer = self._buffers.setdefault(partition, [])
        buffer.extend(rows)
        self._rows += len(rows)
        if len(buffer) >= self._batch_size:
            self._write(partition)

    def _write(self, partition: str) -> None:
        buffer = self._buffers.get(partition)
        if not buffer:
            return
        writer = self._writers.get(partition)
        if writer is None:
            path = self._root / partition
            path.parent.mkdir(parents=True, exist_ok=True)
            writer = pq.ParquetWriter(path, self._schema, compression="zstd")
            self._writers[partition] = writer
        columns = {
            field.name: [row.get(field.name) for row in buffer] for field in self._schema
        }
        writer.write_table(pa.table(columns, schema=self._schema))
        buffer.clear()

    def flush(self) -> None:
        for partition in list(self._buffers):
            self._write(partition)

    def close(self) -> None:
        self.flush()
        for writer in self._writers.values():
            writer.close()
        self._writers.clear()

    @property
    def rows(self) -> int:
        return self._rows


class ParquetTelemetryStore:
    """Partitioned Parquet time-series store."""

    def __init__(
        self,
        dsn: str,
        *,
        partition_by: tuple[str, ...] = ("date",),
        batch_size: int = 50_000,
        reset: bool = False,
    ) -> None:
        self._root = Path(dsn)
        self._partition_by = partition_by
        self._batch_size = batch_size
        self._reset = reset
        self._schema = _arrow_schema(TELEMETRY_TABLE)
        self._writer: _ChunkedParquetWriter | None = None
        self._machines: set[str] = set()
        self._sensors: set[str] = set()
        self._partition_cache: dict[tuple[Any, ...], str] = {}

    @property
    def describe(self) -> str:
        return f"parquet:{self._root} (partitioned by {'/'.join(self._partition_by)})"

    @property
    def root(self) -> Path:
        return self._root

    def initialise(self) -> None:
        if self._reset and self._root.exists():
            shutil.rmtree(self._root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._writer = _ChunkedParquetWriter(self._root, self._schema, self._batch_size)

    def _partition_for(self, row: dict[str, Any]) -> str:
        """Partition path for one row, memoised on the key values.

        There are only a few hundred distinct (date, unit) combinations in a long
        run, so building the path string once per combination rather than once
        per row matters at tens of millions of rows.
        """
        key = tuple(
            row["timestamp"].date() if column == "date" else row.get(column)
            for column in self._partition_by
        )
        cached = self._partition_cache.get(key)
        if cached is not None:
            return cached

        parts = [
            f"{column}={value if value is not None else 'unknown'}"
            for column, value in zip(self._partition_by, key, strict=True)
        ]
        path = "/".join(parts) + "/part.parquet"
        self._partition_cache[key] = path
        return path

    def append(self, rows: list[dict[str, Any]]) -> int:
        """Buffer readings, grouped by partition.

        No per-column type coercion here: the sampler emits values already in the
        schema's types, and coercing tens of millions of rows again was the single
        largest cost in the write path. Arrow validates the types on write, so a
        genuine mismatch still surfaces rather than being silently accepted.
        """
        if not rows:
            return 0
        if self._writer is None:
            raise RuntimeError("telemetry store is not initialised")

        grouped: dict[str, list[dict[str, Any]]] = {}
        machines = self._machines
        sensors = self._sensors
        for row in rows:
            machines.add(row["machine_id"])
            sensors.add(row["sensor_id"])
            grouped.setdefault(self._partition_for(row), []).append(row)
        for partition, batch in grouped.items():
            self._writer.extend(partition, batch)
        return len(rows)

    def flush(self) -> None:
        if self._writer is not None:
            self._writer.flush()

    def close(self) -> None:
        if self._writer is not None:
            self._writer.close()

    def count(self) -> int:
        return self._writer.rows if self._writer else 0

    def distinct(self, column: str) -> set[str]:
        """Distinct machines or sensors seen, for the integrity check.

        Tracked on write rather than by re-reading the dataset: the point of the
        check is to catch an orphan, and scanning tens of millions of rows to do
        it would make the check unusable on a real run.
        """
        if column == "machine_id":
            return set(self._machines)
        if column == "sensor_id":
            return set(self._sensors)
        raise KeyError(f"distinct() supports machine_id and sensor_id, not {column!r}")

    def files(self) -> list[Path]:
        return sorted(self._root.rglob("*.parquet"))

    def total_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.files())


class ParquetEvaluationStore:
    """Ground truth and prediction labels as Parquet, kept apart from operations."""

    def __init__(self, dsn: str, *, batch_size: int = 10_000, reset: bool = False) -> None:
        self._root = Path(dsn)
        self._batch_size = batch_size
        self._reset = reset
        self._writers: dict[str, _ChunkedParquetWriter] = {}

    @property
    def describe(self) -> str:
        return f"parquet:{self._root}"

    @property
    def root(self) -> Path:
        return self._root

    def initialise(self) -> None:
        if self._reset and self._root.exists():
            shutil.rmtree(self._root)
        self._root.mkdir(parents=True, exist_ok=True)
        for name, table in EVAL_TABLES.items():
            self._writers[name] = _ChunkedParquetWriter(
                self._root / name, _arrow_schema(table), self._batch_size
            )

    def append(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        writer = self._writers.get(table)
        if writer is None:
            raise KeyError(f"unknown evaluation table {table!r}; known: {sorted(self._writers)}")
        spec = EVAL_TABLES[table]
        for row in rows:
            coerced = {
                column.name: _coerce(row.get(column.name), column.type)
                for column in spec.columns
            }
            writer.add("part.parquet", coerced)
        return len(rows)

    def flush(self) -> None:
        for writer in self._writers.values():
            writer.flush()

    def close(self) -> None:
        for writer in self._writers.values():
            writer.close()

    def count(self, table: str) -> int:
        writer = self._writers.get(table)
        return writer.rows if writer else 0

    def files(self) -> list[Path]:
        return sorted(self._root.rglob("*.parquet"))
