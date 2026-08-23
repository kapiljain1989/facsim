"""Deterministic random number streams.

Reproducibility is the property everything else in this simulator depends on, and
it is easy to lose. A single shared ``Random`` would make results depend on the
*order* in which machines happened to be visited, so adding one sensor would
change every other machine's history.

Instead each entity draws from its own named stream derived from the master seed.
``Random`` accepts a string seed and derives it stably (SHA-512 of the bytes), so
``sensor:TP-006:vibration`` yields the same sequence on every run regardless of
interleaving, thread scheduling, or how many other streams exist.
"""

from __future__ import annotations

import math
from random import Random

__all__ = ["RngRegistry", "truncated_normal"]


class RngRegistry:
    """Lazily-created, independently-seeded random streams keyed by name.

    Args:
        seed: master seed. Two registries with the same seed produce identical
            streams for identical names.
    """

    __slots__ = ("_seed", "_streams")

    def __init__(self, seed: int) -> None:
        self._seed = seed
        self._streams: dict[str, Random] = {}

    @property
    def seed(self) -> int:
        return self._seed

    def stream(self, name: str) -> Random:
        """Return the stream for ``name``, creating it on first use."""
        stream = self._streams.get(name)
        if stream is None:
            stream = Random(f"{self._seed}:{name}")
            self._streams[name] = stream
        return stream

    def child(self, *parts: object) -> Random:
        """Convenience for hierarchical names: ``child("sensor", machine, tag)``."""
        return self.stream(":".join(str(part) for part in parts))

    def reset(self) -> None:
        """Discard all streams so a re-run starts from the same state."""
        self._streams.clear()

    def __len__(self) -> int:
        return len(self._streams)


def truncated_normal(
    rng: Random,
    mean: float,
    sigma: float,
    low: float | None = None,
    high: float | None = None,
    max_attempts: int = 8,
) -> float:
    """Normal draw confined to ``[low, high]``.

    Retries a few times, then clamps. Clamping rather than looping forever keeps
    the number of RNG draws bounded, which matters: an unbounded loop would make
    the stream position depend on luck and break reproducibility guarantees for
    everything drawn afterwards.
    """
    if sigma <= 0.0:
        value = mean
    else:
        value = rng.gauss(mean, sigma)
        for _ in range(max_attempts - 1):
            if (low is None or value >= low) and (high is None or value <= high):
                break
            value = rng.gauss(mean, sigma)
    if low is not None:
        value = max(low, value)
    if high is not None:
        value = min(high, value)
    return value


def weibull_hazard(
    elapsed_hours: float, characteristic_hours: float, beta: float
) -> float:
    """Instantaneous Weibull hazard rate, per hour.

    ``beta > 1`` gives wear-out: the rate rises with accumulated operating hours.
    ``beta == 1`` reduces to a constant rate, the memoryless case used for modes
    like a grid interruption.
    """
    if characteristic_hours <= 0.0:
        return 0.0
    hours = max(elapsed_hours, 1e-6)
    if beta == 1.0:
        return 1.0 / characteristic_hours
    return (beta / characteristic_hours) * (hours / characteristic_hours) ** (beta - 1.0)


def probability_from_rate(rate_per_hour: float, hours: float) -> float:
    """Convert a hazard rate into ``P(event within hours)``."""
    if rate_per_hour <= 0.0 or hours <= 0.0:
        return 0.0
    return -math.expm1(-rate_per_hour * hours)
