"""Failure engine.

Failures are random and rare, but never a flat coin flip. For every machine and
every mode that can affect it, a Weibull hazard is evaluated against the drivers
of §14 — age, accumulated operating hours, maintenance debt, load, environment
and operator experience:

    lambda = weibull(operating_hours; mtbf, beta) * age * hours * debt * load
             * environment * operator * hazard_scale
    P(initiate in dt) = 1 - exp(-lambda * dt)

When a mode initiates, the fault instant is *scheduled* rather than applied. The
machine then degrades toward it, driving precursor signals that a diagnostic
system can find, and giving the label writer an exact remaining-useful-life. If
maintenance intervenes first, the episode is averted.

The information boundary matters here: the operational failure record holds what
a plant would know at the time — category, severity, alarms, downtime. The mode
and its root cause go to the ground-truth store only.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from pharma_sim.domain.ground_truth import GroundTruthEvent, GroundTruthLedger
from pharma_sim.domain.machine import DegradationEpisode, Machine
from pharma_sim.engine.context import SimContext
from pharma_sim.engine.rng import probability_from_rate, weibull_hazard
from pharma_sim.engine.scheduler import Priority
from pharma_sim.registry.failures import ApplicableMode

__all__ = ["FailureRecord", "FailureEngine"]

logger = logging.getLogger(__name__)

#: Short, observable symptom text per failure category. This is what a plant
#: records at the moment of the stop, before anyone has diagnosed anything.
_SYMPTOMS: dict[str, str] = {
    "MECHANICAL": "Unplanned mechanical stop with abnormal vibration or load",
    "ELECTRICAL": "Drive tripped on electrical protection",
    "SENSOR": "Instrument reading rejected as unreliable",
    "PROCESS": "Process parameter outside its operating window",
    "MATERIAL": "Material-related interruption at the machine",
    "HUMAN": "Operation performed outside the validated procedure",
    "ENVIRONMENTAL": "Environmental condition outside the qualified band",
}


#: Failure modes that leave an observable operational trace at the moment they
#: begin. RCA may use these; it may not use the mode id itself.
_OBSERVABLE_INCIDENTS: dict[str, str] = {
    "MISSED_INSPECTION": "MISSED_INSPECTION",
    "RAW_MATERIAL_SHORTAGE": "MATERIAL_WAIT",
    "INCORRECT_PARAMETER": "PARAMETER_DEVIATION",
    "WRONG_SETUP": "SETUP_ERROR",
    "MATERIAL_VARIATION": "PARAMETER_DEVIATION",
}


@dataclass(slots=True)
class FailureRecord:
    """An operational failure record. Deliberately excludes the root cause."""

    failure_id: str
    machine_id: str
    unit_id: str
    equipment_class: str
    category: str
    severity: str
    symptom: str
    detected_at: datetime
    alarm_count: int
    state_before: str
    batch_id: str | None
    shift_instance_id: str | None
    operator_ids: tuple[str, ...]
    resolved_at: datetime | None = None
    downtime_minutes: float = 0.0
    production_loss_units: float = 0.0
    affected_batches: list[str] = field(default_factory=list)
    maintenance_id: str | None = None
    deviation_id: str | None = None
    run_id: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "failure_id": self.failure_id,
            "machine_id": self.machine_id,
            "unit_id": self.unit_id,
            "equipment_class": self.equipment_class,
            "category": self.category,
            "severity": self.severity,
            "symptom": self.symptom,
            "detected_at": self.detected_at,
            "resolved_at": self.resolved_at,
            "alarm_count": self.alarm_count,
            "state_before": self.state_before,
            "batch_id": self.batch_id,
            "shift_instance_id": self.shift_instance_id,
            "operator_ids": ",".join(self.operator_ids),
            "downtime_minutes": round(self.downtime_minutes, 2),
            "production_loss_units": round(self.production_loss_units, 2),
            "affected_batches": ",".join(self.affected_batches),
            "maintenance_id": self.maintenance_id,
            "deviation_id": self.deviation_id,
            "run_id": self.run_id,
        }


def _warning_progress(threshold: float) -> float:
    """Incubation fraction at which the exponential curve reaches ``threshold``.

    Inverts ``degradation_curve(p, "exponential")`` so the warning transition can
    be scheduled exactly rather than discovered by polling.
    """
    threshold = min(max(threshold, 0.0), 0.999)
    return math.log(threshold * (math.exp(3.0) - 1.0) + 1.0) / 3.0


class FailureEngine:
    """Evaluates hazards, schedules degradation, and raises warnings and faults."""

    def __init__(
        self,
        ctx: SimContext,
        ledger: GroundTruthLedger,
        *,
        on_fault: Callable[[Machine, FailureRecord, DegradationEpisode], None] | None = None,
        on_warning: Callable[[Machine, DegradationEpisode], None] | None = None,
    ) -> None:
        self._ctx = ctx
        self._ledger = ledger
        self._on_fault = on_fault
        self._on_warning = on_warning
        self._failures: dict[str, FailureRecord] = {}
        self._episode_counter = 0
        self._initiations = 0
        self._evaluations = 0

    # ------------------------------------------------------------------ metrics
    @property
    def failures(self) -> dict[str, FailureRecord]:
        return self._failures

    @property
    def initiation_count(self) -> int:
        return self._initiations

    @property
    def evaluation_count(self) -> int:
        return self._evaluations

    def failure(self, failure_id: str) -> FailureRecord | None:
        return self._failures.get(failure_id)

    # --------------------------------------------------------------- evaluation
    def evaluate_all(self, now: datetime, interval_hours: float) -> int:
        """Run one hazard evaluation pass across the plant.

        Only machines currently doing work are exposed to wear-driven hazards,
        which is what makes accumulated operating hours — rather than wall-clock
        time — the thing that ages a machine.
        """
        states = self._ctx.registries.states
        initiated = 0
        for machine in self._ctx.plant.machines.values():
            if states.is_downtime(machine.state) or states.is_offline(machine.state):
                continue
            if machine.active_mode_count() >= self._ctx.registries.failures.max_concurrent_modes:
                continue
            initiated += self._evaluate_machine(machine, now, interval_hours)
        return initiated

    def _evaluate_machine(
        self, machine: Machine, now: datetime, interval_hours: float
    ) -> int:
        registries = self._ctx.registries
        modes = registries.failures.for_class(machine.equipment_class)
        if not modes:
            return 0

        unit = self._ctx.plant.unit(machine.unit_id)
        ambient = self._ctx.environment.at(now)
        drivers = {
            "age_years": machine.age_years(now),
            "operating_khours": machine.operating_hours / 1000.0,
            "pm_overdue_ratio": machine.pm_overdue_ratio(now),
            "load_factor": machine.load_factor(),
            "environment_stress": ambient.stress * unit.spec.environment_sensitivity,
            "operator_inexperience": machine.operator_inexperience,
        }
        multiplier = registries.failures.hazard_multiplier(drivers)
        scale = registries.failures.hazard_scale
        initiated = 0

        for mode in modes:
            if machine.has_active_mode(mode.id):
                continue
            self._evaluations += 1
            # The Weibull clock is per-mode and resets when that mode is
            # repaired: this is a repairable system, not a component running to
            # first failure since commissioning.
            rate = (
                weibull_hazard(
                    max(machine.mode_age_hours(mode.id), 1.0),
                    mode.spec.mtbf_operating_hours,
                    mode.spec.weibull_beta,
                )
                * multiplier
                * scale
            )
            probability = probability_from_rate(rate, interval_hours)
            stream = self._ctx.rngs.child("hazard", machine.machine_id, mode.id)
            if stream.random() < probability:
                self.initiate(machine, mode, now)
                initiated += 1
                if machine.active_mode_count() >= registries.failures.max_concurrent_modes:
                    break
        return initiated

    # --------------------------------------------------------------- initiation
    def initiate(
        self,
        machine: Machine,
        mode: ApplicableMode,
        now: datetime,
        *,
        injected: bool = False,
        severity: str | None = None,
        incubation_hours: float | None = None,
    ) -> DegradationEpisode:
        """Begin an episode and fix the instant its fault will occur."""
        self._episode_counter += 1
        episode_id = f"EP-{self._episode_counter:06d}"
        rng = self._ctx.rngs.child("incubation", machine.machine_id, mode.id, episode_id)

        if incubation_hours is None:
            incubation_hours = rng.uniform(
                mode.spec.incubation_hours_min, mode.spec.incubation_hours_max
            )
        fault_at = now + timedelta(hours=incubation_hours)

        episode = DegradationEpisode(
            episode_id=episode_id,
            failure_id=self._ctx.ids.failure(),
            mode=mode,
            started_at=now,
            fault_at=fault_at,
            injected=injected,
        )
        machine.add_episode(episode)
        self._initiations += 1

        # Some modes correspond to something the plant would visibly record at
        # the time. Logging those as incidents is legitimate evidence; the mode
        # itself and its root cause stay in the ground-truth store.
        observable = _OBSERVABLE_INCIDENTS.get(mode.id)
        if observable is not None:
            machine.record_incident(observable, now)

        self._ctx.bus.publish(
            "DEGRADATION_STARTED",
            now,
            unit_id=machine.unit_id,
            machine_id=machine.machine_id,
            batch_id=machine.current_batch_id,
            payload={
                "failure_mode": mode.id,
                "expected_fault_at": fault_at.isoformat(),
                "episode_id": episode_id,
                "injected": injected,
            },
        )

        self._record_ground_truth(machine, episode, severity or mode.spec.severity)

        if mode.detectable:
            progress = _warning_progress(mode.spec.warning_threshold)
            warn_at = now + timedelta(hours=incubation_hours * progress)
            if warn_at < fault_at:
                self._ctx.scheduler.at(
                    warn_at,
                    self._make_warning_callback(machine, episode),
                    priority=Priority.FAILURE,
                    label=f"warn:{machine.machine_id}:{mode.id}",
                )
        self._ctx.scheduler.at(
            fault_at,
            self._make_fault_callback(machine, episode, severity or mode.spec.severity),
            priority=Priority.FAILURE,
            label=f"fault:{machine.machine_id}:{mode.id}",
        )
        return episode

    def inject(
        self,
        machine_id: str,
        mode_id: str,
        now: datetime,
        *,
        severity: str | None = None,
        incubation_hours: float | None = None,
    ) -> DegradationEpisode:
        """Inject a failure and let it propagate through the normal machinery.

        Nothing here shortcuts the chain: the injected episode degrades, raises
        precursors, warns, faults, stops production, spoils quality and triggers
        maintenance, deviation, RCA and CAPA exactly as a naturally-arising one
        does (§38).
        """
        machine = self._ctx.plant.machine(machine_id)
        mode = self._ctx.registries.failures.applicable(machine.equipment_class, mode_id)
        if mode is None:
            known = [m.id for m in self._ctx.registries.failures.for_class(machine.equipment_class)]
            raise ValueError(
                f"failure mode {mode_id!r} does not apply to {machine_id} "
                f"(equipment class {machine.equipment_class!r}); applicable modes: {known}"
            )
        if incubation_hours is None:
            # Injected failures are meant to be observed, so they incubate near
            # the fast end of the configured range rather than over weeks.
            incubation_hours = max(
                mode.spec.incubation_hours_min,
                min(mode.spec.incubation_hours_max, mode.spec.incubation_hours_min * 1.5),
            )
        return self.initiate(
            machine,
            mode,
            now,
            injected=True,
            severity=severity,
            incubation_hours=incubation_hours,
        )

    # ------------------------------------------------------------------ callbacks
    def _make_warning_callback(
        self, machine: Machine, episode: DegradationEpisode
    ) -> Callable[[datetime], None]:
        def callback(now: datetime) -> None:
            if not episode.active or episode.faulted_at is not None:
                return
            episode.warned_at = now
            warning_state = self._ctx.registries.states.first_or_none("warning")
            if warning_state is not None:
                machine.transition_to(
                    warning_state, now, f"DEGRADATION:{episode.mode_id}", strict=False
                )
            machine.plc.raise_alarm(
                f"WARN_{episode.mode.spec.category}", episode.mode.spec.description
            )
            self._ctx.bus.publish(
                "MACHINE_WARNING",
                now,
                unit_id=machine.unit_id,
                machine_id=machine.machine_id,
                batch_id=machine.current_batch_id,
                severity="MINOR",
                payload={
                    "failure_mode": episode.mode_id,
                    "degradation": round(episode.degradation(now), 4),
                    "episode_id": episode.episode_id,
                },
            )
            truth = self._ledger.get(episode.episode_id)
            if truth is not None:
                truth.warned_at = now
            if self._on_warning is not None:
                self._on_warning(machine, episode)

        return callback

    def _make_fault_callback(
        self, machine: Machine, episode: DegradationEpisode, severity: str
    ) -> Callable[[datetime], None]:
        def callback(now: datetime) -> None:
            if not episode.active:
                return  # averted by maintenance before the scheduled fault
            episode.faulted_at = now
            machine.failure_count += 1

            state_before = machine.state
            fault_state = self._ctx.registries.states.first("fault")
            machine.transition_to(fault_state, now, f"FAILURE:{episode.mode_id}", strict=False)
            machine.plc.raise_alarm(
                f"FAULT_{episode.mode.spec.category}", episode.mode.spec.description
            )

            record = FailureRecord(
                failure_id=episode.failure_id,
                machine_id=machine.machine_id,
                unit_id=machine.unit_id,
                equipment_class=machine.equipment_class,
                category=episode.mode.spec.category,
                severity=severity,
                symptom=_SYMPTOMS.get(episode.mode.spec.category, "Unplanned stop"),
                detected_at=now,
                alarm_count=machine.plc.alarm_count,
                state_before=state_before,
                batch_id=machine.current_batch_id,
                shift_instance_id=None,
                operator_ids=tuple(machine.assigned_operators),
                run_id=self._ctx.run_id,
            )
            if machine.current_batch_id:
                record.affected_batches.append(machine.current_batch_id)
            self._failures[record.failure_id] = record

            truth = self._ledger.get(episode.episode_id)
            if truth is not None:
                truth.faulted_at = now

            self._ctx.bus.publish(
                "MACHINE_FAILURE",
                now,
                unit_id=machine.unit_id,
                machine_id=machine.machine_id,
                batch_id=machine.current_batch_id,
                severity=severity,
                payload={
                    "failure_id": record.failure_id,
                    "failure_mode": episode.mode_id,
                    "category": record.category,
                    "episode_id": episode.episode_id,
                },
            )
            self._ctx.bus.publish(
                "PRODUCTION_STOPPED",
                now,
                unit_id=machine.unit_id,
                machine_id=machine.machine_id,
                batch_id=machine.current_batch_id,
                severity="MAJOR",
                payload={"reason": f"FAILURE:{record.category}", "failure_id": record.failure_id},
            )
            if self._on_fault is not None:
                self._on_fault(machine, record, episode)

        return callback

    # -------------------------------------------------------------- ground truth
    def _record_ground_truth(
        self, machine: Machine, episode: DegradationEpisode, severity: str
    ) -> None:
        """Register the true story of this episode in the evaluation store.

        Written at onset so nothing about it can be reconstructed from the
        operational data after the fact.
        """
        event = GroundTruthEvent(
            ground_truth_id=self._ctx.ids.ground_truth(),
            episode_id=episode.episode_id,
            failure_id=episode.failure_id,
            machine_id=machine.machine_id,
            unit_id=machine.unit_id,
            equipment_class=machine.equipment_class,
            failure_mode=episode.mode_id,
            failure_category=episode.mode.spec.category,
            root_cause=episode.mode.spec.root_cause,
            root_cause_description=episode.mode.spec.root_cause_description,
            onset_at=episode.started_at,
            scheduled_fault_at=episode.fault_at,
            faulted_at=None,
            warned_at=None,
            averted_at=None,
            resolved_at=None,
            incubation_hours=episode.incubation_hours,
            detectable=episode.mode.detectable,
            injected=episode.injected,
            precursor_tags=tuple(p.tag for p in episode.mode.precursors),
            severity=severity,
            run_id=self._ctx.run_id,
        )
        self._ledger.record(event)
