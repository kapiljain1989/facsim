"""Maintenance engine.

Maintenance closes a feedback loop rather than just producing records. Deferring
preventive work accrues maintenance debt, which raises the hazard multiplier and
genuinely makes a failure more likely; a completed repair clears degradation and
relieves accumulated sensor drift. That is what makes "maintenance history
affects future failure probability" (§21) a property of the data rather than a
claim about it.

Predictive maintenance is where failures get averted: when a machine raises a
warning, the plant sometimes acts in time. The episode is then marked averted, so
the prediction labels do not assert a failure that never happened.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from pharma_sim.domain.machine import DegradationEpisode, Machine
from pharma_sim.engine.context import SimContext
from pharma_sim.engine.scheduler import Priority

__all__ = ["MaintenanceRecord", "MaintenanceEngine"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MaintenanceRecord:
    """One maintenance action, carrying the §21 field set."""

    maintenance_id: str
    machine_id: str
    unit_id: str
    maintenance_type: str
    scheduled_time: datetime
    actual_time: datetime | None
    completed_time: datetime | None
    technician_id: str | None
    failure_id: str | None
    parts_replaced: tuple[str, ...]
    duration_hours: float
    cost: float
    status: str
    triggered_by: str
    run_id: str

    def as_row(self) -> dict[str, Any]:
        return {
            "maintenance_id": self.maintenance_id,
            "machine_id": self.machine_id,
            "unit_id": self.unit_id,
            "maintenance_type": self.maintenance_type,
            "scheduled_time": self.scheduled_time,
            "actual_time": self.actual_time,
            "completed_time": self.completed_time,
            "technician_id": self.technician_id,
            "failure_id": self.failure_id,
            "parts_replaced": ",".join(self.parts_replaced),
            "duration_hours": round(self.duration_hours, 3),
            "cost": round(self.cost, 2),
            "status": self.status,
            "triggered_by": self.triggered_by,
            "run_id": self.run_id,
        }


class MaintenanceEngine:
    """Schedules and executes preventive, predictive and corrective maintenance."""

    def __init__(
        self,
        ctx: SimContext,
        *,
        on_complete: Callable[[Machine, MaintenanceRecord, list[DegradationEpisode]], None]
        | None = None,
    ) -> None:
        self._ctx = ctx
        self._records: dict[str, MaintenanceRecord] = {}
        self._on_complete = on_complete
        self._busy_technicians: dict[str, datetime] = {}
        self._averted = 0
        self._deferred = 0

    @property
    def records(self) -> dict[str, MaintenanceRecord]:
        return self._records

    @property
    def averted_count(self) -> int:
        return self._averted

    @property
    def deferred_count(self) -> int:
        return self._deferred

    # ------------------------------------------------------------------ scanning
    def scan_preventive(self, now: datetime) -> int:
        """Look for machines approaching their PM interval.

        A configured share of due PMs is deferred rather than done, which is what
        creates the overdue-maintenance population that later turns up as RCA
        evidence and as a raised hazard.
        """
        policy = self._ctx.config.maintenance
        lead = timedelta(hours=policy.pm_lead_time_hours)
        scheduled = 0

        for machine in self._ctx.plant.machines.values():
            if machine.maintenance_pending:
                continue
            if machine.pm_due_at > now + lead:
                continue
            rng = self._ctx.rngs.child("pm", machine.machine_id)
            if rng.random() < policy.pm_deferral_probability:
                machine.defer_pm(policy.pm_deferral_hours)
                self._deferred += 1
                record = self._new_record(
                    machine,
                    "PREVENTIVE",
                    now,
                    duration_hours=machine.spec.pm_duration_hours,
                    parts=(),
                    cost=0.0,
                    status="DEFERRED",
                    triggered_by="SCHEDULE",
                )
                self._ctx.bus.publish(
                    "MAINTENANCE_DEFERRED",
                    now,
                    unit_id=machine.unit_id,
                    machine_id=machine.machine_id,
                    severity="MINOR",
                    payload={
                        "maintenance_id": record.maintenance_id,
                        "deferred_hours": policy.pm_deferral_hours,
                    },
                )
                self._persist(record)
                continue

            self.schedule(
                machine,
                "PREVENTIVE",
                now,
                start_delay_hours=max(
                    0.0, (machine.pm_due_at - now).total_seconds() / 3600.0
                ),
                duration_hours=machine.spec.pm_duration_hours,
                triggered_by="SCHEDULE",
            )
            scheduled += 1
        return scheduled

    def consider_predictive(self, machine: Machine, episode: DegradationEpisode) -> None:
        """Decide whether to act on a warning before the scheduled fault."""
        policy = self._ctx.config.maintenance
        rng = self._ctx.rngs.child("predictive", machine.machine_id, episode.episode_id)
        if rng.random() >= policy.predictive_response_probability:
            return
        delay = policy.predictive_response_delay_hours * rng.uniform(0.5, 1.5)
        # Only worth scheduling if it would actually beat the fault.
        if self._ctx.clock.now + timedelta(hours=delay) >= episode.fault_at:
            return
        self.schedule(
            machine,
            "PREDICTIVE",
            self._ctx.clock.now,
            start_delay_hours=delay,
            duration_hours=episode.mode.spec.repair.duration_hours * 0.6,
            parts=tuple(episode.mode.spec.repair.parts),
            cost=episode.mode.spec.repair.cost * 0.5,
            triggered_by=f"WARNING:{episode.episode_id}",
        )

    def schedule_corrective(self, machine: Machine, failure_id: str, mode_repair) -> None:
        """Queue the repair for a machine that has already faulted."""
        policy = self._ctx.config.maintenance
        maintenance_type = "EMERGENCY" if mode_repair.duration_hours >= 8.0 else "CORRECTIVE"
        self.schedule(
            machine,
            maintenance_type,
            self._ctx.clock.now,
            start_delay_hours=policy.corrective_delay_hours,
            duration_hours=mode_repair.duration_hours,
            parts=tuple(mode_repair.parts),
            cost=mode_repair.cost,
            failure_id=failure_id,
            triggered_by=f"FAILURE:{failure_id}",
        )

    # ---------------------------------------------------------------- scheduling
    def schedule(
        self,
        machine: Machine,
        maintenance_type: str,
        now: datetime,
        *,
        start_delay_hours: float,
        duration_hours: float,
        parts: tuple[str, ...] = (),
        cost: float = 0.0,
        failure_id: str | None = None,
        triggered_by: str = "",
    ) -> MaintenanceRecord:
        start_at = now + timedelta(hours=start_delay_hours)
        record = self._new_record(
            machine,
            maintenance_type,
            start_at,
            duration_hours=duration_hours,
            parts=parts,
            cost=cost,
            status="SCHEDULED",
            triggered_by=triggered_by,
            failure_id=failure_id,
        )
        machine.mark_maintenance_pending()

        self._ctx.bus.publish(
            "MAINTENANCE_SCHEDULED",
            now,
            unit_id=machine.unit_id,
            machine_id=machine.machine_id,
            payload={
                "maintenance_id": record.maintenance_id,
                "maintenance_type": maintenance_type,
                "scheduled_time": start_at.isoformat(),
            },
        )
        self._ctx.scheduler.at(
            start_at,
            self._make_start_callback(machine, record),
            priority=Priority.MAINTENANCE,
            label=f"maint-start:{machine.machine_id}",
        )
        return record

    def _make_start_callback(
        self, machine: Machine, record: MaintenanceRecord
    ) -> Callable[[datetime], None]:
        def callback(now: datetime) -> None:
            maintenance_state = self._ctx.registries.states.first("maintenance")
            if not machine.force_route_to(maintenance_state, now, f"MAINT:{record.maintenance_type}"):
                # The machine cannot legally reach maintenance right now (mid-repair
                # already). Retry once a little later rather than dropping the work.
                self._ctx.scheduler.at(
                    now + timedelta(hours=1.0),
                    callback,
                    priority=Priority.MAINTENANCE,
                    label=f"maint-retry:{machine.machine_id}",
                )
                return

            record.actual_time = now
            record.status = "IN_PROGRESS"
            record.technician_id = self._assign_technician(now, record.duration_hours)

            self._ctx.bus.publish(
                "MAINTENANCE_STARTED",
                now,
                unit_id=machine.unit_id,
                machine_id=machine.machine_id,
                employee_id=record.technician_id,
                payload={
                    "maintenance_id": record.maintenance_id,
                    "maintenance_type": record.maintenance_type,
                },
            )
            self._ctx.scheduler.at(
                now + timedelta(hours=record.duration_hours),
                self._make_complete_callback(machine, record),
                priority=Priority.MAINTENANCE,
                label=f"maint-end:{machine.machine_id}",
            )

        return callback

    def _make_complete_callback(
        self, machine: Machine, record: MaintenanceRecord
    ) -> Callable[[datetime], None]:
        def callback(now: datetime) -> None:
            policy = self._ctx.config.maintenance
            effectiveness = (
                policy.corrective_effectiveness
                if record.maintenance_type in {"CORRECTIVE", "EMERGENCY"}
                else max(
                    (
                        episode.mode.spec.pm_effectiveness
                        for episode in machine.active_episodes()
                    ),
                    default=0.9,
                )
            )
            resolved = machine.resolve_episodes(now, effectiveness)
            self._averted += sum(1 for episode in resolved if episode.averted)

            for episode in resolved:
                if episode.averted:
                    self._ctx.bus.publish(
                        "DEGRADATION_AVERTED",
                        now,
                        unit_id=machine.unit_id,
                        machine_id=machine.machine_id,
                        payload={
                            "failure_mode": episode.mode_id,
                            "episode_id": episode.episode_id,
                        },
                    )

            machine.record_maintenance(now, record.maintenance_type, effectiveness)
            machine.plc.clear_all_alarms()

            record.completed_time = now
            record.status = "COMPLETED"
            record.cost += policy.hourly_labour_cost * record.duration_hours
            self._persist(record)

            self._ctx.bus.publish(
                "MAINTENANCE_COMPLETED",
                now,
                unit_id=machine.unit_id,
                machine_id=machine.machine_id,
                employee_id=record.technician_id,
                payload={
                    "maintenance_id": record.maintenance_id,
                    "maintenance_type": record.maintenance_type,
                    "duration_hours": round(record.duration_hours, 3),
                },
            )

            idle_state = self._ctx.registries.states.first("idle")
            machine.force_route_to(idle_state, now, "MAINTENANCE_COMPLETE")
            if self._on_complete is not None:
                self._on_complete(machine, record, resolved)

        return callback

    # ------------------------------------------------------------------ internals
    def _assign_technician(self, now: datetime, duration_hours: float) -> str | None:
        """Pick a free technician, preferring the longest idle one.

        A finite pool means a busy plant queues repairs, which is where downtime
        beyond the nominal repair duration comes from.
        """
        role = self._ctx.registries.topology.technician_role
        technicians = self._ctx.plant.technicians(role)
        if not technicians:
            return None
        for technician in technicians:
            busy_until = self._busy_technicians.get(technician.employee_id)
            if busy_until is None or busy_until <= now:
                self._busy_technicians[technician.employee_id] = now + timedelta(
                    hours=duration_hours
                )
                return technician.employee_id
        # Everyone is busy: the soonest-free technician takes it.
        chosen = min(technicians, key=lambda emp: self._busy_technicians[emp.employee_id])
        self._busy_technicians[chosen.employee_id] = (
            self._busy_technicians[chosen.employee_id] + timedelta(hours=duration_hours)
        )
        return chosen.employee_id

    def _new_record(
        self,
        machine: Machine,
        maintenance_type: str,
        scheduled_time: datetime,
        *,
        duration_hours: float,
        parts: tuple[str, ...],
        cost: float,
        status: str,
        triggered_by: str,
        failure_id: str | None = None,
    ) -> MaintenanceRecord:
        record = MaintenanceRecord(
            maintenance_id=self._ctx.ids.maintenance(),
            machine_id=machine.machine_id,
            unit_id=machine.unit_id,
            maintenance_type=maintenance_type,
            scheduled_time=scheduled_time,
            actual_time=None,
            completed_time=None,
            technician_id=None,
            failure_id=failure_id,
            parts_replaced=parts,
            duration_hours=duration_hours,
            cost=cost,
            status=status,
            triggered_by=triggered_by,
            run_id=self._ctx.run_id,
        )
        self._records[record.maintenance_id] = record
        return record

    def _persist(self, record: MaintenanceRecord) -> None:
        self._ctx.records.write("maintenance", record.as_row())
