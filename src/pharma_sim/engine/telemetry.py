"""Telemetry sampling.

Each machine is sampled on a stagger so timestamps spread across the cadence
rather than landing in one spike, and each tag is sampled only when its own
``rate_s`` says it is due (§12). The same pass:

* writes the reading to the time-series store and to the streaming feed,
* updates the machine's PLC tag,
* feeds the bounded history buffer that RCA later reads back,
* records process-parameter values so QC can consume the *measured* value,
* edge-detects alarms so one excursion emits one event rather than thousands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from pharma_sim.domain.history import SensorHistory
from pharma_sim.domain.machine import Machine
from pharma_sim.domain.sensor import PrecursorEffect, Quality
from pharma_sim.engine.context import SimContext
from pharma_sim.engine.scheduler import Priority

__all__ = ["TelemetrySampler", "TelemetryStats"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TelemetryStats:
    readings: int = 0
    dropouts: int = 0
    bad_quality: int = 0
    uncertain: int = 0
    alarms: int = 0
    anomalies: int = 0
    samples_taken: int = 0


class TelemetrySampler:
    """Samples every machine's sensors on the configured cadence."""

    def __init__(
        self,
        ctx: SimContext,
        *,
        interval_seconds: float,
        stream: Callable[[list[dict[str, Any]]], None] | None = None,
        streaming_enabled: bool = False,
    ) -> None:
        self._ctx = ctx
        self._interval = interval_seconds
        self._stream = stream
        self._streaming_enabled = streaming_enabled
        self._stats = TelemetryStats()
        self._histories: dict[str, SensorHistory] = {}
        self._last_sampled: dict[str, datetime] = {}

        lookback = ctx.config.plant.simulation.rca_lookback_hours
        for machine_id in ctx.plant.machines:
            self._histories[machine_id] = SensorHistory(lookback)

    @property
    def stats(self) -> TelemetryStats:
        return self._stats

    def history(self, machine_id: str) -> SensorHistory | None:
        return self._histories.get(machine_id)

    def set_interval(self, seconds: float) -> None:
        """Change cadence mid-run, as ``--then-live`` does."""
        self._interval = seconds

    def set_streaming(self, enabled: bool) -> None:
        self._streaming_enabled = enabled

    # ------------------------------------------------------------------ scheduling
    def schedule_all(self, start: datetime) -> None:
        """Stagger the first sample of each machine across one cadence."""
        machines = list(self._ctx.plant.machines.values())
        if not machines:
            return
        step = self._interval / len(machines)
        for index, machine in enumerate(machines):
            offset = timedelta(seconds=step * index)
            self._ctx.scheduler.at(
                start + offset,
                self._make_callback(machine),
                priority=Priority.SENSOR,
                label=f"sample:{machine.machine_id}",
            )

    def _make_callback(self, machine: Machine) -> Callable[[datetime], None]:
        def callback(now: datetime) -> None:
            self.sample_machine(machine, now)
            self._ctx.scheduler.at(
                now + timedelta(seconds=self._interval),
                callback,
                priority=Priority.SENSOR,
                label=f"sample:{machine.machine_id}",
            )

        return callback

    # -------------------------------------------------------------------- sampling
    def sample_machine(self, machine: Machine, now: datetime) -> int:
        """Sample every due tag on one machine. Returns readings produced."""
        ambient = self._ctx.environment.at(now)
        health = machine.health_at(now)
        offsets = self._combined_offsets(machine, now)
        # Derived tags mirror live machine state; most machines have none, so the
        # snapshot is only built when something will actually read it.
        derived = machine.derived_values(now) if machine.has_derived_tags else None
        history = self._histories.get(machine.machine_id)
        run_id = self._ctx.run_id
        productive = self._ctx.registries.states.is_productive(machine.state)

        rows: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        self._stats.samples_taken += 1

        for tag, model in machine.sensors.items():
            if not self._is_due(model, now):
                continue
            reading = model.sample(
                now,
                state=machine.state,
                health=health,
                ambient=ambient,
                precursor=offsets.get(tag),
                derived_values=derived,
                run_id=run_id,
            )
            self._last_sampled[f"{machine.machine_id}:{tag}"] = now
            if reading is None:
                self._stats.dropouts += 1
                continue

            self._stats.readings += 1
            if reading.quality == Quality.BAD:
                self._stats.bad_quality += 1
            elif reading.quality == Quality.UNCERTAIN:
                self._stats.uncertain += 1

            machine.plc.write(tag, reading.value, reading.quality, now)
            if history is not None:
                history.record(tag, now, reading.value, reading.quality)
            # Only record a process value while the machine is actually producing.
            # A reading taken while it is idle or stopped is legitimately near
            # zero, and averaging those into the stage mean would understate the
            # achieved value and fail QC for a process that ran correctly.
            if (
                model.spec.process_parameter
                and machine.current_batch_id is not None
                and productive
            ):
                machine.record_process_value(tag, reading.value)

            rows.append(
                {
                    "timestamp": reading.timestamp,
                    "machine_id": reading.machine_id,
                    "sensor_id": reading.sensor_id,
                    "tag": reading.tag,
                    "value": reading.value,
                    "unit": reading.unit,
                    "quality": reading.quality,
                    "unit_id": reading.unit_id,
                    "run_id": reading.run_id,
                }
            )
            if self._streaming_enabled and self._stream is not None:
                messages.append(reading.as_message(self._ctx.plant_id, machine.state))

            self._check_limits(machine, model, reading.value, reading.quality, now)

        machine.plc.scan(now, self._ctx.registries.states.ordinal(machine.state))

        if rows:
            self._ctx.records.write_many("sensor_readings", rows)
        if messages and self._stream is not None:
            self._stream(messages)
        return len(rows)

    def _is_due(self, model, now: datetime) -> bool:
        """A tag samples at its own rate, never faster than the global cadence."""
        rate = model.spec.rate_s or self._interval
        if rate <= self._interval:
            return True
        last = self._last_sampled.get(model.sensor_id)
        if last is None:
            return True
        return (now - last).total_seconds() + 1e-6 >= rate

    def _combined_offsets(
        self, machine: Machine, now: datetime
    ) -> dict[str, PrecursorEffect]:
        """Merge failure precursors with process-parameter shifts.

        Both are additive offsets on a tag's mean, and merging them here is what
        makes the chain end to end: a degrading bearing shifts compression force
        in the telemetry, and QC then reads that shifted force.
        """
        if not machine.episodes:
            return {}
        offsets = dict(machine.precursor_effects(now))
        variability = machine.variability_gain(now)
        for tag, shift in machine.parameter_shifts(now).items():
            existing = offsets.get(tag)
            if existing is None:
                offsets[tag] = PrecursorEffect(shift, variability)
            else:
                offsets[tag] = PrecursorEffect(
                    existing.offset + shift, max(existing.sigma_gain, variability)
                )
        if variability > 0.0:
            for tag, model in machine.sensors.items():
                if tag in offsets or not model.spec.process_parameter:
                    continue
                offsets[tag] = PrecursorEffect(0.0, variability)
        return offsets

    def _check_limits(
        self, machine: Machine, model, value: float, quality: str, now: datetime
    ) -> None:
        """Raise alarm and anomaly events on transitions only."""
        if not model.is_meaningful_in(machine.state):
            # Clear any latched alarm so it does not persist across a stop.
            model.latch_alarm(False)
            return
        kind, limit = model.evaluate_limits(value)
        is_alarm = kind is not None and kind.startswith("ALARM")

        if model.latch_alarm(is_alarm):
            machine.plc.raise_alarm(f"{kind}_{model.tag}", f"{model.tag} {kind}")
            self._stats.alarms += 1
            self._ctx.bus.publish(
                "SENSOR_ALARM",
                now,
                unit_id=machine.unit_id,
                machine_id=machine.machine_id,
                batch_id=machine.current_batch_id,
                severity="MAJOR",
                payload={
                    "sensor_id": model.sensor_id,
                    "tag": model.tag,
                    "value": round(value, 4),
                    "limit": limit,
                    "alarm_code": kind,
                },
            )
        elif not is_alarm:
            machine.plc.clear_alarm(f"ALARM_HIGH_{model.tag}")
            machine.plc.clear_alarm(f"ALARM_LOW_{model.tag}")

        if quality == Quality.BAD:
            self._stats.anomalies += 1
            self._ctx.bus.publish(
                "SENSOR_MALFUNCTION",
                now,
                unit_id=machine.unit_id,
                machine_id=machine.machine_id,
                severity="MINOR",
                payload={"sensor_id": model.sensor_id, "tag": model.tag, "kind": "STUCK"},
            )
        elif quality == Quality.UNCERTAIN and kind is not None:
            self._stats.anomalies += 1
            self._ctx.bus.publish(
                "SENSOR_ANOMALY",
                now,
                unit_id=machine.unit_id,
                machine_id=machine.machine_id,
                batch_id=machine.current_batch_id,
                severity="MINOR",
                payload={
                    "sensor_id": model.sensor_id,
                    "tag": model.tag,
                    "value": round(value, 4),
                    "kind": kind,
                },
            )
