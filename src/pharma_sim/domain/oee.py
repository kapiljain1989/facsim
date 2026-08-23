"""OEE calculation.

Availability, Performance and Quality are computed from the time buckets and
counters a machine accumulates. Which states count as productive, as unplanned
downtime, or as a planned stop comes from the state registry's roles, so the
calculation survives a change to the state model.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from pharma_sim.domain.machine import ProductionWindow

__all__ = ["Oee", "compute_oee", "aggregate_windows"]


@dataclass(frozen=True, slots=True)
class Oee:
    """An OEE result and the components behind it."""

    availability: float
    performance: float
    quality: float
    oee: float
    #: Runtime as a share of *all* elapsed time. Reported separately from
    #: availability on purpose: a plant can be highly available on the work it
    #: has and still be lightly loaded, and conflating the two hides which.
    utilisation: float
    loading_seconds: float
    runtime_seconds: float
    downtime_seconds: float
    unscheduled_seconds: float
    good_quantity: float
    actual_quantity: float
    planned_quantity: float

    def as_row(self) -> dict[str, Any]:
        return {
            "availability": round(self.availability, 5),
            "performance": round(self.performance, 5),
            "quality": round(self.quality, 5),
            "oee": round(self.oee, 5),
            "utilisation": round(self.utilisation, 5),
            "loading_seconds": round(self.loading_seconds, 1),
            "runtime_seconds": round(self.runtime_seconds, 1),
            "downtime_seconds": round(self.downtime_seconds, 1),
            "unscheduled_seconds": round(self.unscheduled_seconds, 1),
            "good_quantity": round(self.good_quantity, 2),
            "actual_quantity": round(self.actual_quantity, 2),
            "planned_quantity": round(self.planned_quantity, 2),
        }


def compute_oee(window: ProductionWindow) -> Oee:
    """Derive OEE from one accumulated window.

    Loading time excludes planned stops and offline periods, so cleaning and
    changeover do not count against availability — the standard treatment, and
    the reason the state model declares a ``planned_stop`` role at all.
    """
    # Loading time is the time the machine was expected to produce: running,
    # broken, or waiting on work it already had. Planned stops and time with
    # nothing assigned are excluded, which is the standard treatment.
    loading = window.runtime_seconds + window.downtime_seconds + window.idle_seconds
    availability = (window.runtime_seconds / loading) if loading > 0.0 else 0.0

    elapsed = loading + window.planned_stop_seconds + window.offline_seconds + (
        window.unscheduled_seconds
    )
    utilisation = (window.runtime_seconds / elapsed) if elapsed > 0.0 else 0.0

    # Performance compares what was produced against what the nominal rate would
    # have produced in the same running time.
    performance = (
        (window.actual_quantity / window.planned_quantity)
        if window.planned_quantity > 0.0
        else 0.0
    )
    performance = min(performance, 1.0)

    quality = (
        (window.good_quantity / window.actual_quantity)
        if window.actual_quantity > 0.0
        else 0.0
    )

    return Oee(
        availability=availability,
        performance=performance,
        quality=quality,
        oee=availability * performance * quality,
        utilisation=utilisation,
        loading_seconds=loading,
        runtime_seconds=window.runtime_seconds,
        downtime_seconds=window.downtime_seconds,
        unscheduled_seconds=window.unscheduled_seconds,
        good_quantity=window.good_quantity,
        actual_quantity=window.actual_quantity,
        planned_quantity=window.planned_quantity,
    )


def aggregate_windows(windows: Iterable[ProductionWindow]) -> ProductionWindow:
    """Sum windows so unit, plant, shift and batch OEE reuse one calculation."""
    total = ProductionWindow()
    for window in windows:
        total.planned_quantity += window.planned_quantity
        total.actual_quantity += window.actual_quantity
        total.good_quantity += window.good_quantity
        total.reject_quantity += window.reject_quantity
        total.scrap_quantity += window.scrap_quantity
        total.runtime_seconds += window.runtime_seconds
        total.idle_seconds += window.idle_seconds
        total.downtime_seconds += window.downtime_seconds
        total.planned_stop_seconds += window.planned_stop_seconds
        total.offline_seconds += window.offline_seconds
        total.unscheduled_seconds += window.unscheduled_seconds
        total.energy_kwh += window.energy_kwh
        total.cycle_count += window.cycle_count
    return total
