"""Event type registry and the emitter self-check."""

from __future__ import annotations

from pharma_sim.config.models import EventTypeSpec, EventTypesConfig

__all__ = ["EventTypeRegistry", "UndeclaredEventTypes"]


class UndeclaredEventTypes(Exception):
    """Raised when the engine would emit event types the config does not declare."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(
            "the engine emits event type(s) that event_types.yaml does not declare: "
            f"{missing}. Add them, or the events would be silently unroutable."
        )


class EventTypeRegistry:
    """The declared event vocabulary, with severities and payload contracts."""

    __slots__ = ("_types", "_severities", "_order")

    def __init__(self, config: EventTypesConfig) -> None:
        self._types: dict[str, EventTypeSpec] = {spec.id: spec for spec in config.event_types}
        self._severities: tuple[str, ...] = tuple(config.severities)
        self._order: tuple[str, ...] = tuple(spec.id for spec in config.event_types)

    def __len__(self) -> int:
        return len(self._types)

    def __contains__(self, event_type: object) -> bool:
        return event_type in self._types

    @property
    def ids(self) -> tuple[str, ...]:
        return self._order

    @property
    def severities(self) -> tuple[str, ...]:
        return self._severities

    def has(self, event_type: str) -> bool:
        return event_type in self._types

    def get(self, event_type: str) -> EventTypeSpec:
        try:
            return self._types[event_type]
        except KeyError:
            raise KeyError(
                f"unknown event type {event_type!r}; declare it in event_types.yaml. "
                f"Declared: {sorted(self._types)}"
            ) from None

    def streamed(self) -> frozenset[str]:
        """Event types configured to reach streaming sinks."""
        return frozenset(spec.id for spec in self._types.values() if spec.stream)

    def by_category(self, category: str) -> tuple[str, ...]:
        return tuple(
            spec.id for spec in self._types.values() if spec.category == category
        )

    def verify_emitters(self, emitted: frozenset[str]) -> None:
        """Fail fast if the engine emits an undeclared event type.

        Called at startup with the full set of event types the code can publish,
        so a typo surfaces immediately rather than at hour 300 of a long run.
        """
        missing = sorted(emitted - set(self._types))
        if missing:
            raise UndeclaredEventTypes(missing)
