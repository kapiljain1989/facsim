"""Sink router: fan-out that cannot stall the simulation.

Every sink gets its own worker thread behind a bounded queue. The simulation
enqueues and moves on, so a slow broker or a blocked pipe changes nothing about
the clock. When a queue is full the oldest batch is dropped and counted — never
silently — because a stream that quietly loses data while reporting success is
worse than one that admits it.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from pharma_sim.config.models import SinkSpec, SinksConfig
from pharma_sim.streaming.base import EventSink, SinkStats
from pharma_sim.streaming.jsonl_sink import JsonlSink
from pharma_sim.streaming.mqtt_sink import MqttSink

__all__ = ["SinkRouter", "build_sinks"]

logger = logging.getLogger(__name__)

_SENTINEL: object = object()


class _SinkWorker:
    """One sink, its queue and its thread."""

    def __init__(self, sink: EventSink, queue_size: int, batch_size: int) -> None:
        self.sink = sink
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self._batch_size = batch_size
        self._thread: threading.Thread | None = None
        self._dropped = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        self.sink.open()
        self._thread = threading.Thread(
            target=self._run, name=f"sink-{self.sink.name}", daemon=True
        )
        self._thread.start()

    def submit(self, batch: list[dict[str, Any]]) -> None:
        try:
            self._queue.put_nowait(batch)
        except queue.Full:
            # Drop the oldest so the newest data survives, and account for it.
            try:
                self._queue.get_nowait()
                with self._lock:
                    self._dropped += 1
                self._queue.put_nowait(batch)
            except (queue.Empty, queue.Full):  # pragma: no cover - race under load
                with self._lock:
                    self._dropped += 1

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                self._queue.task_done()
                return
            try:
                self.sink.write(item)
            except Exception:  # pragma: no cover - defensive
                logger.exception("sink %s failed to write", self.sink.name)
            finally:
                self._queue.task_done()

    def drain(self, timeout: float = 10.0) -> None:
        """Wait for queued work, then flush the sink."""
        deadline = threading.Event()
        waiter = threading.Thread(target=lambda: (self._queue.join(), deadline.set()))
        waiter.daemon = True
        waiter.start()
        deadline.wait(timeout)
        try:
            self.sink.flush()
        except Exception:  # pragma: no cover
            logger.exception("sink %s failed to flush", self.sink.name)

    def stop(self, timeout: float = 10.0) -> None:
        self.drain(timeout)
        self._queue.put(_SENTINEL)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        try:
            self.sink.close()
        except Exception:  # pragma: no cover
            logger.exception("sink %s failed to close", self.sink.name)

    def stats(self) -> SinkStats:
        stats = self.sink.stats()
        with self._lock:
            stats.dropped = max(stats.dropped, self._dropped)
        stats.extra["queued"] = self._queue.qsize()
        return stats

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped


class SinkRouter:
    """Fans one message stream out to every enabled sink."""

    def __init__(self, batch_size: int = 500) -> None:
        self._workers: list[_SinkWorker] = []
        self._batch: list[dict[str, Any]] = []
        self._batch_size = batch_size
        self._running = False
        self._submitted = 0

    @property
    def active(self) -> bool:
        return self._running and bool(self._workers)

    @property
    def sink_names(self) -> tuple[str, ...]:
        return tuple(worker.sink.name for worker in self._workers)

    def add(self, sink: EventSink, *, queue_size: int, batch_size: int) -> None:
        self._workers.append(_SinkWorker(sink, queue_size, batch_size))

    def start(self) -> None:
        for worker in self._workers:
            worker.start()
        self._running = bool(self._workers)

    def publish(self, message: dict[str, Any]) -> None:
        """Enqueue one message; batches are dispatched when full."""
        if not self._running:
            return
        self._batch.append(message)
        self._submitted += 1
        if len(self._batch) >= self._batch_size:
            self.dispatch()

    def publish_many(self, messages: list[dict[str, Any]]) -> None:
        if not self._running or not messages:
            return
        self._batch.extend(messages)
        self._submitted += len(messages)
        if len(self._batch) >= self._batch_size:
            self.dispatch()

    def dispatch(self) -> None:
        """Hand the current batch to every sink."""
        if not self._batch:
            return
        batch = self._batch
        self._batch = []
        for worker in self._workers:
            # Each sink gets its own list: a sink must not be able to mutate what
            # another sink sees.
            worker.submit(list(batch))

    def flush(self) -> None:
        self.dispatch()
        for worker in self._workers:
            worker.drain()

    def stop(self) -> None:
        """Graceful shutdown: dispatch, drain, flush, close."""
        self.dispatch()
        for worker in self._workers:
            worker.stop()
        self._running = False

    def stats(self) -> list[SinkStats]:
        return [worker.stats() for worker in self._workers]

    @property
    def submitted(self) -> int:
        return self._submitted

    @property
    def total_dropped(self) -> int:
        return sum(worker.dropped for worker in self._workers)


def build_sinks(
    config: SinksConfig,
    *,
    selected: tuple[str, ...] | None = None,
    mqtt_client_factory=None,
) -> SinkRouter:
    """Build the router from configuration.

    Args:
        selected: sink names to enable regardless of their ``enabled`` flag, as
            ``--sink jsonl,mqtt`` does. ``None`` honours the config.
        mqtt_client_factory: injected MQTT client, used by tests.
    """
    router = SinkRouter()
    wanted = set(selected) if selected else None

    for spec in config.sinks:
        enabled = spec.enabled if wanted is None else spec.name in wanted
        if not enabled:
            continue
        sink = _build_sink(spec, mqtt_client_factory)
        router.add(sink, queue_size=spec.queue_size, batch_size=spec.batch_size)

    if wanted:
        known = {spec.name for spec in config.sinks}
        for name in sorted(wanted - known):
            raise KeyError(
                f"unknown sink {name!r}; declared in sinks.yaml: {sorted(known)}"
            )
    return router


def _build_sink(spec: SinkSpec, mqtt_client_factory) -> EventSink:
    if spec.type == "jsonl":
        return JsonlSink(spec.name, spec.jsonl)
    if spec.type == "mqtt":
        return MqttSink(spec.name, spec.mqtt, client_factory=mqtt_client_factory)
    raise ValueError(f"unsupported sink type {spec.type!r}")
