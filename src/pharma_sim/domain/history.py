"""Bounded sensor history for root-cause analysis.

RCA has to look back over days of telemetry, but keeping every sample in memory
is not viable: 100 machines x ~12 tags x 72 hours at a one-minute cadence is
millions of floats. Instead each tag keeps a deque of fixed-width time buckets
holding count/sum/sum-of-squares, which is enough to recover exactly the two
statistics the RCA rules use — the change in mean across the window, and the
change in variability — at a few hundred bytes per tag.

This is also why RCA is honest: it reads back this summary, not the simulator's
internal state.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

__all__ = ["SensorHistory", "WindowStats"]


@dataclass(slots=True)
class _Bucket:
    """Aggregated readings over one fixed time slice."""

    start: datetime
    count: int = 0
    total: float = 0.0
    total_sq: float = 0.0
    bad_count: int = 0

    def add(self, value: float, bad: bool) -> None:
        self.count += 1
        self.total += value
        self.total_sq += value * value
        if bad:
            self.bad_count += 1

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0

    @property
    def variance(self) -> float:
        if self.count < 2:
            return 0.0
        mean = self.mean
        return max(0.0, self.total_sq / self.count - mean * mean)


@dataclass(frozen=True, slots=True)
class WindowStats:
    """Summary of one tag over a lookback window, split into two halves."""

    tag: str
    samples: int
    first_half_mean: float
    second_half_mean: float
    first_half_std: float
    second_half_std: float
    bad_fraction: float

    @property
    def delta_fraction(self) -> float:
        """Relative change in mean between the halves of the window."""
        if self.first_half_mean == 0.0:
            return 0.0
        return (self.second_half_mean - self.first_half_mean) / abs(self.first_half_mean)

    @property
    def variance_ratio(self) -> float:
        """How much noisier the second half is than the first."""
        if self.first_half_std <= 1e-9:
            return 1.0 if self.second_half_std <= 1e-9 else 10.0
        return self.second_half_std / self.first_half_std


class SensorHistory:
    """Per-tag rolling buckets for one machine."""

    __slots__ = ("_buckets", "_bucket_minutes", "_max_buckets")

    def __init__(self, lookback_hours: float, bucket_minutes: float = 30.0) -> None:
        self._bucket_minutes = bucket_minutes
        # One extra bucket so a full window is always available even mid-bucket.
        self._max_buckets = max(4, int(math.ceil(lookback_hours * 60.0 / bucket_minutes)) + 1)
        self._buckets: dict[str, deque[_Bucket]] = {}

    def record(self, tag: str, at: datetime, value: float, quality: str) -> None:
        buckets = self._buckets.get(tag)
        if buckets is None:
            buckets = deque(maxlen=self._max_buckets)
            self._buckets[tag] = buckets

        start = self._bucket_start(at)
        if not buckets or buckets[-1].start != start:
            buckets.append(_Bucket(start=start))
        buckets[-1].add(value, quality == "BAD")

    def _bucket_start(self, at: datetime) -> datetime:
        minutes = int(self._bucket_minutes)
        total = at.hour * 60 + at.minute
        aligned = (total // minutes) * minutes
        return at.replace(hour=aligned // 60, minute=aligned % 60, second=0, microsecond=0)

    def tags(self) -> tuple[str, ...]:
        return tuple(self._buckets)

    def stats(self, tag: str, until: datetime, hours: float) -> WindowStats | None:
        """Two-half summary of ``tag`` over the ``hours`` ending at ``until``.

        ``until`` is bounded, not open-ended, and that matters: an investigation
        happens *after* the repair, so a window running to "now" would mix the
        pre-fault degradation with post-repair normal running and cancel the very
        trend the investigation is looking for.

        Returns ``None`` when there is too little data to say anything, which the
        RCA engine treats as absence of evidence rather than evidence of absence.
        """
        buckets = self._buckets.get(tag)
        if not buckets:
            return None
        cutoff = until - timedelta(hours=hours)
        window = [
            bucket
            for bucket in buckets
            if cutoff <= bucket.start <= until and bucket.count
        ]
        if len(window) < 2:
            return None

        midpoint = len(window) // 2
        first, second = window[:midpoint], window[midpoint:]
        if not first or not second:
            return None

        def summarise(group: list[_Bucket]) -> tuple[float, float, int]:
            count = sum(bucket.count for bucket in group)
            if count == 0:
                return 0.0, 0.0, 0
            total = sum(bucket.total for bucket in group)
            total_sq = sum(bucket.total_sq for bucket in group)
            mean = total / count
            variance = max(0.0, total_sq / count - mean * mean)
            return mean, math.sqrt(variance), count

        first_mean, first_std, first_count = summarise(first)
        second_mean, second_std, second_count = summarise(second)
        samples = first_count + second_count
        if samples == 0:
            return None
        bad = sum(bucket.bad_count for bucket in window)

        return WindowStats(
            tag=tag,
            samples=samples,
            first_half_mean=first_mean,
            second_half_mean=second_mean,
            first_half_std=first_std,
            second_half_std=second_std,
            bad_fraction=bad / samples,
        )

    def bad_fraction(self, until: datetime, hours: float) -> float:
        """Fraction of bad-quality readings across every tag in the window."""
        cutoff = until - timedelta(hours=hours)
        total = 0
        bad = 0
        for buckets in self._buckets.values():
            for bucket in buckets:
                if cutoff <= bucket.start <= until:
                    total += bucket.count
                    bad += bucket.bad_count
        return (bad / total) if total else 0.0
