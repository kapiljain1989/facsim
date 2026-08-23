"""Read-only data access for the API.

Everything here is a query. The dashboard cannot change the factory: there is no
write path, no auth surface to protect, and no mutation endpoint to get wrong.

Cross-store reads are stitched here rather than in the route handlers, so a
caller asking for "this machine's vibration over the last six hours" does not
need to know whether that lives in Parquet, ClickHouse or a hypertable.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pharma_sim.config.models import FactoryConfig
from pharma_sim.storage.facade import StorageFacade

__all__ = ["DataService", "TelemetryUnavailable"]

logger = logging.getLogger(__name__)


class TelemetryUnavailable(Exception):
    """Raised when the configured time-series backend cannot be read back."""


class DataService:
    """Queries the stored dataset for the API."""

    def __init__(
        self, storage: StorageFacade, config: FactoryConfig, live_plant: Any = None
    ) -> None:
        self._storage = storage
        self._config = config
        # Set only when a live simulation is running in this process. The
        # persisted state log is an append-only sequence of *closed* intervals —
        # "last row" is always the state a machine just left, never the one it
        # is currently in, which is fine for a finished run's history view but
        # means a machine that settles into RUNNING for hours would show
        # whatever it transitioned through beforehand, indefinitely, on a dashboard
        # that is supposed to be live. Reading the in-memory machine sidesteps
        # the log entirely: no query can be stale when there is no query.
        self._live_plant = live_plant

    def _live_state(self, machine_id: str) -> str | None:
        if self._live_plant is None:
            return None
        machine = self._live_plant.machines.get(machine_id)
        return machine.state if machine is not None else None

    # ------------------------------------------------------------------ helpers
    @property
    def store(self):
        return self._storage.relational

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        try:
            return self.store.query(sql, params)
        except Exception as exc:
            logger.warning("query failed: %s", exc)
            return []

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def describe_storage(self) -> dict[str, str]:
        return self._storage.describe()

    # -------------------------------------------------------------------- plant
    def plant(self) -> dict[str, Any]:
        plant = self._one("SELECT * FROM plants LIMIT 1") or {}
        latest_run = self._one("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1") or {}
        return {
            **plant,
            "run": latest_run,
            "states": [row["state_id"] for row in self._rows("SELECT state_id FROM states")],
            "storage": self.describe_storage(),
        }

    def plant_summary(self) -> dict[str, Any]:
        """The overview tiles: current state counts and cumulative totals."""
        machines = self.machines()
        by_state: dict[str, int] = {}
        for machine in machines:
            by_state[machine["state"] or "UNKNOWN"] = (
                by_state.get(machine["state"] or "UNKNOWN", 0) + 1
            )

        production = self._one(
            "SELECT SUM(good_quantity) AS good, SUM(reject_quantity) AS reject, "
            "SUM(runtime_seconds) AS runtime, SUM(downtime_seconds) AS downtime, "
            "SUM(unscheduled_seconds) AS unscheduled, SUM(idle_seconds) AS idle, "
            "SUM(energy_kwh) AS energy FROM production_records"
        ) or {}
        oee = self._one(
            "SELECT AVG(availability) AS availability, AVG(performance) AS performance, "
            "AVG(quality) AS quality, AVG(oee) AS oee, AVG(utilisation) AS utilisation "
            "FROM oee_snapshots WHERE scope = 'PLANT' AND loading_seconds > 0"
        ) or {}
        batches = self._one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN disposition = 'RELEASED' THEN 1 ELSE 0 END) AS released, "
            "SUM(CASE WHEN disposition = 'REJECTED' THEN 1 ELSE 0 END) AS rejected, "
            "SUM(CASE WHEN disposition = 'QUARANTINED' THEN 1 ELSE 0 END) AS quarantined "
            "FROM batches WHERE completed_at IS NOT NULL"
        ) or {}
        qc = self._one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN result IN ('FAIL','OOS') THEN 1 ELSE 0 END) AS failed "
            "FROM qc_results"
        ) or {}
        deviations = self._one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status != 'CLOSED' THEN 1 ELSE 0 END) AS open FROM deviations"
        ) or {}
        reliability = self._one(
            "SELECT COUNT(*) AS failures, SUM(downtime_minutes) AS downtime_minutes "
            "FROM failures"
        ) or {}
        maintenance = self._one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN maintenance_type = 'PREVENTIVE' THEN 1 ELSE 0 END) AS preventive "
            "FROM maintenance"
        ) or {}

        return {
            "machine_count": len(machines),
            "machines_by_state": dict(sorted(by_state.items())),
            "production": production,
            "oee": oee,
            "batches": batches,
            "qc": qc,
            "deviations": deviations,
            "reliability": reliability,
            "maintenance": maintenance,
        }

    # -------------------------------------------------------------------- units
    def units(self) -> list[dict[str, Any]]:
        return self._rows(
            """
            SELECT u.*,
                   (SELECT COUNT(*) FROM machines m WHERE m.unit_id = u.unit_id)
                       AS machines,
                   (SELECT AVG(o.oee) FROM oee_snapshots o
                     WHERE o.scope = 'UNIT' AND o.scope_id = u.unit_id
                       AND o.planned_quantity > 0) AS oee,
                   (SELECT SUM(p.good_quantity) FROM production_records p
                     WHERE p.unit_id = u.unit_id) AS good_quantity,
                   (SELECT SUM(p.reject_quantity) FROM production_records p
                     WHERE p.unit_id = u.unit_id) AS reject_quantity,
                   (SELECT SUM(p.downtime_seconds) FROM production_records p
                     WHERE p.unit_id = u.unit_id) AS downtime_seconds,
                   (SELECT COUNT(*) FROM failures f WHERE f.unit_id = u.unit_id)
                       AS failures
            FROM units u ORDER BY u.sequence
            """
        )

    # ----------------------------------------------------------------- machines
    def machines(self, unit_id: str | None = None) -> list[dict[str, Any]]:
        """Machines with their latest known state and cumulative performance."""
        where = "WHERE m.unit_id = ?" if unit_id else ""
        params = (unit_id,) if unit_id else ()
        rows = self._rows(
            f"""
            SELECT m.*, u.name AS unit_name, u.process_stage,
                   (SELECT h.state FROM machine_state_history h
                     WHERE h.machine_id = m.machine_id
                     ORDER BY h.sequence DESC LIMIT 1) AS state,
                   (SELECT AVG(o.oee) FROM oee_snapshots o
                     WHERE o.scope = 'MACHINE' AND o.scope_id = m.machine_id
                       AND o.planned_quantity > 0) AS oee,
                   (SELECT SUM(p.good_quantity) FROM production_records p
                     WHERE p.machine_id = m.machine_id) AS good_quantity,
                   (SELECT SUM(p.reject_quantity) FROM production_records p
                     WHERE p.machine_id = m.machine_id) AS reject_quantity,
                   (SELECT SUM(p.runtime_seconds) FROM production_records p
                     WHERE p.machine_id = m.machine_id) AS runtime_seconds,
                   (SELECT SUM(p.downtime_seconds) FROM production_records p
                     WHERE p.machine_id = m.machine_id) AS downtime_seconds,
                   (SELECT COUNT(*) FROM failures f WHERE f.machine_id = m.machine_id)
                       AS failures
            FROM machines m JOIN units u ON u.unit_id = m.unit_id
            {where}
            ORDER BY m.machine_id
            """,
            params,
        )
        if self._live_plant is not None:
            for row in rows:
                live_state = self._live_state(row["machine_id"])
                if live_state is not None:
                    row["state"] = live_state
        return rows

    def machine(self, machine_id: str) -> dict[str, Any] | None:
        machine = self._one(
            "SELECT m.*, u.name AS unit_name, u.process_stage "
            "FROM machines m JOIN units u ON u.unit_id = m.unit_id "
            "WHERE m.machine_id = ?",
            (machine_id,),
        )
        if machine is None:
            return None
        machine["sensors"] = self.machine_sensors(machine_id)
        machine["state_history"] = self._rows(
            "SELECT state, entered_at, exited_at, seconds, reason FROM machine_state_history "
            "WHERE machine_id = ? ORDER BY sequence DESC LIMIT 40",
            (machine_id,),
        )
        live_state = self._live_state(machine_id)
        machine["current_state"] = live_state or (
            machine["state_history"][0]["state"] if machine["state_history"] else None
        )
        if live_state is not None:
            machine["state"] = live_state
        machine["production"] = self._rows(
            "SELECT * FROM production_records WHERE machine_id = ? "
            "ORDER BY shift_instance_id DESC LIMIT 20",
            (machine_id,),
        )
        machine["failures"] = self._rows(
            "SELECT * FROM failures WHERE machine_id = ? ORDER BY detected_at DESC LIMIT 20",
            (machine_id,),
        )
        machine["maintenance"] = self._rows(
            "SELECT * FROM maintenance WHERE machine_id = ? "
            "ORDER BY scheduled_time DESC LIMIT 20",
            (machine_id,),
        )
        return machine

    def machine_sensors(self, machine_id: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM sensors WHERE machine_id = ? ORDER BY tag", (machine_id,)
        )

    def machine_events(self, machine_id: str, limit: int = 120) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM machine_events WHERE machine_id = ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (machine_id, limit),
        )

    def machine_timeline(self, machine_id: str, limit: int = 200) -> list[dict[str, Any]]:
        """Everything that happened to one machine, newest first (§31)."""
        return self._rows(
            "SELECT event_id, timestamp, event_type, category, severity, batch_id, payload "
            "FROM events WHERE machine_id = ? ORDER BY timestamp DESC, event_id DESC LIMIT ?",
            (machine_id, limit),
        )

    # ---------------------------------------------------------------- telemetry
    def sensor_series(
        self,
        machine_id: str,
        tag: str,
        *,
        hours: float = 6.0,
        limit: int = 1500,
    ) -> dict[str, Any]:
        """Read one tag's history back from the time-series store.

        Downsampled by striding rather than averaging: the dashboard is looking
        for shape and excursions, and averaging would hide the spikes that make a
        precursor visible.
        """
        telemetry = self._storage.telemetry
        backend = type(telemetry).__name__
        points: list[dict[str, Any]] = []

        if hasattr(telemetry, "root"):  # Parquet
            points = self._read_parquet_series(
                Path(telemetry.root), machine_id, tag, hours, limit
            )
        elif hasattr(telemetry, "_connect") and hasattr(telemetry, "_database"):  # ClickHouse
            points = self._read_clickhouse_series(telemetry, machine_id, tag, hours, limit)
        elif hasattr(telemetry, "_connect"):  # Timescale
            points = self._read_timescale_series(telemetry, machine_id, tag, hours, limit)
        else:  # pragma: no cover - unknown backend
            raise TelemetryUnavailable(f"cannot read history from {backend}")

        spec = self._one(
            "SELECT * FROM sensors WHERE machine_id = ? AND tag = ?", (machine_id, tag)
        )
        return {
            "machine_id": machine_id,
            "tag": tag,
            "unit": (spec or {}).get("unit") or "",
            "warn_low": (spec or {}).get("warn_low"),
            "warn_high": (spec or {}).get("warn_high"),
            "alarm_low": (spec or {}).get("alarm_low"),
            "alarm_high": (spec or {}).get("alarm_high"),
            "points": points,
            "backend": backend,
        }

    @staticmethod
    def _read_parquet_series(
        root: Path, machine_id: str, tag: str, hours: float, limit: int
    ) -> list[dict[str, Any]]:
        import pyarrow.compute as pc
        import pyarrow.dataset as ds

        files = sorted(root.rglob("*.parquet"))
        if not files:
            return []
        dataset = ds.dataset([str(path) for path in files], format="parquet")
        # Filter in Arrow rather than in Python: the dataset can be tens of
        # millions of rows and only a few thousand are wanted.
        table = dataset.to_table(
            columns=["timestamp", "value", "quality"],
            filter=(pc.field("machine_id") == machine_id) & (pc.field("tag") == tag),
        )
        if table.num_rows == 0:
            return []
        rows = table.sort_by([("timestamp", "ascending")]).to_pylist()
        cutoff = rows[-1]["timestamp"] - timedelta(hours=hours)
        rows = [row for row in rows if row["timestamp"] >= cutoff]
        return DataService._stride(rows, limit)

    @staticmethod
    def _read_clickhouse_series(
        store, machine_id: str, tag: str, hours: float, limit: int
    ) -> list[dict[str, Any]]:
        client = store._connect()
        rows = client.execute(
            f"SELECT ts, value, quality FROM {store._database}.{store._table} "
            f"WHERE machine_id = %(m)s AND tag = %(t)s "
            f"AND ts >= (SELECT max(ts) FROM {store._database}.{store._table}) "
            f"    - INTERVAL %(h)s HOUR "
            f"ORDER BY ts",
            {"m": machine_id, "t": tag, "h": int(hours)},
        )
        return DataService._stride(
            [{"timestamp": ts, "value": value, "quality": str(quality)} for ts, value, quality in rows],
            limit,
        )

    @staticmethod
    def _read_timescale_series(
        store, machine_id: str, tag: str, hours: float, limit: int
    ) -> list[dict[str, Any]]:
        connection = store._connect()
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT ts, value, quality FROM {store._schema}.{store._table} "
                f"WHERE machine_id = %s AND tag = %s "
                f"AND ts >= (SELECT max(ts) FROM {store._schema}.{store._table}) "
                f"    - make_interval(hours => %s) ORDER BY ts",
                (machine_id, tag, int(hours)),
            )
            rows = cursor.fetchall()
        quality_names = {1: "GOOD", 2: "UNCERTAIN", 3: "BAD"}
        return DataService._stride(
            [
                {
                    "timestamp": ts,
                    "value": value,
                    "quality": quality_names.get(quality, "GOOD"),
                }
                for ts, value, quality in rows
            ],
            limit,
        )

    @staticmethod
    def _stride(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        if len(rows) <= limit:
            selected = rows
        else:
            step = len(rows) // limit + 1
            # Keep the final point so the chart ends on the latest reading.
            selected = rows[::step]
            if selected[-1] is not rows[-1]:
                selected.append(rows[-1])
        return [
            {
                "t": row["timestamp"].isoformat()
                if isinstance(row["timestamp"], datetime)
                else str(row["timestamp"]),
                "v": None if row["value"] is None else round(float(row["value"]), 4),
                "q": row.get("quality", "GOOD"),
            }
            for row in selected
        ]

    # ------------------------------------------------------------------ batches
    def batches(self, limit: int = 300, disposition: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE disposition = ?" if disposition else ""
        params = (disposition, limit) if disposition else (limit,)
        return self._rows(
            f"SELECT * FROM batches {where} ORDER BY created_at DESC LIMIT ?", params
        )

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        batch = self._one("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
        if batch is None:
            return None
        batch["stages"] = self._rows(
            "SELECT * FROM batch_stages WHERE batch_id = ? ORDER BY sequence", (batch_id,)
        )
        batch["qc_results"] = self._rows(
            "SELECT * FROM qc_results WHERE batch_id = ? ORDER BY timestamp, parameter",
            (batch_id,),
        )
        batch["deviations"] = self._rows(
            "SELECT * FROM deviations WHERE batch_id = ?", (batch_id,)
        )
        failure_ids = [f for f in (batch.get("failure_ids") or "").split(",") if f]
        batch["failures"] = [
            row
            for failure_id in failure_ids
            for row in self._rows(
                "SELECT * FROM failures WHERE failure_id = ?", (failure_id,)
            )
        ]
        rca_ids = [d["rca_id"] for d in batch["deviations"] if d.get("rca_id")]
        batch["rca"] = [
            row
            for rca_id in rca_ids
            for row in self._rows("SELECT * FROM rca WHERE rca_id = ?", (rca_id,))
        ]
        capa_ids = [d["capa_id"] for d in batch["deviations"] if d.get("capa_id")]
        batch["capa"] = [
            row
            for capa_id in capa_ids
            for row in self._rows("SELECT * FROM capa WHERE capa_id = ?", (capa_id,))
        ]
        return batch

    def batch_timeline(self, batch_id: str) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT event_id, timestamp, event_type, category, severity, machine_id, "
            "unit_id, payload FROM events WHERE batch_id = ? "
            "ORDER BY timestamp, event_id",
            (batch_id,),
        )

    # ------------------------------------------------------------------ quality
    def qc_results(self, limit: int = 500, result: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE result = ?" if result else ""
        params = (result, limit) if result else (limit,)
        return self._rows(
            f"SELECT * FROM qc_results {where} ORDER BY timestamp DESC LIMIT ?", params
        )

    def qc_by_parameter(self) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT parameter, parameter_name, phase, COUNT(*) AS tests, "
            "SUM(CASE WHEN result IN ('FAIL','OOS') THEN 1 ELSE 0 END) AS failures, "
            "SUM(CASE WHEN result = 'OOT' THEN 1 ELSE 0 END) AS out_of_trend, "
            "AVG(actual_value) AS mean_value, AVG(target) AS target "
            "FROM qc_results GROUP BY parameter ORDER BY failures DESC, tests DESC"
        )

    def deviations(self, limit: int = 300) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM deviations ORDER BY detected_at DESC LIMIT ?", (limit,)
        )

    def rca_reports(self, limit: int = 300) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM rca ORDER BY completed_at DESC LIMIT ?", (limit,))

    def rca_report(self, rca_id: str) -> dict[str, Any] | None:
        report = self._one("SELECT * FROM rca WHERE rca_id = ?", (rca_id,))
        if report is None:
            return None
        report["evidence"] = self._rows(
            "SELECT * FROM rca_evidence WHERE rca_id = ? ORDER BY weight DESC", (rca_id,)
        )
        return report

    def capas(self, limit: int = 300) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM capa ORDER BY opened_at DESC LIMIT ?", (limit,))

    # -------------------------------------------------------------- reliability
    def failures(self, limit: int = 300) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM failures ORDER BY detected_at DESC LIMIT ?", (limit,)
        )

    def failures_by_category(self) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT category, COUNT(*) AS count, SUM(downtime_minutes) AS downtime_minutes, "
            "SUM(production_loss_units) AS production_loss "
            "FROM failures GROUP BY category ORDER BY downtime_minutes DESC"
        )

    def maintenance(self, limit: int = 300) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM maintenance ORDER BY scheduled_time DESC LIMIT ?", (limit,)
        )

    # ------------------------------------------------------------------- people
    def employees(self, unit_id: str | None = None) -> list[dict[str, Any]]:
        where = "WHERE unit_id = ?" if unit_id else ""
        params = (unit_id,) if unit_id else ()
        return self._rows(
            f"SELECT * FROM employees {where} ORDER BY employee_id", params
        )

    def shifts(self, limit: int = 120) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT s.*, "
            "(SELECT AVG(o.oee) FROM oee_snapshots o "
            "  WHERE o.scope = 'SHIFT' AND o.shift_instance_id = s.shift_instance_id) AS oee "
            "FROM shift_instances s ORDER BY start_time DESC LIMIT ?",
            (limit,),
        )

    def employee_events(self, limit: int = 300) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM employee_events ORDER BY timestamp DESC LIMIT ?", (limit,)
        )

    # -------------------------------------------------------------------- other
    def events(
        self, limit: int = 200, category: str | None = None, severity: str | None = None
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if category:
            clauses.append("category = ?")
            params.append(category)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        return self._rows(
            f"SELECT * FROM events {where} ORDER BY timestamp DESC, event_id DESC LIMIT ?",
            tuple(params),
        )

    def production_trend(self, limit: int = 90) -> list[dict[str, Any]]:
        """Good and reject quantity per shift — same unit, so one axis is honest."""
        return self._rows(
            "SELECT s.shift_instance_id, s.business_date, s.shift_code, s.start_time, "
            "SUM(p.good_quantity) AS good_quantity, SUM(p.reject_quantity) AS reject_quantity, "
            "AVG(p.oee) AS oee, SUM(p.downtime_seconds) AS downtime_seconds "
            "FROM shift_instances s JOIN production_records p "
            "  ON p.shift_instance_id = s.shift_instance_id "
            "GROUP BY s.shift_instance_id ORDER BY s.start_time DESC LIMIT ?",
            (limit,),
        )

    def oee_trend(self, scope: str = "PLANT", limit: int = 90) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT o.shift_instance_id, s.start_time, s.shift_code, "
            "o.availability, o.performance, o.quality, o.oee "
            "FROM oee_snapshots o JOIN shift_instances s "
            "  ON s.shift_instance_id = o.shift_instance_id "
            "WHERE o.scope = ? ORDER BY s.start_time DESC LIMIT ?",
            (scope, limit),
        )

    def integrity(self) -> dict[str, Any]:
        report = self._storage.verify_integrity()
        return {
            "ok": report.ok,
            "checks": [
                {"name": name, "ok": ok, "detail": detail}
                for name, ok, detail in report.checks
            ],
        }

    def table_counts(self) -> dict[str, int]:
        from pharma_sim.storage.schema import TABLES

        counts = {}
        for name in TABLES:
            row = self._one(f"SELECT COUNT(*) AS n FROM {name}")
            counts[name] = int(row["n"]) if row else 0
        return counts
