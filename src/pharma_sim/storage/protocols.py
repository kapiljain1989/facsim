"""Storage interfaces.

The simulation core depends on these shapes, not on a database. That is what lets
one seeded run land in SQLite plus Parquet with no infrastructure, or in
PostgreSQL plus ClickHouse for the production shape, without a line of
simulation code changing.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["RelationalStore", "TelemetryStore", "EvaluationStore"]


@runtime_checkable
class RelationalStore(Protocol):
    """Normalised, foreign-key-enforced storage for business records."""

    def initialise(self) -> None:
        """Create or reconcile the schema."""

    def upsert(self, table: str, rows: list[dict[str, Any]]) -> int:
        """Insert or replace rows by primary key. Returns rows written."""

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run a read-only query."""

    def count(self, table: str) -> int: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...

    @property
    def describe(self) -> str:
        """Human-readable backend description, for status output."""


@runtime_checkable
class TelemetryStore(Protocol):
    """Narrow, append-only, columnar storage for high-frequency readings."""

    def initialise(self) -> None: ...

    def append(self, rows: list[dict[str, Any]]) -> int: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...

    def count(self) -> int: ...

    def distinct(self, column: str) -> set[str]:
        """Distinct values of a column, used by the cross-store integrity check."""

    @property
    def describe(self) -> str: ...


@runtime_checkable
class EvaluationStore(Protocol):
    """Ground truth and prediction labels, isolated from operational data."""

    def initialise(self) -> None: ...

    def append(self, table: str, rows: list[dict[str, Any]]) -> int: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...

    def count(self, table: str) -> int: ...

    @property
    def describe(self) -> str: ...
