"""Structured logging.

Log records carry the *simulated* instant alongside the real one. Without it a
log line from a 30-day fast-forward is unreadable: everything happens within the
same real second, so real timestamps say nothing about when in the plant's
history the event occurred.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from typing import Any, Callable

__all__ = ["configure_logging", "SimTimeFilter", "set_sim_time_source"]

_sim_time_source: Callable[[], datetime | None] = lambda: None


def set_sim_time_source(source: Callable[[], datetime | None]) -> None:
    """Register the clock so log records can report simulated time."""
    global _sim_time_source
    _sim_time_source = source


class SimTimeFilter(logging.Filter):
    """Attaches ``sim_time`` to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        moment = _sim_time_source()
        record.sim_time = moment.isoformat(timespec="seconds") if moment else "-"
        return True


class KeyValueFormatter(logging.Formatter):
    """Human-readable, grep-friendly key=value output."""

    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record, '%H:%M:%S')} "
            f"sim={getattr(record, 'sim_time', '-')} "
            f"{record.levelname:<7} {record.name.split('.')[-1]:<16} "
            f"{record.getMessage()}"
        )
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for ingestion by a log pipeline."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "sim_time": getattr(record, "sim_time", None),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in logging.LogRecord("", 0, "", 0, "", (), None).__dict__ and key not in {
                "sim_time",
                "message",
                "asctime",
            }:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(*, level: str = "INFO", json_format: bool = False) -> None:
    """Install a single stderr handler with the simulated-time filter.

    Logs go to stderr so ``--sink jsonl`` can own stdout and be piped cleanly
    into a consumer.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(SimTimeFilter())
    handler.setFormatter(JsonFormatter() if json_format else KeyValueFormatter())
    root.addHandler(handler)

    # Third-party chatter would otherwise drown the simulation's own narrative.
    logging.getLogger("paho").setLevel(logging.WARNING)
