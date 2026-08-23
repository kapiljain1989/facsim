"""The simulator: assembly, lifecycle and the run loop.

This module owns wiring, not behaviour. Each concern lives in its own engine and
this class connects them, subscribes the persistence and streaming taps to the
event bus, and drives the scheduler forward.

Run modes:

* ``run(days=…)`` / ``run(hours=…)`` fast-forwards — the clock never sleeps, so a
  month of plant history costs seconds.
* ``run(live=True)`` paces against wall time and keeps going until stopped,
  pushing the same messages to MQTT and JSONL that the historical path persists.
"""

from __future__ import annotations

import logging
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pharma_sim.config.errors import ConfigError
from pharma_sim.config.linter import lint_config
from pharma_sim.config.loader import config_fingerprint, load_config
from pharma_sim.config.models import FactoryConfig
from pharma_sim.domain.batch import Batch
from pharma_sim.domain.environment import Environment
from pharma_sim.domain.failure_engine import FailureEngine, FailureRecord
from pharma_sim.domain.ground_truth import GroundTruthLedger
from pharma_sim.domain.machine import DegradationEpisode, Machine
from pharma_sim.domain.plant import FactoryBuilder, Plant
from pharma_sim.domain.qc import QcEngine
from pharma_sim.domain.quality_management import (
    CapaManager,
    Deviation,
    DeviationManager,
    RcaEngine,
)
from pharma_sim.domain.maintenance import MaintenanceEngine
from pharma_sim.domain.shift import ShiftScheduler
from pharma_sim.engine.batch_manager import BatchManager
from pharma_sim.engine.duty_manager import DutyManager
from pharma_sim.engine.clock import ClockMode, ClockState, SimulationClock
from pharma_sim.engine.context import SimContext
from pharma_sim.engine.event_bus import ALL_EVENTS, Event, EventBus
from pharma_sim.engine.ids import IdFactory
from pharma_sim.engine.rng import RngRegistry
from pharma_sim.engine.scheduler import Priority, Scheduler
from pharma_sim.engine.shift_manager import ShiftManager
from pharma_sim.engine.telemetry import TelemetrySampler
from pharma_sim.logging_config import configure_logging
from pharma_sim.registry import Registries
from pharma_sim.storage.facade import StorageFacade
from pharma_sim.storage.factory import build_storage
from pharma_sim.streaming.router import SinkRouter, build_sinks

__all__ = ["Simulator", "SimulationSummary", "EMITTED_EVENT_TYPES"]

logger = logging.getLogger(__name__)

#: Every event type the engine can publish. Reconciled against event_types.yaml
#: at startup, so a typo or an undeclared type fails immediately rather than
#: producing events nothing can route.
EMITTED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "SIMULATION_STARTED",
        "SIMULATION_STOPPED",
        "SHIFT_STARTED",
        "SHIFT_ENDED",
        "EMPLOYEE_CLOCK_IN",
        "EMPLOYEE_CLOCK_OUT",
        "EMPLOYEE_ABSENT",
        "BREAK_START",
        "BREAK_END",
        "OVERTIME_STARTED",
        "MACHINE_ASSIGNED",
        "MACHINE_STATE_CHANGED",
        "SETUP_PERFORMED",
        "CLEANING_PERFORMED",
        "MACHINE_WARNING",
        "MACHINE_FAILURE",
        "PRODUCTION_STOPPED",
        "PRODUCTION_RESUMED",
        "SENSOR_ANOMALY",
        "SENSOR_ALARM",
        "SENSOR_MALFUNCTION",
        "DEGRADATION_STARTED",
        "DEGRADATION_AVERTED",
        "PRODUCTION_ORDER_CREATED",
        "BATCH_STARTED",
        "BATCH_STAGE_STARTED",
        "BATCH_STAGE_COMPLETED",
        "BATCH_COMPLETED",
        "BATCH_DISPOSITION",
        "QC_SAMPLE_COLLECTION",
        "QC_TEST_STARTED",
        "QC_TEST_COMPLETED",
        "QC_FAILED",
        "MAINTENANCE_SCHEDULED",
        "MAINTENANCE_STARTED",
        "MAINTENANCE_COMPLETED",
        "MAINTENANCE_DEFERRED",
        "DEVIATION_CREATED",
        "DEVIATION_CLOSED",
        "RCA_STARTED",
        "RCA_COMPLETED",
        "CAPA_CREATED",
        "CAPA_CLOSED",
        "AMBIENT_EXCURSION_STARTED",
        "AMBIENT_EXCURSION_ENDED",
        "MATERIAL_SHORTAGE",
        "SCENARIO_ACTION_APPLIED",
    }
)

#: Event categories routed to the machine_events table.
_MACHINE_CATEGORIES = frozenset({"MACHINE", "SENSOR", "PRODUCTION"})


