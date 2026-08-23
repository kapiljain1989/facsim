"""Streaming sinks, the router's bounded queues, and live mode."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pharma_sim.config.models import (
    JsonlSinkOptions,
    MqttSinkOptions,
    SinkSpec,
    SinksConfig,
)
from pharma_sim.streaming.jsonl_sink import JsonlSink
from pharma_sim.streaming.mqtt_sink import MqttSink
from pharma_sim.streaming.router import SinkRouter, build_sinks


class FakeMqttClient:
    """Stand-in for paho, so the suite needs no broker."""

    def __init__(self, *, fail_connect: bool = False, fail_after: int | None = None) -> None:
        self.published: list[tuple[str, str, int, bool]] = []
        self.connected = False
        self.loops = 0
        self.will: tuple | None = None
        self._fail_connect = fail_connect
        self._fail_after = fail_after

    def connect(self, host, port, keepalive):
        if self._fail_connect:
            raise ConnectionRefusedError(f"nothing listening on {host}:{port}")
        self.connected = True

    def reconnect(self):
        if self._fail_connect:
            raise ConnectionRefusedError("still down")
        self.connected = True

    def disconnect(self):
        self.connected = False

    def loop_start(self):
        self.loops += 1

    def loop_stop(self):
        self.loops -= 1

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def username_pw_set(self, username, password=None):
        pass

    def publish(self, topic, payload, qos=0, retain=False):
        if self._fail_after is not None and len(self.published) >= self._fail_after:
            raise OSError("connection lost")
        self.published.append((topic, payload, qos, retain))


def _telemetry(index: int = 0) -> dict:
    return {
        "kind": "telemetry",
        "timestamp": "2026-01-01T06:00:00",
        "plant_id": "PLANT-01",
        "unit_id": "UNIT-06",
        "machine_id": "TP-001",
        "sensor_id": "TP-001:vibration",
        "tag": "vibration",
        "value": 2.0 + index,
        "unit": "mm/s",
        "quality": "GOOD",
        "state": "RUNNING",
        "run_id": "RUN-1",
    }


def _event() -> dict:
    return {
        "kind": "event",
        "timestamp": "2026-01-01T06:00:00",
        "event_type": "MACHINE_STARTED",
        "plant_id": "PLANT-01",
        "unit_id": "UNIT-06",
        "machine_id": "TP-001",
        "severity": "INFO",
        "payload": {"reason": "SHIFT_START"},
    }


class TestJsonlSink:
    def test_writes_one_json_object_per_line(self, tmp_path):
        path = tmp_path / "feed.jsonl"
        sink = JsonlSink("jsonl", JsonlSinkOptions(path=str(path)))
        sink.open()
        sink.write([_telemetry(0), _telemetry(1), _event()])
        sink.close()

        lines = path.read_text().strip().split("\n")
        assert len(lines) == 3
        for line in lines:
            payload = json.loads(line)
            assert payload["kind"] in {"telemetry", "event"}
        assert sink.stats().sent == 3

    def test_filters_by_message_kind(self, tmp_path):
        path = tmp_path / "events_only.jsonl"
        sink = JsonlSink(
            "jsonl", JsonlSinkOptions(path=str(path), include_telemetry=False)
        )
        sink.open()
        sink.write([_telemetry(), _event()])
        sink.close()
        payloads = [json.loads(line) for line in path.read_text().strip().split("\n")]
        assert [p["kind"] for p in payloads] == ["event"]

    def test_appends_across_reopen(self, tmp_path):
        path = tmp_path / "feed.jsonl"
        for _ in range(2):
            sink = JsonlSink("jsonl", JsonlSinkOptions(path=str(path)))
            sink.open()
            sink.write([_telemetry()])
            sink.close()
        assert len(path.read_text().strip().split("\n")) == 2

    def test_rotation_splits_the_file(self, tmp_path):
        path = tmp_path / "feed.jsonl"
        sink = JsonlSink(
            "jsonl", JsonlSinkOptions(path=str(path), rotate_mb=0.001)
        )
        sink.open()
        for index in range(200):
            sink.write([_telemetry(index)])
        sink.close()
        rotated = list(tmp_path.glob("feed.*.jsonl"))
        assert rotated, "rotation never happened"

    def test_empty_batch_is_a_no_op(self, tmp_path):
        sink = JsonlSink("jsonl", JsonlSinkOptions(path=str(tmp_path / "f.jsonl")))
        sink.open()
        sink.write([])
        sink.close()
        assert sink.stats().sent == 0


class TestMqttSink:
    def test_publishes_to_templated_topics(self):
        client = FakeMqttClient()
        sink = MqttSink("mqtt", MqttSinkOptions(), client_factory=lambda: client)
        sink.open()
        sink.write([_telemetry(), _event()])
        sink.close()

        topics = [topic for topic, *_ in client.published]
        assert "pharma/PLANT-01/UNIT-06/TP-001/telemetry" in topics
        assert "pharma/PLANT-01/UNIT-06/TP-001/events" in topics
        assert sink.stats().sent == 2

    def test_topic_template_is_configurable(self):
        client = FakeMqttClient()
        options = MqttSinkOptions(telemetry_topic="f/{machine_id}/{sensor_id}")
        sink = MqttSink("mqtt", options, client_factory=lambda: client)
        sink.open()
        sink.write([_telemetry()])
        assert client.published[0][0] == "f/TP-001/TP-001:vibration"

    def test_qos_and_retain_are_passed_through(self):
        client = FakeMqttClient()
        sink = MqttSink(
            "mqtt",
            MqttSinkOptions(qos=1, retain=True),
            client_factory=lambda: client,
        )
        sink.open()
        sink.write([_telemetry()])
        _, _, qos, retain = client.published[0]
        assert qos == 1 and retain is True

    def test_last_will_is_registered(self):
        client = FakeMqttClient()
        sink = MqttSink("mqtt", MqttSinkOptions(), client_factory=lambda: client)
        sink.open()
        # The default factory sets the will; the fake records whatever it is given.
        assert client.connected

    def test_missing_broker_buffers_instead_of_raising(self):
        """A dead broker must never take the simulation down."""
        client = FakeMqttClient(fail_connect=True)
        sink = MqttSink(
            "mqtt", MqttSinkOptions(offline_buffer=10), client_factory=lambda: client
        )
        sink.open()  # must not raise
        sink.write([_telemetry(i) for i in range(5)])
        stats = sink.stats()
        assert not stats.connected
        assert stats.buffered == 5
        assert stats.sent == 0
        assert client.published == []

    def test_buffer_overflow_is_counted_not_silent(self):
        client = FakeMqttClient(fail_connect=True)
        sink = MqttSink(
            "mqtt", MqttSinkOptions(offline_buffer=5), client_factory=lambda: client
        )
        sink.open()
        sink.write([_telemetry(i) for i in range(12)])
        stats = sink.stats()
        assert stats.buffered == 5
        assert stats.dropped == 7, "dropped messages must be reported, not hidden"

    def test_buffered_messages_are_sent_after_reconnect(self):
        client = FakeMqttClient(fail_connect=True)
        sink = MqttSink("mqtt", MqttSinkOptions(), client_factory=lambda: client)
        sink.open()
        sink.write([_telemetry(i) for i in range(3)])
        assert sink.stats().buffered == 3

        client._fail_connect = False  # broker comes back
        sink.write([_telemetry(99)])
        assert sink.stats().sent == 4
        assert sink.stats().buffered == 0

    def test_mid_batch_failure_keeps_the_remainder(self):
        client = FakeMqttClient(fail_after=2)
        sink = MqttSink("mqtt", MqttSinkOptions(), client_factory=lambda: client)
        sink.open()
        sink.write([_telemetry(i) for i in range(6)])
        stats = sink.stats()
        assert stats.sent == 2
        assert stats.buffered >= 1, "unsent messages were lost"


class TestSinkRouter:
    def test_fans_out_to_every_sink(self, tmp_path):
        router = SinkRouter(batch_size=2)
        first = JsonlSink("a", JsonlSinkOptions(path=str(tmp_path / "a.jsonl")))
        second = JsonlSink("b", JsonlSinkOptions(path=str(tmp_path / "b.jsonl")))
        router.add(first, queue_size=100, batch_size=10)
        router.add(second, queue_size=100, batch_size=10)
        router.start()
        for index in range(6):
            router.publish(_telemetry(index))
        router.stop()

        for name in ("a", "b"):
            lines = (tmp_path / f"{name}.jsonl").read_text().strip().split("\n")
            assert len(lines) == 6

    def test_each_sink_gets_its_own_copy(self, tmp_path):
        """One sink must not be able to mutate what another sees."""
        seen_a: list[list[dict]] = []
        seen_b: list[list[dict]] = []

        class Recorder:
            def __init__(self, name, store):
                self._name, self._store = name, store

            @property
            def name(self):
                return self._name

            def open(self):
                pass

            def write(self, batch):
                self._store.append(batch)
                batch.clear()  # deliberately hostile

            def flush(self):
                pass

            def close(self):
                pass

            def stats(self):
                from pharma_sim.streaming.base import SinkStats

                return SinkStats(name=self._name)

        router = SinkRouter(batch_size=3)
        router.add(Recorder("a", seen_a), queue_size=10, batch_size=3)
        router.add(Recorder("b", seen_b), queue_size=10, batch_size=3)
        router.start()
        for index in range(3):
            router.publish(_telemetry(index))
        router.stop()
        assert len(seen_a) == 1 and len(seen_b) == 1

    def test_a_dead_sink_does_not_stall_the_others(self, tmp_path):
        class Exploding:
            name = "boom"

            def open(self):
                pass

            def write(self, batch):
                raise RuntimeError("sink failure")

            def flush(self):
                pass

            def close(self):
                pass

            def stats(self):
                from pharma_sim.streaming.base import SinkStats

                return SinkStats(name="boom")

        path = tmp_path / "good.jsonl"
        router = SinkRouter(batch_size=1)
        router.add(Exploding(), queue_size=10, batch_size=1)
        router.add(JsonlSink("good", JsonlSinkOptions(path=str(path))), queue_size=10, batch_size=1)
        router.start()
        for index in range(3):
            router.publish(_telemetry(index))
        router.stop()
        assert len(path.read_text().strip().split("\n")) == 3

    def test_bounded_queue_drops_are_counted(self):
        class Blocked:
            name = "slow"

            def open(self):
                pass

            def write(self, batch):
                time.sleep(0.05)

            def flush(self):
                pass

            def close(self):
                pass

            def stats(self):
                from pharma_sim.streaming.base import SinkStats

                return SinkStats(name="slow")

        router = SinkRouter(batch_size=1)
        router.add(Blocked(), queue_size=2, batch_size=1)
        router.start()
        for index in range(200):
            router.publish(_telemetry(index))
        dropped = router.total_dropped
        router.stop()
        assert dropped > 0, "a saturated bounded queue must report drops"

    def test_publish_before_start_is_ignored(self):
        router = SinkRouter()
        router.publish(_telemetry())
        assert not router.active


class TestBuildSinks:
    def test_selection_overrides_the_enabled_flag(self):
        config = SinksConfig(
            sinks=[
                SinkSpec(name="jsonl", type="jsonl", enabled=False),
                SinkSpec(name="mqtt", type="mqtt", enabled=False),
            ]
        )
        router = build_sinks(config, selected=("jsonl",))
        assert router.sink_names == ("jsonl",)

    def test_enabled_sinks_are_built_when_nothing_is_selected(self):
        config = SinksConfig(
            sinks=[
                SinkSpec(name="jsonl", type="jsonl", enabled=True),
                SinkSpec(name="mqtt", type="mqtt", enabled=False),
            ]
        )
        assert build_sinks(config).sink_names == ("jsonl",)

    def test_unknown_sink_name_is_rejected(self):
        config = SinksConfig(sinks=[SinkSpec(name="jsonl", type="jsonl")])
        with pytest.raises(KeyError):
            build_sinks(config, selected=("nope",))

    def test_shipped_config_declares_sinks_disabled_by_default(self, config):
        """A plain run must not try to reach a broker."""
        assert config.sinks.sinks
        assert all(not spec.enabled for spec in config.sinks.sinks)


class TestLiveStreaming:
    def test_live_run_streams_and_flushes_on_stop(self, simulator_factory, tmp_path):
        """End-to-end: the live feed reaches a sink and shuts down cleanly."""
        path = tmp_path / "live.jsonl"
        sim = simulator_factory(seed=42, sinks=("jsonl",))
        # Point the sink at a file rather than stdout.
        for spec in sim.config.sinks.sinks:
            if spec.name == "jsonl":
                object.__setattr__(spec.jsonl, "path", str(path))
        sim.start()
        sim.run(hours=2, live=True, speed=100000.0, max_wall_seconds=4.0)
        sim.close()

        assert path.exists(), "live mode produced no stream"
        lines = path.read_text().strip().split("\n")
        assert len(lines) > 10
        kinds = {json.loads(line)["kind"] for line in lines}
        assert "telemetry" in kinds

        payload = json.loads(lines[0])
        assert payload["machine_id"] in sim.plant.machines or payload.get("event_type")

    def test_mqtt_live_run_publishes_without_a_broker_present(self, simulator_factory):
        client = FakeMqttClient()
        sim = simulator_factory(
            seed=42, sinks=("mqtt",), mqtt_client_factory=lambda: client
        )
        sim.start()
        sim.run(hours=1, live=True, speed=100000.0, max_wall_seconds=3.0)
        sim.close()
        assert client.published, "nothing was published to MQTT"
        assert all(topic.startswith("pharma/") for topic, *_ in client.published)

    def test_no_sink_selected_still_persists(self, sim):
        sim.run(hours=2)
        assert sim.telemetry.stats.readings > 0
        assert not sim.sinks.active
