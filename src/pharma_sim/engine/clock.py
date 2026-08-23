"""Simulation clock.

Simulated time is fully decoupled from wall time. In ``FAST_FORWARD`` the clock
never sleeps, so thirty days of plant history costs seconds; in ``PACED`` it
throttles to a configurable ratio of simulated minutes per real second, which is
what makes the live feed resemble an actual factory gateway.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import Enum

__all__ = ["ClockMode", "ClockState", "SimulationClock"]


class ClockMode(str, Enum):
    """How simulated time relates to wall time."""

    FAST_FORWARD = "FAST_FORWARD"
    PACED = "PACED"


class ClockState(str, Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"


class SimulationClock:
    """Holds simulated time and, in paced mode, throttles against wall time.

    Args:
        start_time: simulated instant the clock begins at.
        mode: fast-forward (no sleeping) or paced.
        sim_minutes_per_real_second: pacing ratio, used only in paced mode.
        monotonic: injected wall-clock source, so tests can drive pacing without
            actually sleeping.
        sleeper: injected sleep function, for the same reason.
    """

    __slots__ = (
        "_start_time",
        "_now",
        "_mode",
        "_ratio",
        "_state",
        "_monotonic",
        "_sleeper",
        "_anchor_real",
        "_anchor_sim",
        "_paused_at",
    )

    def __init__(
        self,
        start_time: datetime,
        *,
        mode: ClockMode = ClockMode.FAST_FORWARD,
        sim_minutes_per_real_second: float = 60.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if sim_minutes_per_real_second <= 0.0:
            raise ValueError("sim_minutes_per_real_second must be positive")
        self._start_time = start_time
        self._now = start_time
        self._mode = mode
        self._ratio = sim_minutes_per_real_second
        self._state = ClockState.STOPPED
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._anchor_real = 0.0
        self._anchor_sim = start_time
        self._paused_at: datetime | None = None

    # ---------------------------------------------------------------- state
    @property
    def now(self) -> datetime:
        """Current simulated instant."""
        return self._now

    @property
    def start_time(self) -> datetime:
        return self._start_time

    @property
    def state(self) -> ClockState:
        return self._state

    @property
    def mode(self) -> ClockMode:
        return self._mode

    @property
    def is_running(self) -> bool:
        return self._state is ClockState.RUNNING

    @property
    def elapsed(self) -> timedelta:
        """Simulated time covered since the start."""
        return self._now - self._start_time

    @property
    def elapsed_hours(self) -> float:
        return self.elapsed.total_seconds() / 3600.0

    def set_mode(self, mode: ClockMode, sim_minutes_per_real_second: float | None = None) -> None:
        """Switch pacing mode mid-run, e.g. backfill then go live."""
        self._mode = mode
        if sim_minutes_per_real_second is not None:
            if sim_minutes_per_real_second <= 0.0:
                raise ValueError("sim_minutes_per_real_second must be positive")
            self._ratio = sim_minutes_per_real_second
        self._rebase()

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._state is ClockState.RUNNING:
            return
        self._state = ClockState.RUNNING
        self._paused_at = None
        self._rebase()

    def pause(self) -> None:
        if self._state is not ClockState.RUNNING:
            return
        self._state = ClockState.PAUSED
        self._paused_at = self._now

    def resume(self) -> None:
        if self._state is not ClockState.PAUSED:
            return
        self._state = ClockState.RUNNING
        self._paused_at = None
        self._rebase()

    def stop(self) -> None:
        self._state = ClockState.STOPPED

    def reset(self) -> None:
        """Return to the start instant and the stopped state."""
        self._now = self._start_time
        self._state = ClockState.STOPPED
        self._paused_at = None
        self._rebase()

    # -------------------------------------------------------------- advance
    def advance_to(self, target: datetime) -> None:
        """Move simulated time forward to ``target``.

        In paced mode this sleeps for however long the ratio implies. Time never
        moves backwards: an earlier target is ignored rather than rewinding
        history, which would corrupt any accumulated state.
        """
        if target <= self._now:
            return
        if self._mode is ClockMode.PACED:
            self._sleep_until(target)
        self._now = target

    def advance(self, delta: timedelta) -> None:
        if delta.total_seconds() < 0:
            raise ValueError("cannot advance by a negative duration")
        self.advance_to(self._now + delta)

    def deadline(
        self,
        *,
        days: float = 0.0,
        hours: float = 0.0,
        minutes: float = 0.0,
        seconds: float = 0.0,
    ) -> datetime:
        """Simulated instant a given duration from now."""
        return self._now + timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)

    # --------------------------------------------------------------- pacing
    def _rebase(self) -> None:
        """Re-anchor the wall-clock reference to the current simulated instant."""
        self._anchor_real = self._monotonic()
        self._anchor_sim = self._now

    def _sleep_until(self, target: datetime) -> None:
        sim_seconds = (target - self._anchor_sim).total_seconds()
        real_seconds_required = sim_seconds / (self._ratio * 60.0)
        already_elapsed = self._monotonic() - self._anchor_real
        remaining = real_seconds_required - already_elapsed
        if remaining > 0.0:
            self._sleeper(remaining)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"SimulationClock(now={self._now.isoformat()}, state={self._state.value}, "
            f"mode={self._mode.value}, ratio={self._ratio})"
        )
