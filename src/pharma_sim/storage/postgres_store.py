"""PostgreSQL relational backend.

The production shape of the transactional store. Shares the schema declaration
with SQLite, so the two backends cannot drift apart, and enables the same
foreign keys — here they are on by default rather than needing a pragma.

Requires the ``postgres`` extra: ``uv pip install -e ".[postgres]"``.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Any

import psycopg
from psycopg.types.json import Json

from pharma_sim.storage.schema import EVAL_TABLES, TABLE_ORDER, TABLES, Table

__all__ = ["PostgresStore", "PostgresEvaluationStore"]

logger = logging.getLogger(__name__)


def _encode(value: Any, column_type: str) -> Any:
    if value is None:
        return None
    if column_type == "JSON":
        return Json(value) if isinstance(value, (dict, list)) else Json(json.loads(str(value)))
    if column_type == "BOOLEAN":
        return bool(value)
    if column_type == "DATE":
        return value.date() if isinstance(value, datetime) else value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, sort_keys=True)
    return value


class _PostgresBase:
    """Shared DDL generation and batched upserts."""

    def __init__(self, dsn: str, schema: str, tables: dict[str, Table], order: tuple[str, ...]):
        self._dsn = dsn
        self._schema = schema
        self._tables = tables
        self._order = order
        self._conn: psycopg.Connection | None = None
        self._written = 0

    @property
    def describe(self) -> str:
        return f"postgres:{self._schema}@{self._safe_dsn()}"

    def _safe_dsn(self) -> str:
        """DSN with any password removed, for logging and status output."""
        if "@" in self._dsn:
            head, tail = self._dsn.rsplit("@", 1)
            if ":" in head:
                return f"{head.rsplit(':', 1)[0]}:***@{tail}"
        return self._dsn

    def _connect(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self._dsn, autocommit=False)
        return self._conn

    def ddl(self, table: Table) -> str:
        parts: list[str] = []
        for column in table.columns:
            piece = f"{column.name} {column.postgres_type()}"
            if not column.nullable:
                piece += " NOT NULL"
            parts.append(piece)
        keys = table.key_columns
        if keys:
            parts.append(f"PRIMARY KEY ({', '.join(keys)})")
        for column in table.columns:
            if column.references:
                ref_table, ref_column = column.references.split(".")
                parts.append(
                    f"FOREIGN KEY ({column.name}) REFERENCES "
                    f"{self._schema}.{ref_table}({ref_column})"
                )
        body = ",\n  ".join(parts)
        return f"CREATE TABLE IF NOT EXISTS {self._schema}.{table.name} (\n  {body}\n)"

    def initialise(self) -> None:
        connection = self._connect()
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {self._schema}")
            for name in self._order:
                table = self._tables.get(name)
                if table is not None:
                    self._create_or_reconcile(cursor, table)
            for name, table in self._tables.items():
                if name not in self._order:
                    self._create_or_reconcile(cursor, table)
        connection.commit()

    def _create_or_reconcile(self, cursor, table: Table) -> None:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s",
            (self._schema, table.name),
        )
        existing = {row[0] for row in cursor.fetchall()}
        if not existing:
            cursor.execute(self.ddl(table))
            for index_columns in table.indexes:
                name = f"ix_{table.name}_{'_'.join(index_columns)}"
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS {name} ON {self._schema}.{table.name} "
                    f"({', '.join(index_columns)})"
                )
            return
        for column in table.columns:
            if column.name in existing:
                continue
            cursor.execute(
                f"ALTER TABLE {self._schema}.{table.name} "
                f"ADD COLUMN IF NOT EXISTS {column.name} {column.postgres_type()}"
            )
            logger.info("schema reconciled: added %s.%s", table.name, column.name)

    def upsert(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        spec = self._tables.get(table)
        if spec is None:
            raise KeyError(f"unknown table {table!r}; declared: {sorted(self._tables)}")

        columns = spec.column_names
        types = {column.name: column.type for column in spec.columns}
        placeholders = ", ".join(["%s"] * len(columns))
        keys = spec.key_columns
        updates = ", ".join(
            f"{name} = EXCLUDED.{name}" for name in columns if name not in keys
        )
        conflict = (
            f"ON CONFLICT ({', '.join(keys)}) DO "
            + (f"UPDATE SET {updates}" if updates else "NOTHING")
            if keys
            else ""
        )
        sql = (
            f"INSERT INTO {self._schema}.{table} ({', '.join(columns)}) "
            f"VALUES ({placeholders}) {conflict}"
        )
        payload = [
            tuple(_encode(row.get(name), types[name]) for name in columns) for row in rows
        ]
        connection = self._connect()
        with connection.cursor() as cursor:
            cursor.executemany(sql, payload)
        self._written += len(payload)
        return len(payload)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        # Unqualified table names are resolved against this store's schema.
        connection = self._connect()
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL search_path TO {self._schema}, public")
            cursor.execute(sql, params or None)
            if cursor.description is None:
                return []
            names = [description[0] for description in cursor.description]
            return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def count(self, table: str) -> int:
        rows = self.query(f"SELECT COUNT(*) AS n FROM {self._schema}.{table}")
        return int(rows[0]["n"]) if rows else 0

    def distinct(self, table: str, column: str) -> set[str]:
        rows = self.query(
            f"SELECT DISTINCT {column} AS v FROM {self._schema}.{table} "
            f"WHERE {column} IS NOT NULL"
        )
        return {str(row["v"]) for row in rows}

    def flush(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.commit()

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.commit()
            self._conn.close()
        self._conn = None

    @property
    def rows_written(self) -> int:
        return self._written


class PostgresStore(_PostgresBase):
    """Transactional store in the ``oltp`` schema."""

    def __init__(self, dsn: str, *, schema: str = "oltp", reset: bool = False) -> None:
        super().__init__(dsn, schema, TABLES, TABLE_ORDER)
        self._reset = reset

    def initialise(self) -> None:
        if self._reset:
            connection = self._connect()
            with connection.cursor() as cursor:
                cursor.execute(f"DROP SCHEMA IF EXISTS {self._schema} CASCADE")
            connection.commit()
        super().initialise()


class PostgresEvaluationStore(_PostgresBase):
    """Ground truth and labels in their own schema, isolated from operations."""

    def __init__(self, dsn: str, *, schema: str = "eval", reset: bool = False) -> None:
        super().__init__(dsn, schema, EVAL_TABLES, tuple(EVAL_TABLES))
        self._reset = reset

    def initialise(self) -> None:
        if self._reset:
            connection = self._connect()
            with connection.cursor() as cursor:
                cursor.execute(f"DROP SCHEMA IF EXISTS {self._schema} CASCADE")
            connection.commit()
        super().initialise()

    def append(self, table: str, rows: list[dict[str, Any]]) -> int:
        return self.upsert(table, rows)
