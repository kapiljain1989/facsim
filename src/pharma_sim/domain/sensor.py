"""Sensor simulation.

A reading is composed, never freshly sampled from a uniform range:

    value = baseline scaled by the machine state
          + AR(1) fluctuation, time-scaled so autocorrelation is cadence-correct
          + bounded random-walk drift
          + diurnal / ambient coupling
          + generic wear response to the machine's health index
          + failure-specific precursor offsets
          + instrument malfunction (stuck / dropout / spike / noise burst)

Correlation between tags is *structural* rather than imposed by a correlation
matrix: health, load and ambient are shared inputs, so vibration, motor current
and temperature rise together because the same underlying condition drives all
three. That is the signal RCA and any downstream model must be able to find, and
an imposed matrix would not survive the state changes and failures that make it
interesting.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from random import Random
from typing import Any

from pharma_sim.config.models import SensorSpec
from pharma_sim.domain.environment import Ambient
from pharma_sim.registry.states import StateRegistry

__all__ = ["Quality", "SensorReading", "PrecursorEffect", "SensorModel"]


class Quality:
    """Reading quality, mirroring the categories real OT systems carry."""

    GOOD = "GOOD"
    UNCERTAIN = "UNCERTAIN"
    BAD = "BAD"


@dataclass(frozen=True, slots=True)
class SensorReading:
    """One telemetry sample. This is the narrow row the time-series store holds."""

    timestamp: datetime
    machine_id: str
    sensor_id: str
    tag: str
    value: float
    unit: str
    quality: str
    unit_id: str
    run_id: str

    def as_message(self, plant_id: str, state: str) -> dict[str, Any]:
        """JSON-friendly shape for streaming sinks."""
        return {
            "kind": "telemetry",
            "timestamp": self.timestamp.isoformat(),
            "plant_id": plant_id,
            "unit_id": self.unit_id,
            "machine_id": self.machine_id,
            "sensor_id": self.sensor_id,
            "tag": self.tag,
            "value": self.value,
            "unit": self.unit,
            "quality": self.quality,
            "state": state,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True)
class PrecursorEffect:
    """A developing failure's contribution to one tag."""

    offset: float
    sigma_gain: float


