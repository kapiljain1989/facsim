"""Resolution of an equipment class's effective sensor set.

A machine's sensors come from up to three layers, applied in order:

1. an optional named profile from ``sensors.yaml``,
2. per-equipment-class bindings from ``machines.yaml``,
3. per-machine-group bindings, also from ``machines.yaml``.

Each layer may add a tag, patch fields of an inherited tag, or remove one. This
is what lets two tablet presses differ without inventing a near-duplicate
profile, and it is why no equipment-class-to-profile mapping is imposed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pharma_sim.config.models import (
    EquipmentClassSpec,
    SensorBinding,
    SensorSpec,
    SensorsConfig,
)

__all__ = ["SensorBindingError", "resolve_sensor_specs"]


class SensorBindingError(Exception):
    """Raised when a binding refers to a tag or profile that cannot be resolved."""


def _apply_binding(
    resolved: dict[str, SensorSpec],
    binding: SensorBinding,
    *,
    origin: str,
    default_rate_s: float,
) -> None:
    if binding.remove:
        if binding.tag not in resolved:
            raise SensorBindingError(
                f"{origin}: cannot remove sensor {binding.tag!r} because it is not "
                f"inherited (available: {sorted(resolved)})"
            )
        del resolved[binding.tag]
        return

    fields = binding.inline_fields()
    existing = resolved.get(binding.tag)

    if existing is not None:
        resolved[binding.tag] = existing.model_copy(update=fields)
        return

    if binding.override is not None:
        raise SensorBindingError(
            f"{origin}: sensor {binding.tag!r} uses 'override' but is not inherited "
            f"from a profile; define it inline instead"
        )
    if "baseline" not in fields:
        raise SensorBindingError(
            f"{origin}: sensor {binding.tag!r} is new and therefore needs at least "
            f"a 'baseline'"
        )
    fields.setdefault("rate_s", default_rate_s)
    resolved[binding.tag] = SensorSpec(tag=binding.tag, **fields)


def resolve_sensor_specs(
    equipment_class: EquipmentClassSpec,
    sensors_config: SensorsConfig,
    extra_bindings: Sequence[SensorBinding] | None = None,
    *,
    origin: str | None = None,
) -> list[SensorSpec]:
    """Return the effective, deterministically ordered sensor set.

    Inherited tags keep their profile order; newly added tags follow in binding
    order, so the resulting list is stable across runs.

    Raises:
        SensorBindingError: if the profile is unknown, or a binding removes or
            overrides a tag that was never inherited.
    """
    label = origin or f"equipment class {equipment_class.id!r}"
    resolved: dict[str, SensorSpec] = {}

    if equipment_class.sensor_profile is not None:
        profile = sensors_config.profiles.get(equipment_class.sensor_profile)
        if profile is None:
            raise SensorBindingError(
                f"{label}: unknown sensor_profile "
                f"{equipment_class.sensor_profile!r} (available: "
                f"{sorted(sensors_config.profiles)})"
            )
        for spec in profile:
            if spec.rate_s is None:
                spec = spec.model_copy(update={"rate_s": sensors_config.default_rate_s})
            resolved[spec.tag] = spec

    layers: Iterable[tuple[str, Sequence[SensorBinding]]] = (
        (label, equipment_class.sensors),
        (f"{label} (machine group)", tuple(extra_bindings or ())),
    )
    for layer_label, bindings in layers:
        for binding in bindings:
            _apply_binding(
                resolved,
                binding,
                origin=layer_label,
                default_rate_s=sensors_config.default_rate_s,
            )

    return list(resolved.values())
