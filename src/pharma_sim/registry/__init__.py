"""Runtime registries holding the factory vocabulary declared in configuration.

The engine reads everything about states, events, equipment, sensors, failure
modes, QC parameters, units and products from these objects. No domain term is a
Python enum or literal, which is what allows a schema change in YAML to take
effect with no code change.
"""

from __future__ import annotations

from dataclasses import dataclass

from pharma_sim.config.models import FactoryConfig
from pharma_sim.registry.equipment import EquipmentRegistry, ResolvedEquipment
from pharma_sim.registry.event_types import EventTypeRegistry, UndeclaredEventTypes
from pharma_sim.registry.failures import ApplicableMode, FailureRegistry, degradation_curve
from pharma_sim.registry.qc import EffectiveQcSpec, QcRegistry
from pharma_sim.registry.sensor_binding import SensorBindingError, resolve_sensor_specs
from pharma_sim.registry.states import IllegalTransition, StateRegistry
from pharma_sim.registry.topology import TopologyRegistry

__all__ = [
    "ApplicableMode",
    "EffectiveQcSpec",
    "EquipmentRegistry",
    "EventTypeRegistry",
    "FailureRegistry",
    "IllegalTransition",
    "QcRegistry",
    "Registries",
    "ResolvedEquipment",
    "SensorBindingError",
    "StateRegistry",
    "TopologyRegistry",
    "UndeclaredEventTypes",
    "degradation_curve",
    "resolve_sensor_specs",
]


@dataclass(frozen=True, slots=True)
class Registries:
    """Every registry, built once from a validated configuration."""

    states: StateRegistry
    event_types: EventTypeRegistry
    equipment: EquipmentRegistry
    failures: FailureRegistry
    qc: QcRegistry
    topology: TopologyRegistry

    @classmethod
    def build(cls, config: FactoryConfig) -> Registries:
        equipment = EquipmentRegistry(config.machines, config.sensors)
        return cls(
            states=StateRegistry(config.states),
            event_types=EventTypeRegistry(config.event_types),
            equipment=equipment,
            failures=FailureRegistry(config.failures, equipment),
            qc=QcRegistry(config.qc_rules),
            topology=TopologyRegistry(config.units, config.products),
        )
