"""Deterministic identifier generation.

``uuid4`` would break reproducibility, so every id in the simulator comes from a
monotonic counter here. The factory is injected rather than global, which keeps
two simulators in one process from interfering — something the test suite relies
on when it runs the same seed twice and compares digests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["IdFactory"]


@dataclass
class IdFactory:
    """Issues zero-padded, prefixed, monotonically increasing identifiers.

    Args:
        year: used by sequences whose id embeds a year, such as batch numbers.
    """

    year: int = 2026
    _counters: dict[str, int] = field(default_factory=dict, repr=False)

    def next(self, prefix: str, width: int = 6) -> str:
        """Return the next id for ``prefix``, e.g. ``EVT-000042``."""
        value = self._counters.get(prefix, 0) + 1
        self._counters[prefix] = value
        return f"{prefix}-{value:0{width}d}"

    def next_dated(self, prefix: str, width: int = 6) -> str:
        """Return a year-scoped id, e.g. ``BATCH-2026-000042``."""
        key = f"{prefix}:{self.year}"
        value = self._counters.get(key, 0) + 1
        self._counters[key] = value
        return f"{prefix}-{self.year}-{value:0{width}d}"

    def count(self, prefix: str) -> int:
        """How many ids have been issued for ``prefix``."""
        return self._counters.get(prefix, 0)

    def reset(self) -> None:
        self._counters.clear()

    # Convenience accessors, so call sites read as domain language rather than
    # as string literals scattered through the engine.
    def event(self) -> str:
        return self.next("EVT", width=9)

    def run(self) -> str:
        return self.next("RUN", width=4)

    def batch(self) -> str:
        return self.next_dated("BATCH")

    def order(self) -> str:
        return self.next_dated("ORD")

    def failure(self) -> str:
        return self.next("FAIL", width=5)

    def maintenance(self) -> str:
        return self.next("MNT", width=5)

    def deviation(self) -> str:
        return self.next("DEV", width=5)

    def rca(self) -> str:
        return self.next("RCA", width=5)

    def capa(self) -> str:
        return self.next("CAPA", width=5)

    def qc_test(self) -> str:
        return self.next("QC", width=8)

    def shift_instance(self) -> str:
        return self.next("SHF", width=6)

    def ground_truth(self) -> str:
        return self.next("GT", width=6)