@dataclass(slots=True)
class SimulationSummary:
    """What a run produced. Reported by ``status`` and by the CLI."""

    run_id: str
    seed: int
    config_fingerprint: str
    sim_start: datetime
    sim_end: datetime
    simulated_hours: float
    wall_seconds: float
    events: int
    telemetry_rows: int
    counts: dict[str, int] = field(default_factory=dict)
    sink_stats: list[dict[str, Any]] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"run            {self.run_id}  (seed {self.seed}, config {self.config_fingerprint[:12]})",
            f"simulated      {self.sim_start:%Y-%m-%d %H:%M} -> {self.sim_end:%Y-%m-%d %H:%M}"
            f"  ({self.simulated_hours:,.1f} h)",
            f"wall clock     {self.wall_seconds:,.1f} s",
            f"events         {self.events:,}",
            f"telemetry      {self.telemetry_rows:,} readings",
        ]
        if self.counts:
            lines.append("records written")
            for name, count in self.counts.items():
                lines.append(f"  {name:<24} {count:>12,}")
        if self.sink_stats:
            lines.append("streaming sinks")
            for stats in self.sink_stats:
                lines.append(
                    f"  {stats['sink']:<12} sent={stats['sent']:,} "
                    f"dropped={stats['dropped']:,} errors={stats['errors']:,} "
                    f"connected={stats['connected']}"
                )
        return "\n".join(lines)


