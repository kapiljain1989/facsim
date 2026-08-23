"""Equipment class registry with resolved sensor sets."""

from __future__ import annotations

from dataclasses import dataclass

from pharma_sim.config.models import (
    EquipmentClassSpec,
    MachineGroupSpec,
    MachinesConfig,
    SensorSpec,
    SensorsConfig,
)
from pharma_sim.registry.sensor_binding import resolve_sensor_specs

__all__ = ["EquipmentRegistry", "ResolvedEquipment"]


@dataclass(frozen=True, slots=True)
class ResolvedEquipment:
    """An equipment class together with its effective sensor set."""

    spec: EquipmentClassSpec
    sensors: tuple[SensorSpec, ...]

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def tags(self) -> frozenset[str]:
        return frozenset(sensor.tag for sensor in self.sensors)

    def process_parameters(self) -> tuple[str, ...]:
        """Tags flagged as process parameters, in declaration order."""
        return tuple(sensor.tag for sensor in self.sensors if sensor.process_parameter)


class EquipmentRegistry:
    """Equipment classes, resolved sensor sets, and per-group resolution."""

    __slots__ = ("_classes", "_sensors_config", "_layout", "_group_cache")

    def __init__(self, machines: MachinesConfig, sensors: SensorsConfig) -> None:
        self._sensors_config = sensors
        self._layout = machines.layout
        self._classes: dict[str, ResolvedEquipment] = {}
        for spec in machines.equipment_classes:
            resolved = resolve_sensor_specs(spec, sensors)
            self._classes[spec.id] = ResolvedEquipment(spec=spec, sensors=tuple(resolved))
        self._group_cache: dict[tuple[str, str], tuple[SensorSpec, ...]] = {}

    def __len__(self) -> int:
        return len(self._classes)

    def __contains__(self, class_id: object) -> bool:
        return class_id in self._classes

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._classes)

    def get(self, class_id: str) -> ResolvedEquipment:
        try:
            return self._classes[class_id]
        except KeyError:
            raise KeyError(
                f"unknown equipment class {class_id!r}; declared: {sorted(self._classes)}"
            ) from None

    def sensors_for_group(self, unit_id: str, group: MachineGroupSpec) -> tuple[SensorSpec, ...]:
        """Effective sensor set for a machine group, applying its own bindings.

        Cached per ``(unit, prefix)``: every machine in a group shares one spec
        list, which matters when a hundred machines each resolve their tags.
        """
        key = (unit_id, group.id_prefix)
        cached = self._group_cache.get(key)
        if cached is not None:
            return cached
        equipment = self.get(group.equipment_class)
        if not group.sensors:
            resolved = equipment.sensors
        else:
            resolved = tuple(
                resolve_sensor_specs(
                    equipment.spec,
                    self._sensors_config,
                    group.sensors,
                    origin=f"machine group {group.id_prefix!r} in {unit_id}",
                )
            )
        self._group_cache[key] = resolved
        return resolved

    def groups_for_unit(self, unit_id: str) -> tuple[MachineGroupSpec, ...]:
        return tuple(self._layout.get(unit_id, ()))

    def all_tags(self) -> frozenset[str]:
        tags: set[str] = set()
        for equipment in self._classes.values():
            tags |= equipment.tags
        return frozenset(tags)

    def classes_with_tag(self, tag: str) -> tuple[str, ...]:
        return tuple(
            class_id for class_id, equipment in self._classes.items() if tag in equipment.tags
        )

    def profile_of(self, class_id: str) -> str | None:
        return self.get(class_id).spec.sensor_profile
