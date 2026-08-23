"""Streaming sink interface and shared statistics."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = ["EventSink", "SinkStats"]


@dataclass(slots=True)
class SinkStats:
    """Per-sink counters.

    ``dropped`` exists because a bounded queue must be honest. Silently
    discarding messages would make a truncated stream look complete, so drops are
    counted, logged and surfaced by ``status``.
    """

    name: str
    sent: int = 0
    dropped: int = 0
    errors: int = 0
    reconnects: int = 0
    buffered: int = 0
    connected: bool = False
    last_error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {
            "sink": self.name,
            "sent": self.sent,
            "dropped": self.dropped,
            "errors": self.errors,
            "reconnects": self.reconnects,
            "buffered": self.buffered,
            "connected": self.connected,
            "last_error": self.last_error,
            **self.extra,
        }


@runtime_checkable
class EventSink(Protocol):
    """A destination for the live message stream.

    Implementations must be non-blocking from the simulation's point of view and
    must never raise into the caller: a broker that is down is a sink problem, not
    a reason to stop generating a factory's data.
    """

    @property
    def name(self) -> str: ...

    def open(self) -> None: ...

    def write(self, batch: list[dict[str, Any]]) -> None: ...

    def flush(self) -> None: ...

    def close(self) -> None: ...

    def stats(self) -> SinkStats: ...
