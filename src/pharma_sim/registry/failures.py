"""Failure mode registry.

Resolves which modes can affect which equipment, and precomputes each mode's
precursor list per equipment class. A mode's precursor tag that the equipment
does not carry is dropped here rather than checked on every sample.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from pharma_sim.config.models import (
    FailureModeSpec,
    FailuresConfig,
    PrecursorSpec,
    Transfer,
)
from pharma_sim.registry.equipment import EquipmentRegistry

__all__ = ["FailureRegistry", "degradation_curve"]


def degradation_curve(progress: float, curve: str) -> float:
    """Map incubation progress in ``[0, 1]`` onto degradation in ``[0, 1]``.

    The shape matters for detectability. ``linear`` degrades steadily;
    ``exponential`` stays quiet then accelerates, which is what a real bearing
    does; ``late_knee`` hides almost entirely until close to failure, giving a
    short warning window that a predictive model has to work for.
    """
    p = min(max(progress, 0.0), 1.0)
    if curve == "linear":
        return p
    if curve == "quadratic":
        return p * p
    if curve == "late_knee":
        return p**4
    # exponential: slow start, sharp rise, normalised to reach exactly 1.0
    return (math.exp(3.0 * p) - 1.0) / (math.exp(3.0) - 1.0)


@dataclass(frozen=True, slots=True)
class ApplicableMode:
    """A failure mode as it applies to one equipment class."""

    spec: FailureModeSpec
    precursors: tuple[PrecursorSpec, ...]
    #: Process parameter shifts filtered to those the equipment actually measures.
    parameter_shifts: tuple[tuple[str, float], ...]

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def detectable(self) -> bool:
        # A mode is only really detectable if it also has a signal to show.
        return self.spec.detectable and bool(self.precursors)


class FailureRegistry:
    """Failure modes, their applicability, and the hazard factor curves."""

    __slots__ = ("_modes", "_config", "_by_class", "_root_causes")

    def __init__(self, config: FailuresConfig, equipment: EquipmentRegistry) -> None:
        self._config = config
        self._modes: dict[str, FailureModeSpec] = {spec.id: spec for spec in config.failure_modes}
        self._root_causes = frozenset(spec.root_cause for spec in config.failure_modes)
        self._by_class: dict[str, tuple[ApplicableMode, ...]] = {}

        for class_id in equipment.ids:
            resolved = equipment.get(class_id)
            allowed = set(resolved.spec.failure_modes)
            tags = resolved.tags
            applicable: list[ApplicableMode] = []
            for spec in config.failure_modes:
                if allowed and spec.id not in allowed:
                    continue
                if spec.equipment_classes and class_id not in spec.equipment_classes:
                    continue
                if spec.sensor_profiles and resolved.spec.sensor_profile not in spec.sensor_profiles:
                    continue
                precursors = tuple(p for p in spec.precursors if p.tag in tags)
                shifts = tuple(
                    (name, value)
                    for name, value in spec.effects.process_parameter_shifts.items()
                    if name in tags
                )
                applicable.append(
                    ApplicableMode(spec=spec, precursors=precursors, parameter_shifts=shifts)
                )
            self._by_class[class_id] = tuple(applicable)

    def __len__(self) -> int:
        return len(self._modes)

    def __contains__(self, mode_id: object) -> bool:
        return mode_id in self._modes

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._modes)

    @property
    def root_causes(self) -> frozenset[str]:
        return self._root_causes

    @property
    def hazard_scale(self) -> float:
        return self._config.hazard_scale

    @property
    def max_concurrent_modes(self) -> int:
        return self._config.max_concurrent_modes

    def get(self, mode_id: str) -> FailureModeSpec:
        try:
            return self._modes[mode_id]
        except KeyError:
            raise KeyError(
                f"unknown failure mode {mode_id!r}; declared: {sorted(self._modes)}"
            ) from None

    def for_class(self, class_id: str) -> tuple[ApplicableMode, ...]:
        """Modes that can affect this equipment class, with filtered precursors."""
        return self._by_class.get(class_id, ())

    def applicable(self, class_id: str, mode_id: str) -> ApplicableMode | None:
        for mode in self._by_class.get(class_id, ()):
            if mode.id == mode_id:
                return mode
        return None

    def hazard_multiplier(self, drivers: dict[str, float]) -> float:
        """Product of the configured hazard factors for the given drivers (§14)."""
        factors = self._config.hazard_factors
        transfers: tuple[Transfer, ...] = (
            factors.age,
            factors.operating_hours,
            factors.maintenance_debt,
            factors.load,
            factors.environment,
            factors.operator,
        )
        multiplier = 1.0
        for transfer in transfers:
            multiplier *= max(transfer.evaluate(drivers), 0.0)
        return multiplier

    def category_of(self, mode_id: str) -> str:
        return self.get(mode_id).category
