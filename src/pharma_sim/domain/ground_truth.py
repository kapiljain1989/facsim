"""Hidden ground truth and forward-looking prediction labels.

This is the evaluation dataset, and it is deliberately kept out of the
operational one. The information boundary is:

* **Operational data** records what a plant would actually know at the time — a
  machine tripped, these alarms were active, this category of fault, this repair
  was done, these QC results failed. An RCA record here is a *conclusion*, and it
  can be wrong.
* **Ground truth** records what the simulator knows — which failure mode was
  really developing, its true root cause, when it began, and whether maintenance
  averted it.

Mixing the two would silently invalidate any evaluation done with this data, so
they are written to a separate store and a test asserts the separation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

__all__ = [
    "GroundTruthEvent",
    "PredictionLabel",
    "GroundTruthLedger",
    "NO_EPISODE",
]

#: Marks a label row that belongs to no degradation episode. A sentinel
#: rather than NULL because the label key must stay unique, and a NULL
#: cannot participate in a primary key.
NO_EPISODE = "NO_EPISODE"


@dataclass(slots=True)
class GroundTruthEvent:
    """The true story behind one degradation episode."""

    ground_truth_id: str
    episode_id: str
    failure_id: str | None
    machine_id: str
    unit_id: str
    equipment_class: str
    failure_mode: str
    failure_category: str
    root_cause: str
    root_cause_description: str
    onset_at: datetime
    scheduled_fault_at: datetime
    faulted_at: datetime | None
    warned_at: datetime | None
    averted_at: datetime | None
    resolved_at: datetime | None
    incubation_hours: float
    detectable: bool
    injected: bool
    precursor_tags: tuple[str, ...]
    severity: str
    affected_batches: list[str] = field(default_factory=list)
    affected_qc_failures: list[str] = field(default_factory=list)
    production_loss_units: float = 0.0
    downtime_minutes: float = 0.0
    run_id: str = ""

    @property
    def outcome(self) -> str:
        if self.averted_at is not None:
            return "AVERTED"
        if self.faulted_at is not None:
            return "FAULTED"
        return "IN_PROGRESS"

    def as_row(self) -> dict[str, Any]:
        return {
            "ground_truth_id": self.ground_truth_id,
            "episode_id": self.episode_id,
            "failure_id": self.failure_id,
            "machine_id": self.machine_id,
            "unit_id": self.unit_id,
            "equipment_class": self.equipment_class,
            "failure_mode": self.failure_mode,
            "failure_category": self.failure_category,
            "root_cause": self.root_cause,
            "root_cause_description": self.root_cause_description,
            "onset_at": self.onset_at,
            "scheduled_fault_at": self.scheduled_fault_at,
            "faulted_at": self.faulted_at,
            "warned_at": self.warned_at,
            "averted_at": self.averted_at,
            "resolved_at": self.resolved_at,
            "incubation_hours": round(self.incubation_hours, 3),
            "detectable": self.detectable,
            "injected": self.injected,
            "precursor_tags": ",".join(self.precursor_tags),
            "severity": self.severity,
            "outcome": self.outcome,
            "affected_batches": ",".join(self.affected_batches),
            "affected_qc_failures": ",".join(self.affected_qc_failures),
            "production_loss_units": round(self.production_loss_units, 2),
            "downtime_minutes": round(self.downtime_minutes, 2),
            "run_id": self.run_id,
        }


@dataclass(slots=True)
class PredictionLabel:
    """A forward-looking label for one machine at one instant.

    ``rul_hours`` is exact rather than estimated, because the fault instant was
    fixed when the episode began. ``averted`` distinguishes an episode that was
    caught by maintenance from one that ran to failure — without it the labels
    would assert outcomes that never happened.
    """

    machine_id: str
    unit_id: str
    equipment_class: str
    timestamp: datetime
    health_index: float
    degrading: bool
    failure_mode: str | None
    failure_category: str | None
    root_cause: str | None
    episode_id: str
    rul_hours: float | None
    will_fail_24h: bool
    will_fail_72h: bool
    will_fail_168h: bool
    degradation_stage: str
    averted: bool
    detectable: bool
    run_id: str

    def as_row(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "unit_id": self.unit_id,
            "equipment_class": self.equipment_class,
            "timestamp": self.timestamp,
            "health_index": round(self.health_index, 5),
            "degrading": self.degrading,
            "failure_mode": self.failure_mode,
            "failure_category": self.failure_category,
            "root_cause": self.root_cause,
            "episode_id": self.episode_id,
            "rul_hours": None if self.rul_hours is None else round(self.rul_hours, 3),
            "will_fail_24h": self.will_fail_24h,
            "will_fail_72h": self.will_fail_72h,
            "will_fail_168h": self.will_fail_168h,
            "degradation_stage": self.degradation_stage,
            "averted": self.averted,
            "detectable": self.detectable,
            "run_id": self.run_id,
        }


def degradation_stage_for(health: float) -> str:
    """Coarse stage label, useful as a multi-class target."""
    if health <= 0.0:
        return "HEALTHY"
    if health < 0.25:
        return "EARLY"
    if health < 0.55:
        return "DEVELOPING"
    if health < 0.85:
        return "ADVANCED"
    return "IMMINENT"


class GroundTruthLedger:
    """Holds ground-truth events and generates the label series from them."""

    def __init__(self, run_id: str, label_interval_minutes: float) -> None:
        self._run_id = run_id
        self._interval = timedelta(minutes=label_interval_minutes)
        self._events: dict[str, GroundTruthEvent] = {}

    def record(self, event: GroundTruthEvent) -> None:
        self._events[event.episode_id] = event

    def get(self, episode_id: str) -> GroundTruthEvent | None:
        return self._events.get(episode_id)

    def all(self) -> list[GroundTruthEvent]:
        return list(self._events.values())

    def by_failure(self, failure_id: str) -> GroundTruthEvent | None:
        for event in self._events.values():
            if event.failure_id == failure_id:
                return event
        return None

    def __len__(self) -> int:
        return len(self._events)

    # ------------------------------------------------------------------- labels
    def labels_for_episode(
        self, event: GroundTruthEvent, *, until: datetime
    ) -> list[PredictionLabel]:
        """Label the precursor window of one episode at the label cadence.

        Emitted when the episode closes, so the outcome — faulted or averted — is
        already known and every row can state it correctly.
        """
        from pharma_sim.registry.failures import degradation_curve

        end = event.faulted_at or event.averted_at or event.resolved_at or until
        if end <= event.onset_at:
            return []

        labels: list[PredictionLabel] = []
        total = (event.scheduled_fault_at - event.onset_at).total_seconds()
        moment = event.onset_at
        averted = event.averted_at is not None

        while moment <= end:
            remaining = (event.scheduled_fault_at - moment).total_seconds() / 3600.0
            progress = (
                0.0 if total <= 0 else min(1.0, (moment - event.onset_at).total_seconds() / total)
            )
            health = degradation_curve(progress, "exponential")
            labels.append(
                PredictionLabel(
                    machine_id=event.machine_id,
                    unit_id=event.unit_id,
                    equipment_class=event.equipment_class,
                    timestamp=moment,
                    health_index=health,
                    degrading=True,
                    failure_mode=event.failure_mode,
                    failure_category=event.failure_category,
                    root_cause=event.root_cause,
                    episode_id=event.episode_id,
                    rul_hours=max(0.0, remaining),
                    # A failure that maintenance averted did not happen, so the
                    # horizon flags must say so.
                    will_fail_24h=(not averted) and 0.0 <= remaining <= 24.0,
                    will_fail_72h=(not averted) and 0.0 <= remaining <= 72.0,
                    will_fail_168h=(not averted) and 0.0 <= remaining <= 168.0,
                    degradation_stage=degradation_stage_for(health),
                    averted=averted,
                    detectable=event.detectable,
                    run_id=self._run_id,
                )
            )
            moment = moment + self._interval
        return labels

    def healthy_label(
        self,
        *,
        machine_id: str,
        unit_id: str,
        equipment_class: str,
        timestamp: datetime,
    ) -> PredictionLabel:
        """A negative example: no episode developing on this machine."""
        return PredictionLabel(
            machine_id=machine_id,
            unit_id=unit_id,
            equipment_class=equipment_class,
            timestamp=timestamp,
            health_index=0.0,
            degrading=False,
            failure_mode=None,
            failure_category=None,
            root_cause=None,
            episode_id=NO_EPISODE,
            rul_hours=None,
            will_fail_24h=False,
            will_fail_72h=False,
            will_fail_168h=False,
            degradation_stage="HEALTHY",
            averted=False,
            detectable=False,
            run_id=self._run_id,
        )
