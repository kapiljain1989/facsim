"""Embedded live simulator and the fan-out hub that feeds connected browsers.

The dashboard has two modes:

* **Historical** — read the stored dataset. Nothing runs; the API is a query
  surface over whatever the last run produced.
* **Live** — the API hosts a simulator on a background thread and pushes its
  telemetry and events to every connected browser.

The hub sits between them. It is bounded and drop-oldest for the same reason the
streaming sinks are: a browser that stops reading must not be able to slow the
simulation down, and a silently truncated feed is worse than one that reports its
losses.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from pharma_sim.streaming.base import SinkStats

__all__ = ["LiveHub", "LiveSimulator", "HubSink"]

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class _Subscriber:
    """One connected browser.

    ``eq=False`` keeps identity-based hashing: subscribers live in a set and are
    distinguished by which connection they are, not by their contents. A
    value-equal dataclass would be unhashable.
    """

    queue: asyncio.Queue
    dropped: int = 0


@dataclass
class LiveHub:
    """Fans messages from the simulation thread out to async subscribers.

    ``publish`` is called from the simulator's thread and must never block, so it
    hands work to the event loop with ``call_soon_threadsafe`` and drops the
    oldest message for any subscriber that has fallen behind.
    """

    loop: asyncio.AbstractEventLoop | None = None
    max_queue: int = 2000
    #: Recent history, so a browser connecting mid-run sees a populated chart
    #: immediately rather than an empty one that fills over the next minute.
    replay_size: int = 4000
    _subscribers: set[_Subscriber] = field(default_factory=set)
    _replay: deque = field(default_factory=lambda: deque(maxlen=4000))
    _lock: threading.Lock = field(default_factory=threading.Lock)
    published: int = 0
    dropped: int = 0

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def subscribe(self) -> _Subscriber:
        subscriber = _Subscriber(queue=asyncio.Queue(maxsize=self.max_queue))
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def replay(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            messages = list(self._replay)
        return messages[-limit:] if limit else messages

    def publish_many(self, messages: list[dict[str, Any]]) -> None:
        """Called from the simulation thread. Never blocks, never raises."""
        if not messages:
            return
        with self._lock:
            self._replay.extend(messages)
            subscribers = list(self._subscribers)
            self.published += len(messages)
        if not subscribers or self.loop is None or self.loop.is_closed():
            return
        try:
            self.loop.call_soon_threadsafe(self._deliver, subscribers, messages)
        except RuntimeError:  # pragma: no cover - loop shutting down
            pass

    def _deliver(self, subscribers: list[_Subscriber], messages: list[dict[str, Any]]) -> None:
        for subscriber in subscribers:
            for message in messages:
                try:
                    subscriber.queue.put_nowait(message)
                except asyncio.QueueFull:
                    # Drop the oldest so the newest data survives, and count it.
                    try:
                        subscriber.queue.get_nowait()
                        subscriber.queue.put_nowait(message)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass
                    subscriber.dropped += 1
                    self.dropped += 1


class HubSink:
    """An :class:`~pharma_sim.streaming.base.EventSink` that feeds the hub.

    Registering as a normal sink means the dashboard sees exactly the same
    messages MQTT and JSONL consumers do — one stream, several destinations.
    """

    def __init__(self, hub: LiveHub, name: str = "dashboard") -> None:
        self._hub = hub
        self._name = name
        self._stats = SinkStats(name=name, connected=True)

    @property
    def name(self) -> str:
        return self._name

    def open(self) -> None:
        self._stats.connected = True

    def write(self, batch: list[dict[str, Any]]) -> None:
        self._hub.publish_many(batch)
        self._stats.sent += len(batch)
        self._stats.dropped = self._hub.dropped

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self._stats.connected = False

    def stats(self) -> SinkStats:
        # The hub owns the drop counter, so read it here rather than relying on
        # the copy taken during the last write.
        self._stats.dropped = self._hub.dropped
        self._stats.extra["subscribers"] = self._hub.subscriber_count
        return self._stats


class LiveSimulator:
    """Runs a simulator on a background thread, feeding the hub."""

    def __init__(
        self,
        config_dir: str | Path,
        hub: LiveHub,
        *,
        seed: int | None = None,
        speed: float = 60.0,
        warmup_hours: float = 0.0,
        sinks: tuple[str, ...] = (),
    ) -> None:
        self._config_dir = Path(config_dir)
        self._hub = hub
        self._seed = seed
        self._speed = speed
        self._warmup_hours = warmup_hours
        self._sinks = sinks
        self._thread: threading.Thread | None = None
        self._sim = None
        self._error: BaseException | None = None
        self._ready = threading.Event()

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        from pharma_sim.simulator import Simulator

        sim = Simulator(
            self._config_dir,
            seed=self._seed,
            sinks=self._sinks or None,
            reset_storage=True,
            configure_logs=False,
            log_level="WARNING",
        )
        # Register the hub as an additional sink, so the browser feed is the same
        # stream the other sinks receive.
        sim.sinks.add(HubSink(self._hub), queue_size=20_000, batch_size=200)
        sim._streaming = True
        sim.telemetry.set_streaming(True)
        self._sim = sim

        self._thread = threading.Thread(target=self._run, name="live-sim", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=60.0)

    def _run(self) -> None:
        try:
            sim = self._sim
            sim.start()
            # One composed call, not a warm-up followed by a live call: run()
            # finalises the simulation when it returns, so a separate warm-up
            # would end the run before the live phase ever started.
            self._notify_when_warm(sim)
            sim.run(
                hours=self._warmup_hours or 0.0,
                then_live=True,
                speed=self._speed,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced through status
            self._error = exc
            logger.exception("live simulation stopped")
        finally:
            self._ready.set()

    def _notify_when_warm(self, sim) -> None:
        """Release the caller once the fast-forward has produced some history.

        The API should not block for the whole warm-up before serving, but it
        should not serve an empty plant either, so readiness is signalled from a
        watcher rather than from the run itself.
        """
        if self._warmup_hours <= 0:
            self._ready.set()
            return

        target = sim.clock.start_time + timedelta(hours=self._warmup_hours * 0.9)

        def watch() -> None:
            while not self._ready.is_set():
                if sim.clock.now >= target:
                    self._ready.set()
                    return
                time.sleep(0.2)

        threading.Thread(target=watch, name="live-warmup", daemon=True).start()

    def stop(self) -> None:
        if self._sim is not None:
            self._sim.stop()
        if self._thread is not None:
            self._thread.join(timeout=15.0)
        if self._sim is not None:
            try:
                self._sim.finish()
            except Exception:  # pragma: no cover
                logger.exception("error finishing the live simulation")
            self._sim.close()

    # ------------------------------------------------------------------- access
    @property
    def simulator(self):
        return self._sim

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def error(self) -> str | None:
        return None if self._error is None else f"{type(self._error).__name__}: {self._error}"

    def status(self) -> dict[str, Any]:
        if self._sim is None:
            return {"live": False, "running": False, "error": self.error}
        status = self._sim.status()
        status["live"] = True
        status["running"] = self.running
        status["error"] = self.error
        status["hub"] = {
            "subscribers": self._hub.subscriber_count,
            "published": self._hub.published,
            "dropped": self._hub.dropped,
        }
        return status
