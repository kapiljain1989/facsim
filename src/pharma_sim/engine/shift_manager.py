"""Shift lifecycle: rosters, attendance, breaks and machine assignment.

Shifts are the heartbeat of the plant. A shift start decides who turned up, which
machines therefore have an operator, and how experienced that operator is — which
in turn feeds the reject rate, the setup-error probability and the failure hazard.
A shift end closes out production accounting and computes OEE.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from pharma_sim.domain.employee import Employee, EmployeeAttendance, draw_attendance
from pharma_sim.domain.machine import ProductionWindow
from pharma_sim.domain.oee import aggregate_windows, compute_oee
from pharma_sim.domain.shift import ShiftInstance, ShiftScheduler
from pharma_sim.engine.context import SimContext
from pharma_sim.engine.scheduler import Priority

__all__ = ["ShiftManager"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ShiftStats:
    shifts_started: int = 0
    shifts_ended: int = 0
    absences: int = 0
    overtime_events: int = 0
    assignments: int = 0


class ShiftManager:
    """Drives shift starts and ends, and everything that hangs off them."""

    def __init__(self, ctx: SimContext, scheduler: ShiftScheduler) -> None:
        self._ctx = ctx
        self._shifts = scheduler
        self._instances: dict[str, ShiftInstance] = {}
        self._current: ShiftInstance | None = None
        self._attendance: dict[str, EmployeeAttendance] = {}
        self._stats = ShiftStats()
        self._horizon: datetime | None = None

    @property
    def stats(self) -> ShiftStats:
        return self._stats

    @property
    def current(self) -> ShiftInstance | None:
        return self._current

    @property
    def instances(self) -> dict[str, ShiftInstance]:
        return self._instances

    def current_instance_id(self) -> str | None:
        return self._current.shift_instance_id if self._current else None

    def set_horizon(self, horizon: datetime | None) -> None:
        """Stop chaining new shifts past the end of the run."""
        self._horizon = horizon

    # ------------------------------------------------------------------ bootstrap
    def bootstrap(self, now: datetime) -> None:
        """Open whichever shift already covers ``now``, then chain forward.

        Runs usually start mid-shift, so the plant should not sit idle until the
        next boundary.
        """
        resolved = self._shifts.shift_for(now)
        if resolved is not None:
            business_date, code = resolved
            self._begin(business_date, code, now)
        self._schedule_next(now)

    def _schedule_next(self, after: datetime) -> None:
        start, business_date, code = self._shifts.first_start_at_or_after(
            after + timedelta(seconds=1)
        )
        if self._horizon is not None and start > self._horizon:
            return
        self._ctx.scheduler.at(
            start,
            lambda now: self._on_shift_start(business_date, code, now),
            priority=Priority.SHIFT,
            label=f"shift-start:{code}",
        )

    def _on_shift_start(self, business_date: date, code: str, now: datetime) -> None:
        self._begin(business_date, code, now)
        self._schedule_next(now)

    # ------------------------------------------------------------------- shift start
    def _begin(self, business_date: date, code: str, now: datetime) -> None:
        instance = self._shifts.instance(
            business_date, code, self._ctx.ids.shift_instance()
        )
        roster = self._ctx.plant.employees_on_shift(code)
        instance.roster = [employee.employee_id for employee in roster]

        rng = self._ctx.rngs.child("attendance", instance.shift_instance_id)
        absenteeism = self._shifts.config.absenteeism_rate
        present: list[Employee] = []

        for employee in roster:
            attending = draw_attendance(employee, rng, absenteeism)
            record = EmployeeAttendance(
                employee_id=employee.employee_id,
                shift_instance_id=instance.shift_instance_id,
                present=attending,
            )
            self._attendance[employee.employee_id] = record
            if attending:
                instance.present.append(employee.employee_id)
                present.append(employee)
            else:
                instance.absent.append(employee.employee_id)
                self._stats.absences += 1

        # The instance row must exist before any event references it, because the
        # event table's foreign key is enforced.
        self._instances[instance.shift_instance_id] = instance
        self._current = instance
        self._ctx.records.write("shift_instances", instance.as_row())

        self._ctx.bus.publish(
            "SHIFT_STARTED",
            now,
            shift_instance_id=instance.shift_instance_id,
            payload={
                "shift_code": code,
                "shift_instance_id": instance.shift_instance_id,
                "roster": len(instance.roster),
                "present": len(instance.present),
                "absent": len(instance.absent),
            },
        )
        self._stats.shifts_started += 1

        for employee_id in instance.absent:
            employee = self._ctx.plant.employee(employee_id)
            self._ctx.bus.publish(
                "EMPLOYEE_ABSENT",
                now,
                unit_id=employee.unit_id,
                employee_id=employee_id,
                shift_instance_id=instance.shift_instance_id,
                severity="MINOR",
                payload={"shift_instance_id": instance.shift_instance_id},
            )

        self._schedule_people(instance, present, rng, now)
        self._assign_machines(instance, present, now)

        end = max(instance.end, now + timedelta(minutes=1))
        self._ctx.scheduler.at(
            end,
            lambda moment: self._on_shift_end(instance, moment),
            priority=Priority.SHIFT,
            label=f"shift-end:{code}",
        )

    def _schedule_people(
        self, instance: ShiftInstance, present: list[Employee], rng, now: datetime
    ) -> None:
        """Clock-ins, breaks, clock-outs and occasional overtime."""
        config = self._shifts.config
        for employee in present:
            record = self._attendance[employee.employee_id]

            jitter = rng.uniform(-config.clock_in_jitter_min, config.clock_in_jitter_min)
            clock_in = max(now, instance.start + timedelta(minutes=jitter))
            record.clock_in = clock_in
            self._ctx.scheduler.at(
                clock_in,
                self._clock_event("EMPLOYEE_CLOCK_IN", employee, instance),
                priority=Priority.EMPLOYEE,
                label=f"clock-in:{employee.employee_id}",
            )

            for window in instance.breaks:
                if window.start <= now:
                    continue
                record.breaks_taken += 1
                self._ctx.scheduler.at(
                    window.start,
                    self._break_event("BREAK_START", employee, instance, window.label),
                    priority=Priority.EMPLOYEE,
                    label=f"break:{employee.employee_id}",
                )
                self._ctx.scheduler.at(
                    window.end,
                    self._break_event("BREAK_END", employee, instance, window.label),
                    priority=Priority.EMPLOYEE,
                    label=f"break-end:{employee.employee_id}",
                )

            overtime = 0.0
            if rng.random() < config.overtime_probability:
                overtime = config.overtime_duration_min * rng.uniform(0.5, 1.5)
                record.overtime_minutes = overtime
                self._stats.overtime_events += 1
                self._ctx.scheduler.at(
                    instance.end,
                    self._overtime_event(employee, instance, overtime),
                    priority=Priority.EMPLOYEE,
                    label=f"overtime:{employee.employee_id}",
                )

            out_jitter = rng.uniform(0.0, config.clock_out_jitter_min)
            clock_out = instance.end + timedelta(minutes=overtime + out_jitter)
            record.clock_out = clock_out
            self._ctx.scheduler.at(
                clock_out,
                self._clock_event("EMPLOYEE_CLOCK_OUT", employee, instance),
                priority=Priority.EMPLOYEE,
                label=f"clock-out:{employee.employee_id}",
            )

    def _clock_event(
        self, event_type: str, employee: Employee, instance: ShiftInstance
    ) -> Callable[[datetime], None]:
        def callback(now: datetime) -> None:
            self._ctx.bus.publish(
                event_type,
                now,
                unit_id=employee.unit_id,
                employee_id=employee.employee_id,
                shift_instance_id=instance.shift_instance_id,
                payload={"shift_instance_id": instance.shift_instance_id},
            )

        return callback

    def _break_event(
        self, event_type: str, employee: Employee, instance: ShiftInstance, label: str
    ) -> Callable[[datetime], None]:
        def callback(now: datetime) -> None:
            self._ctx.bus.publish(
                event_type,
                now,
                unit_id=employee.unit_id,
                employee_id=employee.employee_id,
                shift_instance_id=instance.shift_instance_id,
                payload={"label": label},
            )

        return callback

    def _overtime_event(
        self, employee: Employee, instance: ShiftInstance, minutes: float
    ) -> Callable[[datetime], None]:
        def callback(now: datetime) -> None:
            self._ctx.bus.publish(
                "OVERTIME_STARTED",
                now,
                unit_id=employee.unit_id,
                employee_id=employee.employee_id,
                shift_instance_id=instance.shift_instance_id,
                payload={"minutes": round(minutes, 1)},
            )

        return callback

    # -------------------------------------------------------------- assignment
    def _assign_machines(
        self, instance: ShiftInstance, present: list[Employee], now: datetime
    ) -> None:
        """Spread present operators across their unit's machines.

        Certification is preferred but not guaranteed: a short-staffed shift can
        put a less suitable operator on a machine, which raises that machine's
        inexperience and, through the hazard model, its failure probability.
        """
        states = self._ctx.registries.states
        by_unit: dict[str, list[Employee]] = {}
        for employee in present:
            if employee.unit_id is None:
                continue
            if employee.role in {
                self._ctx.registries.topology.technician_role,
                self._ctx.registries.topology.qc_analyst_role,
            }:
                continue
            by_unit.setdefault(employee.unit_id, []).append(employee)

        idle_state = states.first("idle")
        # A model with no offline state parks unstaffed machines in idle instead.
        offline_state = states.first_or_none("offline") or idle_state

        for unit_id, unit in self._ctx.plant.units.items():
            operators = by_unit.get(unit_id, [])
            if not operators:
                for machine in unit.machines:
                    machine.accrue_time(now)
                    if machine.current_batch_id is not None:
                        # Never abandon a machine mid-batch at a shift boundary:
                        # taking it offline would zero its process readings and
                        # fail QC for a stage that was running correctly.
                        continue
                    if machine.is_continuous:
                        # A utility on continuous duty is unattended by
                        # definition. Powering the purified-water system down
                        # because no operator clocked in would be wrong.
                        continue
                    machine.assign_operators([], 1.0)
                    machine.force_route_to(offline_state, now, "NO_OPERATOR")
                continue

            attended = [m for m in unit.machines if not m.is_continuous]
            for index, machine in enumerate(attended):
                # Prefer a certified operator; fall back to round-robin.
                certified = [
                    operator
                    for operator in operators
                    if operator.certified_for(machine.equipment_class)
                ]
                pool = certified or operators
                operator = pool[index % len(pool)]
                machine.assign_operators([operator.employee_id], operator.inexperience)
                self._attendance[operator.employee_id].assigned_machines.append(
                    machine.machine_id
                )
                self._stats.assignments += 1

                self._ctx.bus.publish(
                    "MACHINE_ASSIGNED",
                    now,
                    unit_id=unit_id,
                    machine_id=machine.machine_id,
                    employee_id=operator.employee_id,
                    shift_instance_id=instance.shift_instance_id,
                    payload={
                        "shift_instance_id": instance.shift_instance_id,
                        "certified": bool(certified),
                    },
                )

                machine.accrue_time(now)
                if states.is_offline(machine.state) or machine.state == offline_state:
                    machine.force_route_to(idle_state, now, "SHIFT_STAFFED")

    # ---------------------------------------------------------------- shift end
    def _on_shift_end(self, instance: ShiftInstance, now: datetime) -> None:
        self._flush_production(instance, now)
        for employee_id in instance.present:
            record = self._attendance.get(employee_id)
            if record is not None:
                self._ctx.records.write(
                    "employee_events",
                    {
                        "event_id": f"ATT-{instance.shift_instance_id}-{employee_id}",
                        "timestamp": record.clock_in or now,
                        "employee_id": employee_id,
                        "event_type": "ATTENDANCE_SUMMARY",
                        "shift_instance_id": instance.shift_instance_id,
                        "unit_id": self._ctx.plant.employee(employee_id).unit_id,
                        "machine_id": None,
                        "payload": {
                            "assigned_machines": record.assigned_machines,
                            "overtime_minutes": record.overtime_minutes,
                            "breaks_taken": record.breaks_taken,
                        },
                        "run_id": self._ctx.run_id,
                    },
                )

        self._ctx.bus.publish(
            "SHIFT_ENDED",
            now,
            shift_instance_id=instance.shift_instance_id,
            payload={
                "shift_code": instance.shift_code,
                "shift_instance_id": instance.shift_instance_id,
            },
        )
        self._stats.shifts_ended += 1

    def _flush_production(self, instance: ShiftInstance, now: datetime) -> None:
        """Close out the shift's production accounting and compute OEE."""
        per_unit: dict[str, list[ProductionWindow]] = {}
        plant_windows: list[ProductionWindow] = []

        for machine in self._ctx.plant.machines.values():
            machine.accrue_time(now)
            window = machine.flush_shift_window()
            if (
                window.runtime_seconds <= 0.0
                and window.idle_seconds <= 0.0
                and window.downtime_seconds <= 0.0
                and window.offline_seconds <= 0.0
            ):
                continue
            oee = compute_oee(window)
            row = {
                "machine_id": machine.machine_id,
                "shift_instance_id": instance.shift_instance_id,
                "unit_id": machine.unit_id,
                "equipment_class": machine.equipment_class,
                **window.as_row(),
                "availability": round(oee.availability, 5),
                "performance": round(oee.performance, 5),
                "quality": round(oee.quality, 5),
                "oee": round(oee.oee, 5),
                "utilisation": round(oee.utilisation, 5),
                "run_id": self._ctx.run_id,
            }
            self._ctx.records.write("production_records", row)
            self._ctx.records.write(
                "oee_snapshots",
                {
                    "scope": "MACHINE",
                    "scope_id": machine.machine_id,
                    "shift_instance_id": instance.shift_instance_id,
                    **oee.as_row(),
                    "run_id": self._ctx.run_id,
                },
            )
            per_unit.setdefault(machine.unit_id, []).append(window)
            plant_windows.append(window)

        for unit_id, windows in per_unit.items():
            oee = compute_oee(aggregate_windows(windows))
            self._ctx.records.write(
                "oee_snapshots",
                {
                    "scope": "UNIT",
                    "scope_id": unit_id,
                    "shift_instance_id": instance.shift_instance_id,
                    **oee.as_row(),
                    "run_id": self._ctx.run_id,
                },
            )

        if plant_windows:
            oee = compute_oee(aggregate_windows(plant_windows))
            self._ctx.records.write(
                "oee_snapshots",
                {
                    "scope": "PLANT",
                    "scope_id": self._ctx.plant_id,
                    "shift_instance_id": instance.shift_instance_id,
                    **oee.as_row(),
                    "run_id": self._ctx.run_id,
                },
            )
            self._ctx.records.write(
                "oee_snapshots",
                {
                    "scope": "SHIFT",
                    "scope_id": instance.shift_code,
                    "shift_instance_id": instance.shift_instance_id,
                    **oee.as_row(),
                    "run_id": self._ctx.run_id,
                },
            )
