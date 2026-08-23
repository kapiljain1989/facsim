"""Central event bus.

Every event is validated against its declaration in ``event_types.yaml``: an
undeclared type, or a payload missing a required field, raises immediately. That
turns a silent data-quality problem — an event nobody notices is malformed until
analysis time — into a startup or first-emission failure.

Subscribers are invoked in registration order so the event stream is
reproducible, and exceptions in one subscriber never suppress the others.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pharma_sim.registry.event_types import EventTypeRegistry

__all__ = ["Event", "EventBus", "ALL_EVENTS"]

logger = logging.getLogger(__name__)

#: Subscribe with this to receive every event type.
ALL_EVENTS = "*"


@dataclass(slots=True)
class Event:
    """A single simulation event, carrying the §26 field set."""

    event_id: str
    timestamp: datetime
    event_type: str
    plant_id: str
    severity: str
    category: str
    run_id: str
    unit_id: str | None = None
    machine_id: str | None = None
    batch_id: str | None = None
    employee_id: str | None = None
    shift_instance_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        """Flat mapping for relational storage."""
        return {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "category": self.category,
            "severity": self.severity,
            "plant_id": self.plant_id,
            "unit_id": self.unit_id,
            "machine_id": self.machine_id,
            "batch_id": self.batch_id,
            "employee_id": self.employee_id,
            "shift_instance_id": self.shift_instance_id,
            "run_id": self.run_id,
            "payload": self.payload,
        }

    def as_message(self) -> dict[str, Any]:
        """JSON-friendly shape for streaming sinks."""
        row = self.as_row()
        row["timestamp"] = self.timestamp.isoformat()
        row["kind"] = "event"
        return row


class EventBus:
    """Publishes validated events to ordered subscribers.

    Args:
        registry: the declared event vocabulary.
        plant_id: stamped onto every event.
        run_id: stamped onto every event, so runs remain separable in storage.
        id_factory: supplies deterministic event ids.
    """

    __slots__ = (
        "_registry",
        "_plant_id",
        "_run_id",
        "_next_id",
        "_subscribers",
        "_wildcard",
        "_counts",
        "_emitted_types",
    )

    def __init__(
        self,
        registry: EventTypeRegistry,
        *,
        plant_id: str,
        run_id: str,
        next_id: Callable[[], str],
    ) -> None:
        self._registry = registry
        self._plant_id = plant_id
        self._run_id = run_id
        self._next_id = next_id
        self._subscribers: dict[str, list[Callable[[Event], None]]] = {}
        self._wildcard: list[Callable[[Event], None]] = []
        self._counts: dict[str, int] = {}
        self._emitted_types: set[str] = set()

    # ---------------------------------------------------------- subscription
    def subscribe(
        self, event_type: str | Iterable[str], handler: Callable[[Event], None]
    ) -> None:
        """Register ``handler``; pass :data:`ALL_EVENTS` for every type."""
        if event_type == ALL_EVENTS:
            self._wildcard.append(handler)
            return
        types = [event_type] if isinstance(event_type, str) else list(event_type)
        for name in types:
            if not self._registry.has(name):
                raise KeyError(
                    f"cannot subscribe to undeclared event type {name!r}; "
                    f"declare it in event_types.yaml"
                )
            self._subscribers.setdefault(name, []).append(handler)

    # ------------------------------------------------------------- publishing
    def publish(
        self,
        event_type: str,
        timestamp: datetime,
        *,
        unit_id: str | None = None,
        machine_id: str | None = None,
        batch_id: str | None = None,
        employee_id: str | None = None,
        shift_instance_id: str | None = None,
        severity: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Event:
        """Validate and dispatch one event.

        Raises:
            KeyError: the event type is not declared.
            ValueError: the payload is missing a declared required field.
        """
        spec = self._registry.get(event_type)
        data = payload or {}
        missing = [name for name in spec.required_fields if name not in data]
        if missing:
            raise ValueError(
                f"event {event_type!r} is missing required payload field(s) "
                f"{missing}; declared in event_types.yaml as {spec.required_fields}"
            )

        event = Event(
            event_id=self._next_id(),
            timestamp=timestamp,
            event_type=event_type,
            plant_id=self._plant_id,
            severity=severity or spec.default_severity,
            category=spec.category,
            run_id=self._run_id,
            unit_id=unit_id,
            machine_id=machine_id,
            batch_id=batch_id,
            employee_id=employee_id,
            shift_instance_id=shift_instance_id,
            payload=data,
        )

        self._counts[event_type] = self._counts.get(event_type, 0) + 1
        self._emitted_types.add(event_type)

        for handler in self._subscribers.get(event_type, ()):
            self._dispatch(handler, event)
        for handler in self._wildcard:
            self._dispatch(handler, event)
        return event

    @staticmethod
    def _dispatch(handler: Callable[[Event], None], event: Event) -> None:
        try:
            handler(event)
        except Exception:
            # One misbehaving subscriber must not silently drop the event for the
            # others, nor abort the simulation; log with context and continue.
            logger.exception(
                "event subscriber failed", extra={"event_type": event.event_type}
            )

    # ---------------------------------------------------------------- reports
    @property
    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    @property
    def total(self) -> int:
        return sum(self._counts.values())

    def emitted_types(self) -> frozenset[str]:
        return frozenset(self._emitted_types)

    def __iter__(self) -> Iterator[tuple[str, int]]:
        return iter(sorted(self._counts.items()))
