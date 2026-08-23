"""MQTT sink — the OT-native transport.

Design points that matter in practice:

* **A missing broker is not fatal.** If the broker is unreachable the sink buffers
  up to a configured bound, keeps retrying in the background, and the simulation
  carries on. A synthetic-data generator that dies because a container is down
  would be useless.
* **The client is injectable.** ``client_factory`` lets the test suite drive a
  fake client and assert on published topics and payloads with no broker
  anywhere, which is what keeps the default test run Docker-free.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from typing import Any, Callable

from pharma_sim.config.models import MqttSinkOptions
from pharma_sim.streaming.base import SinkStats

__all__ = ["MqttSink"]

logger = logging.getLogger(__name__)


class MqttSink:
    """Publishes telemetry and events to per-machine MQTT topics."""

    def __init__(
        self,
        name: str,
        options: MqttSinkOptions,
        *,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._name = name
        self._options = options
        self._client_factory = client_factory
        self._client: Any | None = None
        self._stats = SinkStats(name=name)
        self._offline: deque[dict[str, Any]] = deque(maxlen=max(1, options.offline_buffer))

    @property
    def name(self) -> str:
        return self._name

    # ------------------------------------------------------------------ lifecycle
    def _default_client(self) -> Any:
        import paho.mqtt.client as mqtt

        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=self._options.client_id
        )
        if self._options.username:
            client.username_pw_set(self._options.username, self._options.password or None)
        # Last will, so a consumer can tell a crashed publisher from a quiet one.
        client.will_set(
            f"pharma/status/{self._options.client_id}", payload="offline", qos=1, retain=True
        )
        return client

    def open(self) -> None:
        factory = self._client_factory or self._default_client
        try:
            self._client = factory()
            self._client.connect(
                self._options.host, self._options.port, self._options.keepalive
            )
            if hasattr(self._client, "loop_start"):
                self._client.loop_start()
            self._stats.connected = True
            logger.info(
                "mqtt sink connected to %s:%s", self._options.host, self._options.port
            )
        except Exception as exc:
            # Buffer and retry rather than fail the run.
            self._stats.connected = False
            self._stats.errors += 1
            self._stats.last_error = str(exc)
            logger.warning(
                "mqtt broker %s:%s unreachable (%s); buffering up to %d messages",
                self._options.host,
                self._options.port,
                exc,
                self._offline.maxlen,
            )

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self.flush()
            if hasattr(self._client, "loop_stop"):
                self._client.loop_stop()
            self._client.disconnect()
        except Exception as exc:  # pragma: no cover - shutdown best effort
            self._stats.last_error = str(exc)
        finally:
            self._client = None
            self._stats.connected = False

    def _reconnect(self) -> bool:
        if self._client is None:
            self.open()
            return self._stats.connected
        try:
            self._client.reconnect()
            self._stats.connected = True
            self._stats.reconnects += 1
            return True
        except Exception as exc:
            self._stats.last_error = str(exc)
            return False

    # --------------------------------------------------------------------- writes
    def _topic_for(self, message: dict[str, Any]) -> str:
        template = (
            self._options.telemetry_topic
            if message.get("kind") == "telemetry"
            else self._options.event_topic
        )
        return template.format(
            plant_id=message.get("plant_id") or "unknown",
            unit_id=message.get("unit_id") or "unassigned",
            machine_id=message.get("machine_id") or "plant",
            sensor_id=message.get("sensor_id") or "none",
            event_type=message.get("event_type") or "telemetry",
        )

    def write(self, batch: list[dict[str, Any]]) -> None:
        if not batch:
            return
        if not self._stats.connected and not self._reconnect():
            self._buffer(batch)
            return

        pending = list(self._offline)
        self._offline.clear()
        pending.extend(batch)

        for message in pending:
            if not self._publish(message):
                # Connection lost mid-batch: keep the remainder for next time.
                index = pending.index(message)
                self._buffer(pending[index:])
                return
        self._stats.buffered = len(self._offline)

    def _publish(self, message: dict[str, Any]) -> bool:
        try:
            payload = json.dumps(message, default=str, separators=(",", ":"))
            self._client.publish(  # type: ignore[union-attr]
                self._topic_for(message),
                payload,
                qos=self._options.qos,
                retain=self._options.retain,
            )
            self._stats.sent += 1
            return True
        except Exception as exc:
            self._stats.errors += 1
            self._stats.last_error = str(exc)
            self._stats.connected = False
            return False

    def _buffer(self, messages: list[dict[str, Any]]) -> None:
        for message in messages:
            if len(self._offline) == self._offline.maxlen:
                self._stats.dropped += 1
            self._offline.append(message)
        self._stats.buffered = len(self._offline)

    def flush(self) -> None:
        if self._offline and self._stats.connected:
            pending = list(self._offline)
            self._offline.clear()
            for message in pending:
                if not self._publish(message):
                    self._buffer([message])
                    break
        self._stats.buffered = len(self._offline)

    def stats(self) -> SinkStats:
        self._stats.buffered = len(self._offline)
        return self._stats
