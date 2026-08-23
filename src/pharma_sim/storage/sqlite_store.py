"""SQLite relational backend.

The zero-setup default. Two choices worth noting:

* **Foreign keys are enabled.** ``PRAGMA foreign_keys=ON`` is off by default in
  SQLite, so referential integrity (§40) would otherwise be a claim rather than a
  guarantee. With it on, an orphan row is rejected by the database.
* **The schema is reconciled, not assumed.** On open, existing tables are
  compared against the declaration and missing nullable columns are added. A
  config change that introduces a new field therefore does not require dropping
  the database.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pharma_sim.storage.schema import TABLE_ORDER, TABLES, Table

__all__ = ["SqliteStore"]

logger = logging.getLogger(__name__)


def _encode(value: Any) -> Any:
    """Convert a Python value into something SQLite can store."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, sort_keys=True)
    return value


class SqliteStore:
    """Relational store backed by a single SQLite file."""

    def __init__(
        self,
        dsn: str,
        *,
        tables: dict[str, Table] | None = None,
        table_order: tuple[str, ...] | None = None,
        enforce_foreign_keys: bool = True,
    ) -> None:
        self._path = Path(dsn)
        self._tables = tables if tables is not None else TABLES
        self._order = table_order if table_order is not None else TABLE_ORDER
        self._enforce_fk = enforce_foreign_keys
        self._conn: sqlite3.Connection | None = None
        self._written = 0
        # SQLite connections are bound to their creating thread by default, but
        # the API serves sync endpoints from a threadpool while the simulator
        # writes from its own thread. Sharing one connection under a lock is
        # correct here and keeps the write path single-connection.
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ lifecycle
    @property
    def describe(self) -> str:
        return f"sqlite:{self._path}"

    @property
    def path(self) -> Path:
        return self._path

    def initialise(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self._path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        cursor = self._conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA foreign_keys={'ON' if self._enforce_fk else 'OFF'}")
        for name in self._order:
            table = self._tables.get(name)
            if table is not None:
                self._create_or_reconcile(cursor, table)
        # Any table not mentioned in the order still gets created.
        for name, table in self._tables.items():
            if name not in self._order:
                self._create_or_reconcile(cursor, table)
        self._conn.commit()

    def _create_or_reconcile(self, cursor: sqlite3.Cursor, table: Table) -> None:
        existing = self._existing_columns(cursor, table.name)
        if not existing:
            cursor.execute(self.ddl(table))
            for index_columns in table.indexes:
                index_name = f"ix_{table.name}_{'_'.join(index_columns)}"
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS {index_name} "
                    f"ON {table.name} ({', '.join(index_columns)})"
                )
            return

        for column in table.columns:
            if column.name in existing:
                continue
            # Only nullable additions can be reconciled in place; a new NOT NULL
            # column would have no value for existing rows.
            cursor.execute(
                f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column.sqlite_type()}"
            )
            logger.info(
                "schema reconciled: added %s.%s", table.name, column.name
            )

    @staticmethod
    def _existing_columns(cursor: sqlite3.Cursor, table_name: str) -> set[str]:
        cursor.execute(f"PRAGMA table_info({table_name})")
        return {row[1] for row in cursor.fetchall()}

    @staticmethod
    def ddl(table: Table) -> str:
        """CREATE TABLE statement for one table."""
        parts: list[str] = []
        for column in table.columns:
            piece = f"{column.name} {column.sqlite_type()}"
            if not column.nullable:
                piece += " NOT NULL"
            if column.primary_key and not table.composite_key:
                piece += " PRIMARY KEY"
            parts.append(piece)
        if table.composite_key:
            parts.append(f"PRIMARY KEY ({', '.join(table.composite_key)})")
        for column in table.columns:
            if column.references:
                ref_table, ref_column = column.references.split(".")
                parts.append(
                    f"FOREIGN KEY ({column.name}) REFERENCES {ref_table}({ref_column})"
                )
        return f"CREATE TABLE IF NOT EXISTS {table.name} (\n  " + ",\n  ".join(parts) + "\n)"

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.commit()
                self._conn.close()
                self._conn = None

    def flush(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.commit()

    # --------------------------------------------------------------------- writes
    def upsert(self, table: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        if self._conn is None:
            raise RuntimeError("store is not initialised; call initialise() first")
        spec = self._tables.get(table)
        if spec is None:
            raise KeyError(f"unknown table {table!r}; declared: {sorted(self._tables)}")

        columns = spec.column_names
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        )
        payload = [tuple(_encode(row.get(name)) for name in columns) for row in rows]
        with self._lock:
            self._conn.executemany(sql, payload)
        self._written += len(payload)
        return len(payload)

    # ---------------------------------------------------------------------- reads
    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if self._conn is None:
            raise RuntimeError("store is not initialised")
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def count(self, table: str) -> int:
        rows = self.query(f"SELECT COUNT(*) AS n FROM {table}")
        return int(rows[0]["n"]) if rows else 0

    def distinct(self, table: str, column: str) -> set[str]:
        rows = self.query(f"SELECT DISTINCT {column} AS v FROM {table} WHERE {column} IS NOT NULL")
        return {str(row["v"]) for row in rows}

    def table_counts(self) -> dict[str, int]:
        return {name: self.count(name) for name in self._tables}

    @property
    def rows_written(self) -> int:
        return self._written
