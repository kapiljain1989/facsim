"""Deterministic discrete-event scheduler.

The simulator is event-driven rather than a fixed-step loop, so a 1-second sensor
tag and an hourly hazard evaluation coexist without either being oversampled
(§12). Work is held in a heap keyed by ``(when, priority, sequence)``.

The sequence number is what makes runs reproducible: two callbacks scheduled for
the same instant with the same priority always fire in the order they were
queued, so the event stream does not depend on heap internals or dict ordering.
"""

from __future__ import annotations

import heapq
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = ["Priority", "ScheduledTask", "Scheduler"]


class Priority:
    """Ordering for work due at the same instant, lowest first.

    The ordering is deliberate: a shift must open before employees clock in, a
    machine state must settle before its sensors are sampled, and telemetry must
    be produced before production is accounted for.
    """

    CLOCK = 0
    SHIFT = 10
    EMPLOYEE = 20
    MAINTENANCE = 30
    MACHINE = 40
    FAILURE = 50
    SENSOR = 60
    PRODUCTION = 70
    BATCH = 80
    QUALITY = 90
    ANALYSIS = 100
    LABEL = 110
    PERSIST = 120


@dataclass(order=True, slots=True)
class ScheduledTask:
    """One unit of future work."""

    when: datetime
    priority: int
    sequence: int
    callback: Callable[[datetime], None] = field(compare=False)
    label: str = field(compare=False, default="")
    cancelled: bool = field(compare=False, default=False)

    def cancel(self) -> None:
        self.cancelled = True


class Scheduler:
    """A priority queue of future callbacks, drained in simulated-time order."""

    __slots__ = ("_heap", "_counter", "_executed", "_cancelled")

    def __init__(self) -> None:
        self._heap: list[ScheduledTask] = []
        self._counter = itertools.count()
        self._executed = 0
        self._cancelled = 0

    def __len__(self) -> int:
        return len(self._heap)

    @property
    def executed(self) -> int:
        return self._executed

    @property
    def pending(self) -> int:
        return len(self._heap)

    def at(
        self,
        when: datetime,
        callback: Callable[[datetime], None],
        *,
        priority: int = Priority.MACHINE,
        label: str = "",
    ) -> ScheduledTask:
        """Schedule ``callback`` to run at simulated instant ``when``."""
        task = ScheduledTask(
            when=when,
            priority=priority,
            sequence=next(self._counter),
            callback=callback,
            label=label,
        )
        heapq.heappush(self._heap, task)
        return task

    def peek_time(self) -> datetime | None:
        """When the next live task is due, skipping cancelled ones."""
        while self._heap and self._heap[0].cancelled:
            heapq.heappop(self._heap)
            self._cancelled += 1
        return self._heap[0].when if self._heap else None

    def run_until(self, horizon: datetime, advance: Callable[[datetime], None]) -> int:
        """Execute every task due at or before ``horizon``.

        Args:
            horizon: simulated instant to stop at, inclusive.
            advance: moves the clock to a task's due time before it runs, so a
                callback always observes the instant it was scheduled for.

        Returns:
            How many tasks executed.
        """
        executed = 0
        while self._heap:
            task = self._heap[0]
            if task.cancelled:
                heapq.heappop(self._heap)
                self._cancelled += 1
                continue
            if task.when > horizon:
                break
            heapq.heappop(self._heap)
            advance(task.when)
            task.callback(task.when)
            executed += 1
            self._executed += 1
        return executed

    def drain(self) -> None:
        self._heap.clear()

    def stats(self) -> dict[str, Any]:
        return {
            "pending": len(self._heap),
            "executed": self._executed,
            "cancelled": self._cancelled,
        }
