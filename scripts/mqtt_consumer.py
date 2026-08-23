#!/usr/bin/env python
"""Subscribe to the simulator's MQTT feed and report what arrives.

Proves the live stream end to end from a consumer's point of view, which is the
only way to know the feed actually works rather than that the publisher believes
it does.

    docker compose up -d mosquitto
    python scripts/mqtt_consumer.py &
    python -m pharma_sim run --live --speed 60 --sink mqtt
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from collections import Counter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--topic", default="pharma/#")
    parser.add_argument("--seconds", type=float, default=30.0, help="how long to listen")
    parser.add_argument("--show", type=int, default=5, help="sample messages to print")
    args = parser.parse_args()

    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("paho-mqtt is not installed", file=sys.stderr)
        return 2

    counts: Counter[str] = Counter()
    machines: set[str] = set()
    tags: set[str] = set()
    shown = 0
    first_at: float | None = None
    last_at: float | None = None

    def on_connect(client, userdata, flags, reason_code, properties=None):
        print(f"connected to {args.host}:{args.port}, subscribing to {args.topic}")
        client.subscribe(args.topic)

    def on_message(client, userdata, message):
        nonlocal shown, first_at, last_at
        now = time.monotonic()
        if first_at is None:
            first_at = now
        last_at = now
        try:
            payload = json.loads(message.payload)
        except json.JSONDecodeError:
            # The publisher's last-will is a plain-text liveness marker, not a
            # data message, so it is expected to be non-JSON.
            counts["status" if "/status/" in message.topic else "unparseable"] += 1
            return

        kind = payload.get("kind", "unknown")
        counts[kind] += 1
        if payload.get("machine_id"):
            machines.add(payload["machine_id"])
        if payload.get("tag"):
            tags.add(payload["tag"])
        if kind == "event":
            counts[f"event:{payload.get('event_type')}"] += 1

        if shown < args.show:
            shown += 1
            print(f"\n[{message.topic}]")
            print(json.dumps(payload, indent=2)[:600])

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="pharma-consumer")
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(args.host, args.port, 60)
    except OSError as exc:
        print(
            f"could not reach the broker at {args.host}:{args.port} ({exc}).\n"
            f"Start one with:  docker compose up -d mosquitto",
            file=sys.stderr,
        )
        return 1

    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        del signum, frame
        stopping = True

    signal.signal(signal.SIGINT, stop)
    client.loop_start()

    deadline = time.monotonic() + args.seconds
    while not stopping and time.monotonic() < deadline:
        time.sleep(0.2)
    client.loop_stop()
    client.disconnect()

    total = sum(
        count
        for key, count in counts.items()
        if not key.startswith("event:") and key != "status"
    )
    print("\n" + "=" * 60)
    print(f"received {total:,} messages from {len(machines)} machine(s)")
    for kind in ("telemetry", "event", "status", "unparseable"):
        if counts[kind]:
            print(f"  {kind:<12} {counts[kind]:>8,}")
    if first_at and last_at and last_at > first_at:
        print(f"  rate         {total / (last_at - first_at):>8.1f} msg/s")
    if tags:
        print(f"  distinct tags {len(tags)}: {', '.join(sorted(tags)[:10])}")
    event_types = {
        key.split(":", 1)[1]: count
        for key, count in counts.items()
        if key.startswith("event:")
    }
    if event_types:
        print("  event types:")
        for name, count in sorted(event_types.items(), key=lambda kv: -kv[1])[:10]:
            print(f"    {name:<28} {count:>6,}")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
