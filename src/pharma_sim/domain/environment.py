"""Plant environment: the shared latent driver behind correlated readings.

Ambient temperature and humidity are computed once per sample instant and fed to
every sensor. That is deliberate: it means readings on unrelated machines move
together over the day the way real plant data does, rather than each drifting
independently. Excursions are the same mechanism turned up, and they raise the
environment factor in the failure hazard model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from random import Random

from pharma_sim.config.models import AmbientConfig

__all__ = ["Ambient", "Environment"]


@dataclass(frozen=True, slots=True)
class Ambient:
    """Plant conditions at one instant."""

    temperature_c: float
    humidity_pct: float
    temperature_delta: float
    humidity_delta: float
    excursion_kind: str | None = None

    @property
    def stress(self) -> float:
        """Environmental stress in ``[0, 1]``, feeding the hazard model (§14)."""
        thermal = abs(self.temperature_delta) / 8.0
        moisture = abs(self.humidity_delta) / 20.0
        return min(1.0, thermal + moisture)

    def delta_for(self, source: str) -> float:
        return self.humidity_delta if source == "humidity" else self.temperature_delta


class Environment:
    """Generates ambient conditions, including occasional excursions."""

    __slots__ = (
        "_config",
        "_rng",
        "_excursion_until",
        "_excursion_kind",
        "_excursion_delta",
        "_last_evaluated",
        "_forced_until",
    )

    def __init__(self, config: AmbientConfig, rng: Random) -> None:
        self._config = config
        self._rng = rng
        self._excursion_until: datetime | None = None
        self._excursion_kind: str | None = None
        self._excursion_delta = 0.0
        self._last_evaluated: datetime | None = None
        self._forced_until: datetime | None = None

    # ------------------------------------------------------------- excursions
    def maybe_start_excursion(self, now: datetime, hours: float) -> tuple[str, float] | None:
        """Possibly begin an excursion; returns ``(kind, delta)`` when one starts.

        Called on the hazard cadence rather than per sample, so excursion
        frequency is independent of how often sensors happen to be read.
        """
        if self._excursion_until is not None and now < self._excursion_until:
            return None
        if self._excursion_until is not None and now >= self._excursion_until:
            self._excursion_until = None
            self._excursion_kind = None
            self._excursion_delta = 0.0

        daily = self._config.excursion_probability_per_day
        probability = min(1.0, daily * (hours / 24.0))
        if self._rng.random() >= probability:
            return None

        kind = "TEMPERATURE" if self._rng.random() < 0.6 else "HUMIDITY"
        magnitude = (
            self._config.excursion_temperature_delta_c
            if kind == "TEMPERATURE"
            else self._config.humidity_diurnal_amplitude_pct * 3.0
        )
        delta = magnitude * self._rng.uniform(0.7, 1.4)
        duration = self._config.excursion_duration_hours * self._rng.uniform(0.6, 1.8)
        self._excursion_kind = kind
        self._excursion_delta = delta
        self._excursion_until = now.fromtimestamp(
            now.timestamp() + duration * 3600.0, tz=now.tzinfo
        )
        return kind, delta

    def force_excursion(self, now: datetime, kind: str, delta: float, hours: float) -> None:
        """Start an excursion explicitly, used by the scenario engine."""
        self._excursion_kind = kind
        self._excursion_delta = delta
        self._excursion_until = now.fromtimestamp(
            now.timestamp() + hours * 3600.0, tz=now.tzinfo
        )

    def excursion_ended(self, now: datetime) -> str | None:
        """Clear a finished excursion, returning its kind if one just ended."""
        if self._excursion_until is not None and now >= self._excursion_until:
            kind = self._excursion_kind
            self._excursion_until = None
            self._excursion_kind = None
            self._excursion_delta = 0.0
            return kind
        return None

    @property
    def excursion_active(self) -> bool:
        return self._excursion_until is not None

    # ---------------------------------------------------------------- sampling
    def at(self, now: datetime) -> Ambient:
        """Ambient conditions at ``now``.

        Deterministic in the instant: the diurnal cycle plus any active
        excursion. No RNG is drawn here, so sampling more often does not change
        the environment's trajectory.
        """
        config = self._config
        hour = now.hour + now.minute / 60.0 + now.second / 3600.0
        # Peaks mid-afternoon, troughs before dawn.
        phase = math.sin(2.0 * math.pi * (hour - 9.0) / 24.0)

        temp_delta = config.temperature_diurnal_amplitude_c * phase
        # Humidity runs counter to temperature over the day.
        humid_delta = -config.humidity_diurnal_amplitude_pct * phase

        kind = self._excursion_kind if self.excursion_active else None
        if kind == "TEMPERATURE":
            temp_delta += self._excursion_delta
        elif kind == "HUMIDITY":
            humid_delta += self._excursion_delta

        return Ambient(
            temperature_c=config.temperature_c + temp_delta,
            humidity_pct=max(0.0, min(100.0, config.humidity_pct + humid_delta)),
            temperature_delta=temp_delta,
            humidity_delta=humid_delta,
            excursion_kind=kind,
        )
