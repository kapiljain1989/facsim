"""Simulated PLC.

Each machine has one PLC holding its tags in the memory areas a real controller
would use (§9). The tag map is derived entirely from the machine's resolved
sensor specification, so adding a tag in YAML adds a PLC address with no code
change.

The PLC is the read surface an OPC-UA or Modbus adapter would sit on later; the
streaming sinks read the same values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pharma_sim.config.models import SensorSpec

__all__ = ["PlcTag", "Plc"]

#: Memory areas, mirroring conventional PLC addressing.
_AREAS = ("DI", "DO", "AI", "AO", "COUNTER")


@dataclass(slots=True)
class PlcTag:
    """One addressable PLC tag."""

    name: str
    area: str
    address: str
    unit: str
    value: float = 0.0
    quality: str = "GOOD"
    updated_at: datetime | None = None

    def as_row(self, plc_id: str, machine_id: str) -> dict[str, Any]:
        return {
            "plc_id": plc_id,
            "machine_id": machine_id,
            "tag_name": self.name,
            "area": self.area,
            "address": self.address,
            "unit": self.unit,
        }


class Plc:
    """A machine's controller: tag map, alarms, counters and state word.

    Args:
        plc_id: controller identifier.
        machine_id: the machine it controls.
        specs: resolved sensor specifications; each becomes a tag.
    """

    __slots__ = (
        "plc_id",
        "machine_id",
        "_tags",
        "_by_area",
        "_alarms",
        "_alarm_code",
        "_state_word",
        "_scan_count",
        "_last_scan",
    )

    def __init__(self, plc_id: str, machine_id: str, specs: tuple[SensorSpec, ...]) -> None:
        self.plc_id = plc_id
        self.machine_id = machine_id
        self._tags: dict[str, PlcTag] = {}
        self._by_area: dict[str, list[str]] = {area: [] for area in _AREAS}

        counters: dict[str, int] = {area: 0 for area in _AREAS}
        for spec in specs:
            area = spec.plc_area
            index = counters[area]
            counters[area] += 1
            self._tags[spec.tag] = PlcTag(
                name=spec.tag,
                area=area,
                address=self._address(area, index),
                unit=spec.unit,
            )
            self._by_area[area].append(spec.tag)

        # Every controller carries these regardless of instrumentation.
        self._alarms: dict[str, str] = {}
        self._alarm_code = 0
        self._state_word = 0
        self._scan_count = 0
        self._last_scan: datetime | None = None

    @staticmethod
    def _address(area: str, index: int) -> str:
        prefixes = {"DI": "I", "DO": "Q", "AI": "IW", "AO": "QW", "COUNTER": "C"}
        return f"%{prefixes[area]}{index * 2}"

    # -------------------------------------------------------------------- tags
    def __len__(self) -> int:
        return len(self._tags)

    def __contains__(self, tag: object) -> bool:
        return tag in self._tags

    @property
    def tags(self) -> tuple[str, ...]:
        return tuple(self._tags)

    def tag(self, name: str) -> PlcTag:
        try:
            return self._tags[name]
        except KeyError:
            raise KeyError(
                f"{self.machine_id}: PLC has no tag {name!r}; tags: {sorted(self._tags)}"
            ) from None

    def area(self, area: str) -> tuple[str, ...]:
        if area not in self._by_area:
            raise KeyError(f"unknown PLC area {area!r}; areas: {_AREAS}")
        return tuple(self._by_area[area])

    def write(self, name: str, value: float, quality: str, at: datetime) -> None:
        tag = self.tag(name)
        tag.value = value
        tag.quality = quality
        tag.updated_at = at

    def read(self, name: str) -> float:
        return self.tag(name).value

    def snapshot(self) -> dict[str, float]:
        """Current value of every tag, for dashboards and the query facade."""
        return {name: tag.value for name, tag in self._tags.items()}

    def tag_rows(self) -> list[dict[str, Any]]:
        """Persistable tag map, so telemetry rows resolve to a real address."""
        return [tag.as_row(self.plc_id, self.machine_id) for tag in self._tags.values()]

    # ------------------------------------------------------------------ status
    def scan(self, at: datetime, state_word: int) -> None:
        """Record one controller scan cycle."""
        self._scan_count += 1
        self._last_scan = at
        self._state_word = state_word

    @property
    def state_word(self) -> int:
        return self._state_word

    @property
    def scan_count(self) -> int:
        return self._scan_count

    @property
    def last_scan(self) -> datetime | None:
        return self._last_scan

    # ------------------------------------------------------------------ alarms
    def raise_alarm(self, code: str, description: str) -> bool:
        """Set an alarm; returns ``True`` only on the transition into alarm."""
        if code in self._alarms:
            return False
        self._alarms[code] = description
        self._alarm_code = len(self._alarms)
        return True

    def clear_alarm(self, code: str) -> bool:
        if code not in self._alarms:
            return False
        del self._alarms[code]
        self._alarm_code = len(self._alarms)
        return True

    def clear_all_alarms(self) -> None:
        self._alarms.clear()
        self._alarm_code = 0

    @property
    def active_alarms(self) -> tuple[str, ...]:
        return tuple(sorted(self._alarms))

    @property
    def alarm_code(self) -> int:
        """Count of active alarms, exposed as the conventional ALARM_CODE word."""
        return self._alarm_code

    @property
    def alarm_count(self) -> int:
        return len(self._alarms)
