"""Storage facade: one write surface over three stores.

The engines call :meth:`StorageFacade.write` with a table name and a row and know
nothing about which backend it lands in. The facade decides:

* declared relational tables go to the transactional store,
* ``sensor_readings`` goes to the time-series store,
* ``ground_truth_events`` and ``prediction_labels`` go to the evaluation store.

Writes are buffered and flushed in :data:`TABLE_ORDER`, because foreign keys are
enforced and a fact must not reach the database before the dimension it
references.
"""

from __future__ import annotations

import logging
from typing import Any

from pharma_sim.storage.protocols import EvaluationStore, RelationalStore, TelemetryStore
from pharma_sim.storage.schema import EVAL_TABLES, TABLE_ORDER, TABLES

__all__ = ["StorageFacade", "IntegrityReport"]

logger = logging.getLogger(__name__)

TELEMETRY_TABLE_NAME = "sensor_readings"


class IntegrityReport:
    """Result of the cross-store referential integrity check (§40)."""

    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    @property
    def failures(self) -> list[tuple[str, bool, str]]:
        return [check for check in self.checks if not check[1]]

    def render(self) -> str:
        lines = []
        for name, ok, detail in self.checks:
            mark = "PASS" if ok else "FAIL"
            lines.append(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
        return "\n".join(lines)


class StorageFacade:
    """Buffered, dependency-ordered writes across the three stores."""

    def __init__(
        self,
        relational: RelationalStore,
        telemetry: TelemetryStore,
        evaluation: EvaluationStore,
        *,
        flush_threshold: int = 2000,
        telemetry_threshold: int = 20_000,
    ) -> None:
        self._relational = relational
        self._telemetry = telemetry
        self._evaluation = evaluation
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._telemetry_buffer: list[dict[str, Any]] = []
        self._eval_buffers: dict[str, list[dict[str, Any]]] = {}
        self._flush_threshold = flush_threshold
        self._telemetry_threshold = telemetry_threshold
        self._pending = 0
        self._counts: dict[str, int] = {}

    # ------------------------------------------------------------------ lifecycle
    def initialise(self) -> None:
        self._relational.initialise()
        self._telemetry.initialise()
        self._evaluation.initialise()

    def close(self) -> None:
        self.flush()
        self._relational.close()
        self._telemetry.close()
        self._evaluation.close()

    @property
    def relational(self) -> RelationalStore:
        return self._relational

    @property
    def telemetry(self) -> TelemetryStore:
        return self._telemetry

    @property
    def evaluation(self) -> EvaluationStore:
        return self._evaluation

    def describe(self) -> dict[str, str]:
        return {
            "transactional": self._relational.describe,
            "timeseries": self._telemetry.describe,
            "evaluation": self._evaluation.describe,
        }

    # --------------------------------------------------------------------- writes
    def write(self, table: str, row: dict[str, Any]) -> None:
        self.write_many(table, [row])

    def write_many(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        self._counts[table] = self._counts.get(table, 0) + len(rows)

        if table == TELEMETRY_TABLE_NAME:
            self._telemetry_buffer.extend(rows)
            if len(self._telemetry_buffer) >= self._telemetry_threshold:
                self._flush_telemetry()
            return

        if table in EVAL_TABLES:
            buffer = self._eval_buffers.setdefault(table, [])
            buffer.extend(rows)
            if len(buffer) >= self._flush_threshold:
                self._flush_evaluation()
            return

        if table not in TABLES:
            raise KeyError(
                f"unknown table {table!r}; declared relational tables: {sorted(TABLES)}"
            )
        self._buffers.setdefault(table, []).extend(rows)
        self._pending += len(rows)
        if self._pending >= self._flush_threshold:
            self._flush_relational()

    # --------------------------------------------------------------------- flushes
    def _flush_relational(self) -> None:
        if not self._buffers:
            return
        # Dependency order, so an enabled foreign key does not reject a fact whose
        # dimension is sitting in a later buffer.
        for name in TABLE_ORDER:
            rows = self._buffers.pop(name, None)
            if rows:
                self._relational.upsert(name, rows)
        for name in list(self._buffers):
            rows = self._buffers.pop(name)
            if rows:
                self._relational.upsert(name, rows)
        self._relational.flush()
        self._pending = 0

    def _flush_telemetry(self) -> None:
        if self._telemetry_buffer:
            self._telemetry.append(self._telemetry_buffer)
            self._telemetry_buffer.clear()

    def _flush_evaluation(self) -> None:
        for table, rows in self._eval_buffers.items():
            if rows:
                self._evaluation.append(table, rows)
                rows.clear()

    def flush(self) -> None:
        self._flush_relational()
        self._flush_telemetry()
        self._flush_evaluation()
        self._telemetry.flush()
        self._evaluation.flush()

    # ---------------------------------------------------------------------- stats
    def written(self) -> dict[str, int]:
        return dict(sorted(self._counts.items()))

    def total_written(self) -> int:
        return sum(self._counts.values())

    # ------------------------------------------------------------------ integrity
    def verify_integrity(self) -> IntegrityReport:
        """Check referential integrity, including across the store boundary.

        A ClickHouse or Parquet telemetry row cannot have a database-level foreign
        key into the relational store, so the guarantee has to be checked
        explicitly rather than assumed. This is what makes §40 true for a polyglot
        deployment rather than only for the single-database case.
        """
        self.flush()
        report = IntegrityReport()

        machines = self._relational.distinct("machines", "machine_id")
        sensors = self._relational.distinct("sensors", "sensor_id")

        telemetry_machines = self._telemetry.distinct("machine_id")
        orphan_machines = telemetry_machines - machines
        report.add(
            "every telemetry machine_id exists in the relational store",
            not orphan_machines,
            f"{len(orphan_machines)} orphan(s): {sorted(orphan_machines)[:5]}"
            if orphan_machines
            else f"{len(telemetry_machines)} machines checked",
        )

        telemetry_sensors = self._telemetry.distinct("sensor_id")
        orphan_sensors = telemetry_sensors - sensors
        report.add(
            "every telemetry sensor_id exists in the relational store",
            not orphan_sensors,
            f"{len(orphan_sensors)} orphan(s): {sorted(orphan_sensors)[:5]}"
            if orphan_sensors
            else f"{len(telemetry_sensors)} sensors checked",
        )

        # In-database referential checks. These would be impossible to violate
        # with foreign keys on, so a failure here means they are off.
        for name, sql in (
            (
                "every QC result resolves to a batch",
                "SELECT COUNT(*) AS n FROM qc_results q "
                "LEFT JOIN batches b ON q.batch_id = b.batch_id WHERE b.batch_id IS NULL",
            ),
            (
                "every QC result resolves to a product",
                "SELECT COUNT(*) AS n FROM qc_results q "
                "LEFT JOIN products p ON q.product_id = p.product_id WHERE p.product_id IS NULL",
            ),
            (
                "every RCA resolves to a deviation",
                "SELECT COUNT(*) AS n FROM rca r "
                "LEFT JOIN deviations d ON r.deviation_id = d.deviation_id "
                "WHERE d.deviation_id IS NULL",
            ),
            (
                "every CAPA resolves to a deviation",
                "SELECT COUNT(*) AS n FROM capa c "
                "LEFT JOIN deviations d ON c.deviation_id = d.deviation_id "
                "WHERE d.deviation_id IS NULL",
            ),
            (
                "every failure resolves to a machine",
                "SELECT COUNT(*) AS n FROM failures f "
                "LEFT JOIN machines m ON f.machine_id = m.machine_id WHERE m.machine_id IS NULL",
            ),
            (
                "every batch stage resolves to a batch",
                "SELECT COUNT(*) AS n FROM batch_stages s "
                "LEFT JOIN batches b ON s.batch_id = b.batch_id WHERE b.batch_id IS NULL",
            ),
            (
                "every production record resolves to a shift instance",
                "SELECT COUNT(*) AS n FROM production_records p "
                "LEFT JOIN shift_instances s ON p.shift_instance_id = s.shift_instance_id "
                "WHERE s.shift_instance_id IS NULL",
            ),
            (
                "every event resolves to a declared event type",
                "SELECT COUNT(*) AS n FROM events e "
                "LEFT JOIN event_types t ON e.event_type = t.event_type "
                "WHERE t.event_type IS NULL",
            ),
        ):
            try:
                rows = self._relational.query(sql)
                orphans = int(rows[0]["n"]) if rows else 0
                report.add(name, orphans == 0, "" if orphans == 0 else f"{orphans} orphan row(s)")
            except Exception as exc:  # pragma: no cover - backend-specific SQL issues
                report.add(name, False, f"check failed: {exc}")

        return report