class Simulator:
    """Assembles and runs the factory."""

    def __init__(
        self,
        config_dir: str | Path = "config",
        *,
        seed: int | None = None,
        config: FactoryConfig | None = None,
        sinks: tuple[str, ...] | None = None,
        reset_storage: bool = False,
        storage: StorageFacade | None = None,
        mqtt_client_factory=None,
        configure_logs: bool = True,
        log_level: str = "INFO",
        validate: bool = True,
    ) -> None:
        self._config_dir = Path(config_dir)
        self.config = config if config is not None else load_config(self._config_dir)
        if validate:
            issues = lint_config(self.config)
            if issues:
                raise ConfigError(issues, f"configuration in {self._config_dir} is inconsistent")

        self.fingerprint = config_fingerprint(self.config)
        self.seed = seed if seed is not None else self.config.plant.simulation.seed
        simulation = self.config.plant.simulation

        if configure_logs:
            configure_logging(level=log_level)

        self.registries = Registries.build(self.config)
        self.registries.event_types.verify_emitters(EMITTED_EVENT_TYPES)

        self._rngs = RngRegistry(self.seed)
        self._ids = IdFactory(year=simulation.start_time.year)
        self.run_id = self._ids.run()

        self.clock = SimulationClock(
            simulation.start_time,
            mode=ClockMode.FAST_FORWARD,
            sim_minutes_per_real_second=simulation.speed_sim_minutes_per_real_second,
        )
        self.scheduler = Scheduler()
        self.bus = EventBus(
            self.registries.event_types,
            plant_id=self.config.plant.plant_id,
            run_id=self.run_id,
            next_id=self._ids.event,
        )
        self.environment = Environment(
            self.config.plant.ambient, self._rngs.child("ambient")
        )
        self.plant: Plant = FactoryBuilder(self.config, self.registries, self._rngs).build(
            simulation.start_time
        )

        self.storage = storage if storage is not None else build_storage(
            self.config.storage, reset=reset_storage
        )
        self.storage.initialise()

        self.ctx = SimContext(
            config=self.config,
            registries=self.registries,
            plant=self.plant,
            clock=self.clock,
            scheduler=self.scheduler,
            bus=self.bus,
            rngs=self._rngs,
            ids=self._ids,
            environment=self.environment,
            records=self.storage,
            run_id=self.run_id,
        )

        self.sinks: SinkRouter = build_sinks(
            self.config.sinks,
            selected=sinks,
            mqtt_client_factory=mqtt_client_factory,
        )
        self._streaming = bool(sinks) or any(spec.enabled for spec in self.config.sinks.sinks)

        self._build_engines()
        self._subscribe()

        self._started = False
        self._stopping = False
        self._wall_start = 0.0
        self._wall_elapsed = 0.0
        self._state_rows_written = 0
        self._label_rows = 0
        self._previous_signal_handler: Any = None

    # ------------------------------------------------------------------ assembly
    def _build_engines(self) -> None:
        simulation = self.config.plant.simulation

        self.ledger = GroundTruthLedger(self.run_id, simulation.label_interval_min)
        self.qc = QcEngine(self.registries.qc, self.run_id)
        self.deviations = DeviationManager(self.ctx)
        self.rca = RcaEngine(self.ctx)
        self.capa = CapaManager(self.ctx)

        self.maintenance = MaintenanceEngine(self.ctx, on_complete=self._on_maintenance_done)
        self.failures = FailureEngine(
            self.ctx,
            self.ledger,
            on_fault=self._on_machine_fault,
            on_warning=self._on_machine_warning,
        )
        self.telemetry = TelemetrySampler(
            self.ctx,
            interval_seconds=simulation.sensor_sample_interval_s,
            stream=self._stream_messages,
            streaming_enabled=self._streaming,
        )
        self.shifts = ShiftManager(self.ctx, ShiftScheduler(self.config.shifts, self.plant.plant_id))
        self.batches = BatchManager(
            self.ctx,
            self.qc,
            current_shift=self.shifts.current_instance_id,
            on_setup_error=self._on_setup_error,
            on_batch_complete=self._on_batch_complete,
        )
        self.duty = DutyManager(self.ctx)

    def _subscribe(self) -> None:
        """Tap the event bus for persistence, streaming and deviation triggers."""
        self.bus.subscribe(ALL_EVENTS, self._persist_event)
        if self._streaming:
            self.bus.subscribe(ALL_EVENTS, self._stream_event)
        for rule in self.config.deviations.rules:
            self.bus.subscribe(rule.trigger_event, self._maybe_open_deviation)

    # ---------------------------------------------------------------- event taps
    def _persist_event(self, event: Event) -> None:
        self.storage.write("events", event.as_row())
        if event.category == "EMPLOYEE" and event.employee_id:
            self.storage.write(
                "employee_events",
                {
                    "event_id": event.event_id,
                    "timestamp": event.timestamp,
                    "employee_id": event.employee_id,
                    "event_type": event.event_type,
                    "shift_instance_id": event.shift_instance_id,
                    "unit_id": event.unit_id,
                    "machine_id": event.machine_id,
                    "payload": event.payload,
                    "run_id": self.run_id,
                },
            )
        elif event.category in _MACHINE_CATEGORIES and event.machine_id:
            self.storage.write(
                "machine_events",
                {
                    "event_id": event.event_id,
                    "timestamp": event.timestamp,
                    "machine_id": event.machine_id,
                    "unit_id": event.unit_id,
                    "event_type": event.event_type,
                    "severity": event.severity,
                    "batch_id": event.batch_id,
                    "payload": event.payload,
                    "run_id": self.run_id,
                },
            )

    def _stream_event(self, event: Event) -> None:
        if event.event_type in self.registries.event_types.streamed():
            self.sinks.publish(event.as_message())

    def _stream_messages(self, messages: list[dict[str, Any]]) -> None:
        if self._streaming:
            self.sinks.publish_many(messages)

    # -------------------------------------------------------------- engine hooks
    def _on_setup_error(self, machine: Machine, now: datetime) -> None:
        """A setup error sometimes escalates into a real failure mode.

        Most mis-sets are caught by the in-process check before they matter; only
        a minority survive to disturb the process. Escalating every one would
        flood the dataset with human-cause failures and crowd out the wear-out
        modes that carry observable precursors.
        """
        mode = self.registries.failures.applicable(machine.equipment_class, "WRONG_SETUP")
        if mode is None or machine.has_active_mode("WRONG_SETUP"):
            return
        rng = self._rngs.child("setup-escalation", machine.machine_id)
        if rng.random() >= 0.25:
            return
        self.failures.initiate(machine, mode, now, incubation_hours=0.5)

    def _on_machine_warning(self, machine: Machine, episode: DegradationEpisode) -> None:
        self.maintenance.consider_predictive(machine, episode)

    def _on_machine_fault(
        self, machine: Machine, record: FailureRecord, episode: DegradationEpisode
    ) -> None:
        record.shift_instance_id = self.shifts.current_instance_id()
        self.storage.write("failures", record.as_row())
        self.maintenance.schedule_corrective(
            machine, record.failure_id, episode.mode.spec.repair
        )

    def _on_maintenance_done(
        self, machine: Machine, record, resolved: list[DegradationEpisode]
    ) -> None:
        now = self.clock.now
        for episode in resolved:
            truth = self.ledger.get(episode.episode_id)
            if truth is not None:
                truth.resolved_at = now
                truth.averted_at = episode.averted_at
                if episode.faulted_at is not None:
                    truth.downtime_minutes = (
                        now - episode.faulted_at
                    ).total_seconds() / 60.0
            failure = self.failures.failure(episode.failure_id)
            if failure is not None:
                failure.resolved_at = now
                failure.maintenance_id = record.maintenance_id
                if failure.detected_at:
                    failure.downtime_minutes = (
                        now - failure.detected_at
                    ).total_seconds() / 60.0
                self.storage.write("failures", failure.as_row())
            # Labels are emitted when the episode closes, so every row can state
            # the real outcome — faulted or averted.
            self._emit_labels(episode.episode_id)

        if machine.current_batch_id is None:
            self.bus.publish(
                "PRODUCTION_RESUMED",
                now,
                unit_id=machine.unit_id,
                machine_id=machine.machine_id,
                payload={"reason": f"MAINTENANCE_COMPLETE:{record.maintenance_id}"},
            )

    def _on_batch_complete(self, batch: Batch) -> None:
        now = self.clock.now
        for machine_id in batch.machines_used:
            self.capa.register_verification_batch(machine_id, batch, now)
        # Attribute the batch to any ground-truth episode whose fault touched it.
        for failure_id in batch.failure_ids:
            truth = self.ledger.by_failure(failure_id)
            if truth is None:
                continue
            if batch.batch_id not in truth.affected_batches:
                truth.affected_batches.append(batch.batch_id)
            for result in batch.failed_qc:
                truth.affected_qc_failures.append(result.test_id)
            truth.production_loss_units += batch.reject_quantity

    def _maybe_open_deviation(self, event: Event) -> None:
        """Open a deviation and, when required, schedule the investigation."""
        failure_id = event.payload.get("failure_id")
        description = self._describe(event)
        deviation = self.deviations.open(
            event_type=event.event_type,
            event_id=event.event_id,
            now=event.timestamp,
            unit_id=event.unit_id,
            machine_id=event.machine_id,
            batch_id=event.batch_id,
            failure_id=failure_id,
            description=description,
        )
        if deviation is None:
            return
        self.storage.write("deviations", deviation.as_row())

        if event.batch_id and event.batch_id in self.batches.active:
            self.batches.active[event.batch_id].link_deviation(deviation.deviation_id)

        if not deviation.requires_rca:
            self.deviations.close(deviation, event.timestamp)
            return

        delay = self.config.plant.simulation.rca_investigation_delay_hours
        self.deviations.advance(deviation, "INVESTIGATION")
        self.scheduler.at(
            event.timestamp + timedelta(hours=delay),
            lambda now: self._investigate(deviation, now),
            priority=Priority.ANALYSIS,
            label=f"rca:{deviation.deviation_id}",
        )

    @staticmethod
    def _describe(event: Event) -> str:
        if event.event_type == "QC_FAILED":
            return (
                f"{event.payload.get('parameter')} measured "
                f"{event.payload.get('actual_value')} against target "
                f"{event.payload.get('target')} "
                f"[{event.payload.get('lower_limit')}, {event.payload.get('upper_limit')}]"
            )
        if event.event_type == "MACHINE_FAILURE":
            return f"{event.payload.get('category')} fault on {event.machine_id}"
        if event.event_type == "SENSOR_ALARM":
            return (
                f"{event.payload.get('tag')} at {event.payload.get('value')} breached "
                f"{event.payload.get('alarm_code')} limit {event.payload.get('limit')}"
            )
        return event.event_type

    # ---------------------------------------------------------------------- RCA
    def _investigate(self, deviation: Deviation, now: datetime) -> None:
        machine = (
            self.plant.machines.get(deviation.machine_id) if deviation.machine_id else None
        )
        history = (
            self.telemetry.history(deviation.machine_id) if deviation.machine_id else None
        )
        category = None
        if deviation.failure_id:
            failure = self.failures.failure(deviation.failure_id)
            category = failure.category if failure else None

        report = self.rca.investigate(
            deviation,
            now=now,
            history=history,
            category=category,
            signals=self._rca_signals(machine, deviation, now),
        )
        self.storage.write("deviations", deviation.as_row())

        if deviation.requires_capa:
            capa = self.capa.open(deviation, report, now)
            self.deviations.advance(deviation, "CAPA_PENDING")
            self.storage.write("deviations", deviation.as_row())
            del capa
        else:
            self.deviations.close(deviation, now)

    def _rca_signals(
        self, machine: Machine | None, deviation: Deviation, now: datetime
    ) -> dict[str, float]:
        """Non-sensor evidence, drawn only from what the dataset records."""
        lookback = self.config.plant.simulation.rca_lookback_hours
        signals: dict[str, float] = {}

        if machine is not None:
            signals["pm_overdue_hours"] = machine.pm_overdue_hours(now)
            signals["hours_since_last_maintenance"] = machine.hours_since_maintenance(now)
            signals["corrective_repairs_90d"] = float(machine.corrective_repairs_since(now))
            signals["operator_inexperience"] = machine.operator_inexperience
            signals["alarm_count"] = float(machine.plc.alarm_count)
            warning_states = self.registries.states.members("warning")
            signals["warning_duration_hours"] = sum(
                interval.seconds
                for interval in machine.state_history
                if interval.state in warning_states
                and interval.exited_at >= now - timedelta(hours=lookback)
            ) / 3600.0
            history = self.telemetry.history(machine.machine_id)
            if history is not None:
                signals["sensor_quality_bad_fraction"] = history.bad_fraction(now, lookback)
            window = machine.lifetime_window
            if window.actual_quantity > 0:
                # An *increase* over this machine's own nominal reject rate, not
                # the absolute level: every machine rejects a little by design.
                observed = window.reject_quantity / window.actual_quantity
                signals["reject_rate_increase"] = max(
                    0.0, observed - machine.spec.base_reject_rate
                )
            # Read from recorded incidents rather than live episode state: by the
            # time an investigation runs the machine has usually been repaired,
            # and a signal derived from "is a mode active now" would read zero
            # for every failure it was supposed to explain.
            since = deviation.detected_at - timedelta(hours=lookback)
            signals["setup_error_count"] = float(
                machine.incident_count("SETUP_ERROR", since)
            )
            signals["missed_inspection_count"] = float(
                machine.incident_count("MISSED_INSPECTION", since)
            )
            signals["material_wait_hours"] = 3.0 * machine.incident_count(
                "MATERIAL_WAIT", since
            )
            signals["parameter_deviation_count"] = float(
                machine.incident_count("PARAMETER_DEVIATION", since)
            )

        if self.environment.excursion_active:
            signals["ambient_excursion_hours"] = 2.0

        batch = self.batches.active.get(deviation.batch_id or "")
        if batch is None:
            batch = next(
                (b for b in self.batches.completed if b.batch_id == deviation.batch_id), None
            )
        if batch is not None:
            signals["qc_failure_count"] = float(len(batch.failed_qc))
            signals["parameter_deviation_count"] = max(
                signals.get("parameter_deviation_count", 0.0),
                float(sum(len(stage.deviating_parameters) for stage in batch.stages)),
            )
            if batch.planned_quantity:
                signals["batch_reject_rate"] = batch.reject_quantity / batch.planned_quantity
        return signals

    # -------------------------------------------------------------------- labels
    def _emit_labels(self, episode_id: str) -> None:
        truth = self.ledger.get(episode_id)
        if truth is None:
            return
        labels = self.ledger.labels_for_episode(truth, until=self.clock.now)
        if labels:
            self.storage.write_many(
                "prediction_labels", [label.as_row() for label in labels]
            )
            self._label_rows += len(labels)

    def _emit_healthy_labels(self, now: datetime) -> None:
        """Negative examples for machines with nothing developing."""
        rows = []
        for machine in self.plant.machines.values():
            if machine.active_mode_count() > 0:
                continue
            rows.append(
                self.ledger.healthy_label(
                    machine_id=machine.machine_id,
                    unit_id=machine.unit_id,
                    equipment_class=machine.equipment_class,
                    timestamp=now,
                ).as_row()
            )
        if rows:
            self.storage.write_many("prediction_labels", rows)
            self._label_rows += len(rows)

    # ------------------------------------------------------------- periodic work
    def _schedule_periodic(self, start: datetime) -> None:
        simulation = self.config.plant.simulation

        self._repeat(
            start + timedelta(minutes=simulation.hazard_evaluation_interval_min),
            timedelta(minutes=simulation.hazard_evaluation_interval_min),
            self._hazard_tick,
            Priority.FAILURE,
            "hazard",
        )
        self._repeat(
            start + timedelta(minutes=simulation.production_tick_min),
            timedelta(minutes=simulation.production_tick_min),
            self._production_tick,
            Priority.PRODUCTION,
            "production",
        )
        self._repeat(
            start + timedelta(minutes=simulation.label_interval_min),
            timedelta(minutes=simulation.label_interval_min),
            self._emit_healthy_labels,
            Priority.LABEL,
            "labels",
        )
        self._repeat(
            start + timedelta(hours=1),
            timedelta(hours=1),
            self._housekeeping_tick,
            Priority.PERSIST,
            "housekeeping",
        )

    def _repeat(
        self,
        first: datetime,
        interval: timedelta,
        action,
        priority: int,
        label: str,
    ) -> None:
        def callback(now: datetime) -> None:
            action(now)
            if not self._stopping:
                self.scheduler.at(
                    now + interval, callback, priority=priority, label=label
                )

        self.scheduler.at(first, callback, priority=priority, label=label)

    def _hazard_tick(self, now: datetime) -> None:
        interval = self.config.plant.simulation.hazard_evaluation_interval_min / 60.0
        self.failures.evaluate_all(now, interval)
        self.maintenance.scan_preventive(now)

        started = self.environment.maybe_start_excursion(now, interval)
        if started is not None:
            kind, delta = started
            self.bus.publish(
                "AMBIENT_EXCURSION_STARTED",
                now,
                severity="MINOR",
                payload={"kind": kind, "delta": round(delta, 2)},
            )
        ended = self.environment.excursion_ended(now)
        if ended is not None:
            self.bus.publish(
                "AMBIENT_EXCURSION_ENDED", now, payload={"kind": ended}
            )

    def _production_tick(self, now: datetime) -> None:
        for machine in self.plant.machines.values():
            machine.accrue_time(now)
        # Duty runs before replenishment so coupled equipment reacts to the line
        # state this tick observed, not the one the new batches will create.
        self.duty.tick(now)
        self.batches.replenish(now)

    def _housekeeping_tick(self, now: datetime) -> None:
        """Persist completed state intervals and keep buffers moving."""
        self._flush_state_history()
        self.storage.flush()
        if self._streaming:
            self.sinks.dispatch()

    def _flush_state_history(self) -> None:
        rows = []
        for machine in self.plant.machines.values():
            if not machine.state_history:
                continue
            for interval in machine.state_history:
                rows.append(
                    {
                        "machine_id": interval.machine_id,
                        "sequence": interval.sequence,
                        "entered_at": interval.entered_at,
                        "state": interval.state,
                        "exited_at": interval.exited_at,
                        "seconds": round(interval.seconds, 2),
                        "reason": interval.reason,
                        "batch_id": interval.batch_id,
                        "run_id": self.run_id,
                    }
                )
            machine.state_history.clear()
        if rows:
            self.storage.write_many("machine_state_history", rows)
            self._state_rows_written += len(rows)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        """Persist the topology and schedule the opening work."""
        if self._started:
            return
        start = self.clock.start_time
        self._persist_topology()

        self.bus.publish(
            "SIMULATION_STARTED",
            start,
            payload={
                "run_id": self.run_id,
                "seed": self.seed,
                "config_fingerprint": self.fingerprint,
            },
        )

        if self._streaming:
            self.sinks.start()

        self.shifts.bootstrap(start)
        self.telemetry.schedule_all(start)
        self.batches.replenish(start)
        self._schedule_periodic(start)

        self.clock.start()
        self._started = True
        self._wall_start = time.monotonic()
        logger.info(
            "simulation %s started at %s (seed=%s, fingerprint=%s)",
            self.run_id,
            start.isoformat(),
            self.seed,
            self.fingerprint[:12],
        )

    def pause(self) -> None:
        self.clock.pause()

    def resume(self) -> None:
        self.clock.resume()

    def stop(self) -> None:
        self._stopping = True
        self.clock.stop()

    def reset(self) -> None:
        """Return the clock and scheduler to the start of the run."""
        self.scheduler.drain()
        self.clock.reset()
        self._rngs.reset()
        self._started = False
        self._stopping = False

    # ------------------------------------------------------------------- running
    def run(
        self,
        *,
        days: float = 0.0,
        hours: float = 0.0,
        minutes: float = 0.0,
        until: datetime | None = None,
        live: bool = False,
        speed: float | None = None,
        then_live: bool = False,
        max_wall_seconds: float | None = None,
    ) -> SimulationSummary:
        """Advance the simulation.

        Args:
            days/hours/minutes/until: how far to fast-forward.
            live: run indefinitely, paced against wall time.
            speed: simulated minutes per real second while paced.
            then_live: fast-forward the requested span, then keep streaming live.
            max_wall_seconds: safety stop for live mode, used by the tests.
        """
        if not self._started:
            self.start()

        horizon: datetime | None = None
        if until is not None:
            horizon = until
        elif days or hours or minutes:
            horizon = self.clock.start_time + timedelta(
                days=days, hours=hours, minutes=minutes
            )

        if horizon is not None:
            self.shifts.set_horizon(horizon)
            self.batches.set_horizon(horizon)
            self._run_fast_forward(horizon)

        if live or then_live:
            self._run_live(speed=speed, max_wall_seconds=max_wall_seconds)

        return self.finish()

    def _run_fast_forward(self, horizon: datetime) -> None:
        self.clock.set_mode(ClockMode.FAST_FORWARD)
        executed = self.scheduler.run_until(horizon, self.clock.advance_to)
        # Land exactly on the horizon so the final accounting covers the whole span.
        self.clock.advance_to(horizon)
        for machine in self.plant.machines.values():
            machine.accrue_time(horizon)
        logger.info(
            "fast-forward complete: %s tasks executed to %s", f"{executed:,}", horizon.isoformat()
        )

    def _run_live(self, *, speed: float | None, max_wall_seconds: float | None) -> None:
        """Stream indefinitely at wall-clock pace until interrupted."""
        simulation = self.config.plant.simulation
        self.clock.set_mode(
            ClockMode.PACED,
            speed if speed is not None else simulation.speed_sim_minutes_per_real_second,
        )
        self.telemetry.set_interval(simulation.live_sensor_sample_interval_s)
        if not self._streaming:
            logger.warning(
                "live mode with no sink enabled: data will persist but nothing will stream. "
                "Pass --sink jsonl or --sink mqtt."
            )
        self.shifts.set_horizon(None)
        self.batches.set_horizon(None)
        self._install_signal_handler()
        started = time.monotonic()

        logger.info(
            "live feed started at %.0fx (sinks: %s). Press Ctrl-C to stop.",
            self.clock._ratio if hasattr(self.clock, "_ratio") else 0.0,
            ", ".join(self.sinks.sink_names) or "none",
        )
        try:
            while not self._stopping:
                next_due = self.scheduler.peek_time()
                if next_due is None:
                    break
                self.scheduler.run_until(next_due, self.clock.advance_to)
                if self._streaming:
                    self.sinks.dispatch()
                if max_wall_seconds is not None and time.monotonic() - started >= max_wall_seconds:
                    break
        except KeyboardInterrupt:  # pragma: no cover - interactive path
            logger.info("interrupt received; shutting down cleanly")
        finally:
            self._restore_signal_handler()

    def _install_signal_handler(self) -> None:
        def handler(signum, frame):  # pragma: no cover - interactive path
            del signum, frame
            logger.info("SIGINT received; finishing the current step and flushing sinks")
            self._stopping = True

        try:
            self._previous_signal_handler = signal.signal(signal.SIGINT, handler)
        except ValueError:  # pragma: no cover - not on the main thread
            self._previous_signal_handler = None

    def _restore_signal_handler(self) -> None:
        if self._previous_signal_handler is not None:
            try:
                signal.signal(signal.SIGINT, self._previous_signal_handler)
            except ValueError:  # pragma: no cover
                pass
            self._previous_signal_handler = None

    # ------------------------------------------------------------------ shutdown
    def finish(self) -> SimulationSummary:
        """Close out the run: flush everything and write the final records."""
        now = self.clock.now
        self._stopping = True

        for machine in self.plant.machines.values():
            machine.accrue_time(now)
        self._flush_state_history()

        # Episodes still developing at the end still deserve honest labels.
        for truth in self.ledger.all():
            if truth.resolved_at is None and truth.averted_at is None:
                self._emit_labels(truth.episode_id)

        for truth in self.ledger.all():
            self.storage.write("ground_truth_events", truth.as_row())
        for record in self.maintenance.records.values():
            self.storage.write("maintenance", record.as_row())
        for failure in self.failures.failures.values():
            self.storage.write("failures", failure.as_row())
        for deviation in self.deviations.deviations.values():
            self.storage.write("deviations", deviation.as_row())
        self.capa.finalise()

        self.bus.publish(
            "SIMULATION_STOPPED",
            now,
            payload={
                "run_id": self.run_id,
                "simulated_hours": round(self.clock.elapsed_hours, 3),
            },
        )

        self._wall_elapsed = time.monotonic() - self._wall_start if self._wall_start else 0.0
        self.storage.write(
            "runs",
            {
                "run_id": self.run_id,
                "config_fingerprint": self.fingerprint,
                "seed": self.seed,
                "mode": self.clock.mode.value,
                "started_at": self.clock.start_time,
                "ended_at": now,
                "sim_start": self.clock.start_time,
                "sim_end": now,
                "simulated_hours": round(self.clock.elapsed_hours, 4),
                "event_count": self.bus.total,
                "telemetry_count": self.telemetry.stats.readings,
                "notes": f"config={self._config_dir}",
            },
        )
        self.storage.flush()

        if self._streaming:
            self.sinks.stop()

        self.clock.stop()
        return self.summary()

    def close(self) -> None:
        self.storage.close()

    # -------------------------------------------------------------------- output
    def summary(self) -> SimulationSummary:
        return SimulationSummary(
            run_id=self.run_id,
            seed=self.seed,
            config_fingerprint=self.fingerprint,
            sim_start=self.clock.start_time,
            sim_end=self.clock.now,
            simulated_hours=self.clock.elapsed_hours,
            wall_seconds=self._wall_elapsed,
            events=self.bus.total,
            telemetry_rows=self.telemetry.stats.readings,
            counts=self.storage.written(),
            sink_stats=[stats.as_row() for stats in self.sinks.stats()],
        )

    def status(self) -> dict[str, Any]:
        """A live snapshot, used by ``pharma_sim status`` and the tests."""
        states = self.registries.states
        by_state: dict[str, int] = {}
        for machine in self.plant.machines.values():
            by_state[machine.state] = by_state.get(machine.state, 0) + 1

        return {
            "run_id": self.run_id,
            "seed": self.seed,
            "config_fingerprint": self.fingerprint,
            "clock": {
                "now": self.clock.now.isoformat(),
                "state": self.clock.state.value,
                "mode": self.clock.mode.value,
                "simulated_hours": round(self.clock.elapsed_hours, 3),
            },
            "topology": {
                "units": len(self.plant.units),
                "machines": self.plant.machine_count,
                "employees": len(self.plant.employees),
                "workers": self.plant.worker_count,
                "sensors": sum(len(m.sensors) for m in self.plant.machines.values()),
                "shifts": len(self.config.shifts.shifts),
                "states": len(states),
                "products": len(self.config.products.products),
            },
            "machines_by_state": dict(sorted(by_state.items())),
            "machines_warning": sum(
                1 for m in self.plant.machines.values() if states.is_warning(m.state)
            ),
            "machines_fault": sum(
                1 for m in self.plant.machines.values() if states.is_fault(m.state)
            ),
            "degrading": sum(
                1 for m in self.plant.machines.values() if m.active_mode_count() > 0
            ),
            "events": self.bus.total,
            "telemetry": {
                "readings": self.telemetry.stats.readings,
                "dropouts": self.telemetry.stats.dropouts,
                "bad_quality": self.telemetry.stats.bad_quality,
                "alarms": self.telemetry.stats.alarms,
            },
            "batches": {
                "active": len(self.batches.active),
                "completed": self.batches.stats.batches_completed,
                "released": self.batches.stats.released,
                "rejected": self.batches.stats.rejected,
                "quarantined": self.batches.stats.quarantined,
                "qc_tests": self.batches.stats.qc_tests,
                "qc_failures": self.batches.stats.qc_failures,
            },
            "reliability": {
                "episodes_started": self.failures.initiation_count,
                "faults": len(self.failures.failures),
                "averted": self.maintenance.averted_count,
                "pm_deferred": self.maintenance.deferred_count,
                "maintenance_actions": len(self.maintenance.records),
            },
            "quality_management": {
                "deviations": len(self.deviations.deviations),
                "rca_reports": len(self.rca.reports),
                "capas": len(self.capa.capas),
                "capas_closed": sum(
                    1 for capa in self.capa.capas.values() if capa.status == "CLOSED"
                ),
            },
            "shifts": {
                "started": self.shifts.stats.shifts_started,
                "ended": self.shifts.stats.shifts_ended,
                "absences": self.shifts.stats.absences,
                "overtime": self.shifts.stats.overtime_events,
            },
            "labels": self._label_rows,
            "storage": self.storage.describe(),
            "sinks": [stats.as_row() for stats in self.sinks.stats()],
            "scheduler": self.scheduler.stats(),
        }

    # ---------------------------------------------------------------- topology IO
    def _persist_topology(self) -> None:
        """Write the dimensions, including the vocabulary itself."""
        from pharma_sim.config.loader import canonical_payload

        self.storage.write(
            "config_versions",
            {
                "fingerprint": self.fingerprint,
                "created_at": self.clock.start_time,
                "config_dir": str(self._config_dir),
                "change_count": len(canonical_payload(self.config)),
                "change_summary": "initial load",
            },
        )
        self.storage.write(
            "runs",
            {
                "run_id": self.run_id,
                "config_fingerprint": self.fingerprint,
                "seed": self.seed,
                "mode": self.clock.mode.value,
                "started_at": self.clock.start_time,
                "sim_start": self.clock.start_time,
            },
        )

        states = self.registries.states
        for spec in self.config.states.states:
            self.storage.write(
                "states",
                {
                    "state_id": spec.id,
                    "description": spec.description,
                    "production_rate_factor": spec.production_rate_factor,
                    "reject_rate_add": spec.reject_rate_add,
                    "energy_factor": spec.energy_factor,
                    "roles": ",".join(states.roles_of(spec.id)),
                    "allowed_transitions": ",".join(sorted(states.allowed_from(spec.id))),
                },
            )
        for spec in self.config.event_types.event_types:
            self.storage.write(
                "event_types",
                {
                    "event_type": spec.id,
                    "category": spec.category,
                    "default_severity": spec.default_severity,
                    "description": spec.description,
                    "required_fields": ",".join(spec.required_fields),
                    "streamed": spec.stream,
                },
            )
        for class_id in self.registries.equipment.ids:
            resolved = self.registries.equipment.get(class_id)
            self.storage.write(
                "equipment_classes",
                {
                    "equipment_class": class_id,
                    "name": resolved.spec.name,
                    "sensor_profile": resolved.spec.sensor_profile,
                    "nominal_rate_per_hour": resolved.spec.nominal_rate_per_hour,
                    "pm_interval_hours": resolved.spec.pm_interval_hours,
                    "base_reject_rate": resolved.spec.base_reject_rate,
                    "sensor_count": len(resolved.sensors),
                },
            )

        self.storage.write("plants", self.plant.as_row())
        for unit in self.plant.units.values():
            self.storage.write("units", unit.as_row())
        for employee in self.plant.employees.values():
            self.storage.write("employees", employee.as_row())
        for product in self.config.products.products:
            self.storage.write(
                "products",
                {
                    "product_id": product.product_id,
                    "product_name": product.product_name,
                    "dosage_form": product.dosage_form,
                    "batch_size": product.batch_size,
                    "target_quantity": product.target_quantity,
                    "manufacturing_process": ",".join(product.manufacturing_process),
                    "raw_materials": ",".join(
                        material.material_id for material in product.raw_materials
                    ),
                    "qc_specifications": ",".join(product.qc_specifications),
                    "demand_weight": product.demand_weight,
                },
            )
        for spec in self.config.shifts.shifts:
            self.storage.write(
                "shifts",
                {
                    "shift_code": spec.code,
                    "name": spec.name,
                    "start_time": spec.start.isoformat(),
                    "end_time": spec.end.isoformat(),
                    "crosses_midnight": spec.crosses_midnight,
                    "breaks": ",".join(
                        f"{b.label}@{b.start.isoformat()}+{b.duration_min:g}m"
                        for b in spec.breaks
                    ),
                },
            )
        for machine in self.plant.machines.values():
            self.storage.write("machines", machine.as_row())
            self.storage.write_many("sensors", machine.sensor_rows())
            self.storage.write_many("plc_tags", machine.plc.tag_rows())
        self.storage.flush()

    # -------------------------------------------------------------- interventions
    def inject_failure(
        self, machine_id: str, failure_mode: str, *, severity: str | None = None
    ) -> str:
        """Inject a failure that then propagates through the normal machinery."""
        episode = self.failures.inject(
            machine_id, failure_mode, self.clock.now, severity=severity
        )
        logger.info(
            "injected %s on %s: fault scheduled for %s",
            failure_mode,
            machine_id,
            episode.fault_at.isoformat(),
        )
        return episode.episode_id