class SensorModel:
    """Stateful generator for a single tag on a single machine.

    Holds the AR(1) position, accumulated drift and malfunction state, which is
    why each machine needs its own instance rather than a shared function.

    Args:
        spec: the resolved sensor specification for this machine.
        machine_id: owning machine.
        unit_id: owning unit, denormalised onto readings for query locality.
        states: state registry, for resolving state factors by role.
        rng: this tag's private stream, so its history is reproducible
            independently of every other tag.
        nominal_ambient_c: reference used for ambient coupling.
    """

    __slots__ = (
        "spec",
        "sensor_id",
        "machine_id",
        "unit_id",
        "_states",
        "_rng",
        "_ar",
        "_drift",
        "_last_ts",
        "_last_value",
        "_stuck_until",
        "_stuck_value",
        "_tau_minutes",
        "_alarm_latched",
        "_setpoint",
    )

    def __init__(
        self,
        spec: SensorSpec,
        *,
        machine_id: str,
        unit_id: str,
        states: StateRegistry,
        rng: Random,
    ) -> None:
        self.spec = spec
        self.machine_id = machine_id
        self.unit_id = unit_id
        self.sensor_id = f"{machine_id}:{spec.tag}"
        self._states = states
        self._rng = rng
        self._ar = 0.0
        self._drift = 0.0
        self._last_ts: datetime | None = None
        self._last_value = spec.baseline
        self._stuck_until: datetime | None = None
        self._stuck_value = spec.baseline
        self._alarm_latched = False
        # Overridden while a batch is on the machine, so a process parameter
        # tracks the product's setpoint rather than the profile's nominal value.
        self._setpoint: float | None = None
        # Correlation time implied by (rho, rate_s). Expressing the AR term as a
        # decay over elapsed time rather than per sample means the autocorrelation
        # stays physically consistent whether the tag is sampled at 1s or 60s.
        rate_s = spec.rate_s or 60.0
        if spec.rho <= 0.0:
            self._tau_minutes = 0.0
        else:
            self._tau_minutes = (rate_s / 60.0) / -math.log(spec.rho)

    # ------------------------------------------------------------------ helpers
    @property
    def tag(self) -> str:
        return self.spec.tag

    @property
    def is_derived(self) -> bool:
        return self.spec.derived_from is not None

    @property
    def drift(self) -> float:
        return self._drift

    @property
    def effective_baseline(self) -> float:
        """The setpoint in force, falling back to the configured baseline."""
        return self._setpoint if self._setpoint is not None else self.spec.baseline

    def set_setpoint(self, value: float | None) -> None:
        """Point the tag at a product's setpoint, or clear it back to baseline.

        This is what couples telemetry to the recipe: a 15.5 kN product genuinely
        reads ~15.5 kN, so the QC transfer that consumes the tag sees the right
        number instead of the profile's nominal.
        """
        self._setpoint = value

    @property
    def limit_scale(self) -> float:
        """Factor applied to warn/alarm bands under the current setpoint.

        Alarm limits are recipe-relative in a real plant. Without this, a 640 mg
        product would sit permanently in high-weight alarm against a limit set
        for a 550 mg one — an artefact of the profile, not a process problem.
        """
        if self._setpoint is None or self.spec.baseline == 0.0:
            return 1.0
        return self._setpoint / self.spec.baseline

    def reset(self) -> None:
        self._ar = 0.0
        self._drift = 0.0
        self._last_ts = None
        self._last_value = self.spec.baseline
        self._stuck_until = None
        self._alarm_latched = False

    def relieve_degradation(self, fraction: float) -> None:
        """Undo part of the accumulated drift after maintenance.

        Repairing a machine should visibly restore its signals; leaving drift in
        place would make a repaired machine look untouched in the data.
        """
        self._drift *= max(0.0, 1.0 - fraction)

    # ------------------------------------------------------------------ sampling
    def sample(
        self,
        now: datetime,
        *,
        state: str,
        health: float,
        ambient: Ambient,
        precursor: PrecursorEffect | None = None,
        derived_values: dict[str, float] | None = None,
        run_id: str = "",
    ) -> SensorReading | None:
        """Produce one reading, or ``None`` when the instrument drops out.

        Returning ``None`` models a genuine missing sample rather than a
        placeholder value, so downstream code has to cope with gaps the way it
        would with real historian data.
        """
        spec = self.spec
        dt_minutes = self._elapsed_minutes(now)
        self._last_ts = now

        if spec.derived_from is not None:
            value = float((derived_values or {}).get(spec.derived_from, 0.0))
            value = self._clamp(value)
            self._last_value = value
            return self._reading(now, value, Quality.GOOD, run_id)

        factor = self._states.sensor_factor(state, spec.state_factors)
        mult = factor.mult if factor else 1.0
        offset = factor.offset if factor else 0.0
        sigma_mult = factor.sigma_mult if factor else 1.0

        baseline = self.effective_baseline
        mean = baseline * mult + offset

        if spec.diurnal_amplitude:
            hour = now.hour + now.minute / 60.0
            mean += spec.diurnal_amplitude * math.sin(2.0 * math.pi * (hour - 9.0) / 24.0)
        if spec.ambient_coupling:
            mean += spec.ambient_coupling * ambient.delta_for(spec.ambient_source)

        # Generic wear: proportional to baseline so a percentage response reads
        # sensibly whatever the tag's units are.
        if spec.health_sensitivity and health > 0.0:
            mean += spec.health_sensitivity * abs(baseline) * health

        sigma_gain = 0.0
        if precursor is not None:
            mean += precursor.offset
            sigma_gain = precursor.sigma_gain

        self._advance_drift(dt_minutes)
        mean += self._drift

        sigma = abs(spec.sigma) * sigma_mult * (1.0 + sigma_gain)
        quality = Quality.GOOD

        # --- instrument malfunction -----------------------------------------
        malfunction = spec.malfunction
        dt_days = max(dt_minutes, 0.0) / 1440.0

        if self._stuck_until is not None:
            if now < self._stuck_until:
                return self._reading(now, self._clamp(self._stuck_value), Quality.BAD, run_id)
            self._stuck_until = None

        if malfunction.stuck_probability_per_day > 0.0 and dt_days > 0.0:
            if self._rng.random() < malfunction.stuck_probability_per_day * dt_days:
                self._stuck_value = self._last_value
                self._stuck_until = now.fromtimestamp(
                    now.timestamp() + malfunction.stuck_duration_min * 60.0, tz=now.tzinfo
                )
                return self._reading(now, self._clamp(self._stuck_value), Quality.BAD, run_id)

        if malfunction.dropout_probability > 0.0:
            if self._rng.random() < malfunction.dropout_probability:
                return None

        if malfunction.noise_burst_probability > 0.0:
            if self._rng.random() < malfunction.noise_burst_probability:
                sigma *= max(1.0, malfunction.noise_burst_sigma_multiple)
                quality = Quality.UNCERTAIN

        # --- AR(1) fluctuation ----------------------------------------------
        self._advance_ar(dt_minutes)
        value = mean + sigma * self._ar

        if malfunction.spike_probability > 0.0:
            if self._rng.random() < malfunction.spike_probability:
                direction = 1.0 if self._rng.random() < 0.5 else -1.0
                value += direction * malfunction.spike_sigma_multiple * max(
                    sigma, abs(baseline) * 0.01
                )
                quality = Quality.UNCERTAIN

        clamped = self._clamp(value)
        if clamped != value:
            # Rail-hitting is itself a data-quality signal.
            quality = Quality.UNCERTAIN if quality == Quality.GOOD else quality
        self._last_value = clamped
        return self._reading(now, clamped, quality, run_id)

    # -------------------------------------------------------------- components
    def _elapsed_minutes(self, now: datetime) -> float:
        if self._last_ts is None:
            return 0.0
        return max(0.0, (now - self._last_ts).total_seconds() / 60.0)

    def _advance_ar(self, dt_minutes: float) -> None:
        """Advance the AR(1) state as a continuous-time OU step."""
        if self._tau_minutes <= 0.0:
            self._ar = self._rng.gauss(0.0, 1.0)
            return
        if dt_minutes <= 0.0:
            if self._last_ts is None:
                self._ar = self._rng.gauss(0.0, 1.0)
            return
        rho = math.exp(-dt_minutes / self._tau_minutes)
        innovation = math.sqrt(max(0.0, 1.0 - rho * rho))
        self._ar = rho * self._ar + innovation * self._rng.gauss(0.0, 1.0)

    def _advance_drift(self, dt_minutes: float) -> None:
        """Bounded random walk plus a deterministic trend."""
        spec = self.spec
        if spec.drift_limit <= 0.0 or dt_minutes <= 0.0:
            return
        dt_days = dt_minutes / 1440.0
        trend = spec.drift_per_day * dt_days
        wander = 0.0
        if spec.drift_per_day:
            wander = self._rng.gauss(0.0, abs(spec.drift_per_day) * math.sqrt(dt_days))
        self._drift = max(-spec.drift_limit, min(spec.drift_limit, self._drift + trend + wander))

    def _clamp(self, value: float) -> float:
        spec = self.spec
        if spec.hard_min is not None:
            value = max(spec.hard_min, value)
        if spec.hard_max is not None:
            value = min(spec.hard_max, value)
        return value

    def _reading(
        self, now: datetime, value: float, quality: str, run_id: str
    ) -> SensorReading:
        return SensorReading(
            timestamp=now,
            machine_id=self.machine_id,
            sensor_id=self.sensor_id,
            tag=self.spec.tag,
            value=round(value, 4),
            unit=self.spec.unit,
            quality=quality,
            unit_id=self.unit_id,
            run_id=run_id,
        )

    # --------------------------------------------------------------- alarming
    def is_meaningful_in(self, state: str) -> bool:
        """Whether this tag's limits apply while the machine is in ``state``.

        Process alarms are enabled in run mode, as they are on a real line. Two
        cases qualify:

        * the machine is in a productive state, or
        * the tag declares no factor for this state at all, meaning it runs at
          baseline regardless — utilities such as HVAC, which are monitored
          continuously.

        Anything else is a machine that is stopped or ramping, where a reading
        below the operating band is expected rather than alarming. Skipping those
        is the difference between a handful of meaningful alarms and thousands of
        artefacts.
        """
        factor = self._states.sensor_factor(state, self.spec.state_factors)
        if factor is None:
            return True
        if factor.mult == 0.0 and factor.offset == 0.0:
            return False
        return self._states.is_productive(state)

    def evaluate_limits(self, value: float) -> tuple[str | None, float | None]:
        """Classify a value against the tag's warn/alarm bands.

        Bands are scaled by the active setpoint, so limits follow the recipe.

        Returns ``(kind, breached_limit)`` where kind is ``ALARM_HIGH``,
        ``ALARM_LOW``, ``WARN_HIGH``, ``WARN_LOW`` or ``None``.
        """
        spec = self.spec
        scale = self.limit_scale

        def scaled(limit: float | None) -> float | None:
            return None if limit is None else limit * scale

        alarm_high, alarm_low = scaled(spec.alarm_high), scaled(spec.alarm_low)
        warn_high, warn_low = scaled(spec.warn_high), scaled(spec.warn_low)

        if alarm_high is not None and value > alarm_high:
            return "ALARM_HIGH", alarm_high
        if alarm_low is not None and value < alarm_low:
            return "ALARM_LOW", alarm_low
        if warn_high is not None and value > warn_high:
            return "WARN_HIGH", warn_high
        if warn_low is not None and value < warn_low:
            return "WARN_LOW", warn_low
        return None, None

    def latch_alarm(self, active: bool) -> bool:
        """Edge-detect an alarm so one excursion emits one event, not thousands."""
        changed = active != self._alarm_latched
        self._alarm_latched = active
        return changed and active
