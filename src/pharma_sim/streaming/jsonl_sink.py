"""Newline-delimited JSON sink.

The dependency-free path: point it at stdout and pipe the feed into anything.
Because it needs no broker, it is also what the tests use to assert on the live
stream's content.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, TextIO

from pharma_sim.config.models import JsonlSinkOptions
from pharma_sim.streaming.base import SinkStats

__all__ = ["JsonlSink"]

logger = logging.getLogger(__name__)


class JsonlSink:
    """Writes each message as one JSON line to stdout or a rotating file."""

    def __init__(self, name: str, options: JsonlSinkOptions) -> None:
        self._name = name
        self._options = options
        self._handle: TextIO | None = None
        self._owns_handle = False
        self._stats = SinkStats(name=name)
        self._bytes = 0
        self._rotation = 0

    @property
    def name(self) -> str:
        return self._name

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        if self._options.path == "-":
            self._handle = sys.stdout
            self._owns_handle = False
        else:
            path = Path(self._options.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("a", encoding="utf-8")
            self._owns_handle = True
            self._bytes = path.stat().st_size if path.exists() else 0
        self._stats.connected = True

    def close(self) -> None:
        self.flush()
        if self._handle is not None and self._owns_handle:
            self._handle.close()
        self._handle = None
        self._stats.connected = False

    def flush(self) -> None:
        if self._handle is not None:
            try:
                self._handle.flush()
            except (ValueError, OSError) as exc:  # stdout closed by a pipe
                self._stats.errors += 1
                self._stats.last_error = str(exc)

    # --------------------------------------------------------------------- writes
    def write(self, batch: list[dict[str, Any]]) -> None:
        if self._handle is None or not batch:
            return
        include_telemetry = self._options.include_telemetry
        include_events = self._options.include_events

        lines: list[str] = []
        for message in batch:
            kind = message.get("kind")
            if kind == "telemetry" and not include_telemetry:
                continue
            if kind == "event" and not include_events:
                continue
            lines.append(json.dumps(message, default=str, separators=(",", ":")))

        if not lines:
            return
        payload = "\n".join(lines) + "\n"
        try:
            self._handle.write(payload)
            self._stats.sent += len(lines)
            self._bytes += len(payload)
            self._maybe_rotate()
        except (BrokenPipeError, ValueError, OSError) as exc:
            # A consumer that hung up should not take the simulation down.
            self._stats.errors += 1
            self._stats.last_error = str(exc)
            self._stats.connected = False
            self._handle = None

    def _maybe_rotate(self) -> None:
        limit = self._options.rotate_mb
        if limit <= 0.0 or not self._owns_handle or self._handle is None:
            return
        if self._bytes < limit * 1024 * 1024:
            return
        self._handle.close()
        base = Path(self._options.path)
        self._rotation += 1
        base.rename(base.with_name(f"{base.stem}.{self._rotation}{base.suffix}"))
        self._handle = base.open("a", encoding="utf-8")
        self._bytes = 0
        self._stats.extra["rotations"] = self._rotation

    def stats(self) -> SinkStats:
        return self._stats
