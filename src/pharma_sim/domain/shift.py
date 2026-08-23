"""Shift scheduling.

The awkward case is the shift that crosses midnight: it starts on one calendar
day and ends on the next, so nothing here assumes ``start < end``. Every shift
instance carries explicit start and end instants, computed from the configured
wall-clock times, and breaks are resolved against those instants rather than
against a date.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from pharma_sim.config.models import BreakSpec, ShiftSpec, ShiftsConfig

__all__ = ["ShiftInstance", "ShiftScheduler", "BreakWindow"]


@dataclass(frozen=True, slots=True)
class BreakWindow:
    """A break resolved to absolute instants."""

    label: str
    start: datetime
    end: datetime


@dataclass(slots=True)
class ShiftInstance:
    """One occurrence of a shift on a given date."""

    shift_instance_id: str
    shift_code: str
    plant_id: str
    business_date: date
    start: datetime
    end: datetime
    breaks: tuple[BreakWindow, ...] = ()
    roster: list[str] = field(default_factory=list)
    present: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    def as_row(self) -> dict[str, Any]:
        return {
            "shift_instance_id": self.shift_instance_id,
            "shift_code": self.shift_code,
            "plant_id": self.plant_id,
            "business_date": self.business_date,
            "start_time": self.start,
            "end_time": self.end,
            "duration_hours": round(self.duration_hours, 3),
            "roster_size": len(self.roster),
            "present_count": len(self.present),
            "absent_count": len(self.absent),
        }


class ShiftScheduler:
    """Turns configured shift patterns into dated shift instances."""

    __slots__ = ("_config", "_shifts", "_plant_id")

    def __init__(self, config: ShiftsConfig, plant_id: str) -> None:
        self._config = config
        self._shifts: dict[str, ShiftSpec] = {spec.code: spec for spec in config.shifts}
        self._plant_id = plant_id

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(self._shifts)

    @property
    def config(self) -> ShiftsConfig:
        return self._config

    def spec(self, code: str) -> ShiftSpec:
        try:
            return self._shifts[code]
        except KeyError:
            raise KeyError(
                f"unknown shift code {code!r}; declared: {sorted(self._shifts)}"
            ) from None

    # ------------------------------------------------------------------ instants
    def bounds(self, business_date: date, code: str) -> tuple[datetime, datetime]:
        """Absolute start and end for a shift on ``business_date``.

        A shift whose configured end is at or before its start is understood to
        run into the following day, which is how the night shift is handled
        without special-casing it at every call site.
        """
        spec = self.spec(code)
        start = datetime.combine(business_date, spec.start)
        end = datetime.combine(business_date, spec.end)
        if spec.crosses_midnight:
            end += timedelta(days=1)
        return start, end

    def break_windows(self, start: datetime, end: datetime, breaks: tuple[BreakSpec, ...]) -> tuple[BreakWindow, ...]:
        """Resolve configured break times into instants inside ``[start, end)``.

        A break time earlier than the shift start belongs to the next calendar
        day — the 00:30 tea break on a 22:00 shift, for instance.
        """
        windows: list[BreakWindow] = []
        for spec in breaks:
            moment = datetime.combine(start.date(), spec.start)
            if moment < start:
                moment += timedelta(days=1)
            if moment >= end:
                continue
            finish = min(end, moment + timedelta(minutes=spec.duration_min))
            windows.append(BreakWindow(label=spec.label, start=moment, end=finish))
        return tuple(sorted(windows, key=lambda window: window.start))

    def instance(
        self, business_date: date, code: str, shift_instance_id: str
    ) -> ShiftInstance:
        spec = self.spec(code)
        start, end = self.bounds(business_date, code)
        return ShiftInstance(
            shift_instance_id=shift_instance_id,
            shift_code=code,
            plant_id=self._plant_id,
            business_date=business_date,
            start=start,
            end=end,
            breaks=self.break_windows(start, end, tuple(spec.breaks)),
        )

    def shift_for(self, moment: datetime) -> tuple[date, str] | None:
        """Which shift covers ``moment``, as ``(business_date, code)``.

        Checks the previous calendar day too, so an instant just after midnight
        resolves to the night shift that began the evening before.
        """
        for offset in (0, -1):
            candidate_date = (moment + timedelta(days=offset)).date()
            for code in self._shifts:
                start, end = self.bounds(candidate_date, code)
                if start <= moment < end:
                    return candidate_date, code
        return None

    def upcoming_starts(
        self, after: datetime, horizon_days: int = 1
    ) -> list[tuple[datetime, date, str]]:
        """Shift starts in ``(after, after + horizon]``, in chronological order."""
        results: list[tuple[datetime, date, str]] = []
        for day_offset in range(0, horizon_days + 1):
            candidate_date = (after + timedelta(days=day_offset)).date()
            for code in self._shifts:
                start, _ = self.bounds(candidate_date, code)
                if start > after:
                    results.append((start, candidate_date, code))
        results.sort(key=lambda item: (item[0], item[2]))
        return results

    def first_start_at_or_after(self, moment: datetime) -> tuple[datetime, date, str]:
        """The next shift boundary at or after ``moment``."""
        best: tuple[datetime, date, str] | None = None
        for day_offset in (-1, 0, 1):
            candidate_date = (moment + timedelta(days=day_offset)).date()
            for code in self._shifts:
                start, _ = self.bounds(candidate_date, code)
                if start >= moment and (best is None or start < best[0]):
                    best = (start, candidate_date, code)
        if best is None:  # pragma: no cover - a shift always exists within a day
            raise RuntimeError("no upcoming shift found")
        return best
