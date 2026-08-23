"""Deviation, RCA and CAPA — the quality-management chain.

The RCA engine is the interesting part. It works only from what the operational
dataset records: the summarised sensor window before the event, alarms, machine
state history, maintenance history, operator assignment, process parameters and
QC results. It is told the failure's *category* — a real maintenance report would
state the observed symptom class — but never the failure mode or the true root
cause, which live in the ground-truth store.

It is therefore fallible by design. Overlapping evidence between, say, a blocked
filter and a loaded HEPA bank is intentional: a diagnostic engine that is right
by construction tells you nothing about whether your own diagnostic system works.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pharma_sim.config.models import RcaEvidenceRule, RcaRuleSpec
from pharma_sim.domain.batch import Batch
from pharma_sim.domain.history import SensorHistory
from pharma_sim.engine.context import SimContext

__all__ = [
    "Deviation",
    "RcaReport",
    "RcaEvidenceItem",
    "Capa",
    "DeviationManager",
    "RcaEngine",
    "CapaManager",
]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Deviation:
    """A quality deviation opened by a triggering event (§22)."""

    deviation_id: str
    rule_id: str
    title: str
    severity: str
    status: str
    detected_at: datetime
    plant_id: str
    unit_id: str | None
    machine_id: str | None
    batch_id: str | None
    trigger_event: str
    trigger_event_id: str
    failure_id: str | None
    description: str
    requires_rca: bool
    requires_capa: bool
    closed_at: datetime | None = None
    rca_id: str | None = None
    capa_id: str | None = None
    run_id: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "deviation_id": self.deviation_id,
            "rule_id": self.rule_id,
            "title": self.title,
            "severity": self.severity,
            "status": self.status,
            "detected_at": self.detected_at,
            "closed_at": self.closed_at,
            "plant_id": self.plant_id,
            "unit_id": self.unit_id,
            "machine_id": self.machine_id,
            "batch_id": self.batch_id,
            "trigger_event": self.trigger_event,
            "trigger_event_id": self.trigger_event_id,
            "failure_id": self.failure_id,
            "description": self.description,
            "requires_rca": self.requires_rca,
            "requires_capa": self.requires_capa,
            "rca_id": self.rca_id,
            "capa_id": self.capa_id,
            "run_id": self.run_id,
        }


@dataclass(slots=True)
class RcaEvidenceItem:
    """One quantified observation supporting a conclusion."""

    rca_id: str
    evidence_id: str
    description: str
    tag: str | None
    signal: str | None
    observed_value: float
    threshold: float
    weight: float

    def as_row(self) -> dict[str, Any]:
        return {
            "rca_id": self.rca_id,
            "evidence_id": self.evidence_id,
            "description": self.description,
            "tag": self.tag,
            "signal": self.signal,
            "observed_value": round(self.observed_value, 5),
            "threshold": self.threshold,
            "weight": self.weight,
        }

    def render(self) -> str:
        if self.tag is not None:
            return f"{self.tag} changed by {self.observed_value * 100:+.1f}% over the window"
        return f"{self.signal} = {self.observed_value:.2f}"


@dataclass(slots=True)
class RcaReport:
    """The investigation's conclusion — a claim, which may be wrong."""

    rca_id: str
    deviation_id: str
    machine_id: str | None
    batch_id: str | None
    failure_id: str | None
    started_at: datetime
    completed_at: datetime
    method: str
    root_cause: str
    confidence: float
    score: float
    fishbone_category: str
    five_why: tuple[str, ...]
    causal_chain: tuple[str, ...]
    corrective_action: str
    preventive_action: str
    evidence: list[RcaEvidenceItem] = field(default_factory=list)
    considered: tuple[str, ...] = ()
    run_id: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "rca_id": self.rca_id,
            "deviation_id": self.deviation_id,
            "machine_id": self.machine_id,
            "batch_id": self.batch_id,
            "failure_id": self.failure_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "method": self.method,
            "root_cause": self.root_cause,
            "confidence": round(self.confidence, 4),
            "score": round(self.score, 4),
            "fishbone_category": self.fishbone_category,
            "five_why": " | ".join(self.five_why),
            "causal_chain": " -> ".join(self.causal_chain),
            "corrective_action": self.corrective_action,
            "preventive_action": self.preventive_action,
            "evidence_summary": "; ".join(item.render() for item in self.evidence),
            "evidence_count": len(self.evidence),
            "alternatives_considered": ",".join(self.considered),
            "run_id": self.run_id,
        }


@dataclass(slots=True)
class Capa:
    """A corrective and preventive action with verification (§24)."""

    capa_id: str
    deviation_id: str
    rca_id: str
    problem: str
    root_cause: str
    corrective_action: str
    preventive_action: str
    opened_at: datetime
    owner_id: str | None
    status: str
    verification_batches_required: int
    verification_batches_passed: int = 0
    verified_batch_ids: list[str] = field(default_factory=list)
    closed_at: datetime | None = None
    run_id: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "capa_id": self.capa_id,
            "deviation_id": self.deviation_id,
            "rca_id": self.rca_id,
            "problem": self.problem,
            "root_cause": self.root_cause,
            "corrective_action": self.corrective_action,
            "preventive_action": self.preventive_action,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "owner_id": self.owner_id,
            "status": self.status,
            "verification_batches_required": self.verification_batches_required,
            "verification_batches_passed": self.verification_batches_passed,
            "verified_batch_ids": ",".join(self.verified_batch_ids),
            "run_id": self.run_id,
        }


class DeviationManager:
    """Opens deviations from configured triggering events."""

    def __init__(self, ctx: SimContext) -> None:
        self._ctx = ctx
        self._deviations: dict[str, Deviation] = {}
        self._rules = {rule.trigger_event: rule for rule in ctx.config.deviations.rules}

    @property
    def deviations(self) -> dict[str, Deviation]:
        return self._deviations

    def rule_for(self, event_type: str):
        return self._rules.get(event_type)

    def open(
        self,
        *,
        event_type: str,
        event_id: str,
        now: datetime,
        unit_id: str | None,
        machine_id: str | None,
        batch_id: str | None,
        failure_id: str | None,
        description: str,
    ) -> Deviation | None:
        """Open a deviation if a configured rule covers this event type."""
        rule = self._rules.get(event_type)
        if rule is None:
            return None
        deviation = Deviation(
            deviation_id=self._ctx.ids.deviation(),
            rule_id=rule.id,
            title=rule.title or event_type,
            severity=rule.severity,
            status=self._ctx.config.deviations.statuses[0],
            detected_at=now,
            plant_id=self._ctx.plant_id,
            unit_id=unit_id,
            machine_id=machine_id,
            batch_id=batch_id,
            trigger_event=event_type,
            trigger_event_id=event_id,
            failure_id=failure_id,
            description=description,
            requires_rca=rule.requires_rca,
            requires_capa=rule.requires_capa,
            run_id=self._ctx.run_id,
        )
        self._deviations[deviation.deviation_id] = deviation
        self._ctx.bus.publish(
            "DEVIATION_CREATED",
            now,
            unit_id=unit_id,
            machine_id=machine_id,
            batch_id=batch_id,
            severity=rule.severity,
            payload={
                "deviation_id": deviation.deviation_id,
                "title": deviation.title,
                "severity": rule.severity,
            },
        )
        return deviation

    def close(self, deviation: Deviation, now: datetime) -> None:
        deviation.status = self._ctx.config.deviations.statuses[-1]
        deviation.closed_at = now
        self._ctx.records.write("deviations", deviation.as_row())
        self._ctx.bus.publish(
            "DEVIATION_CLOSED",
            now,
            unit_id=deviation.unit_id,
            machine_id=deviation.machine_id,
            batch_id=deviation.batch_id,
            payload={"deviation_id": deviation.deviation_id},
        )

    def advance(self, deviation: Deviation, status: str) -> None:
        if status in self._ctx.config.deviations.statuses:
            deviation.status = status


class RcaEngine:
    """Rule-based root-cause analysis over recorded evidence only."""

    def __init__(self, ctx: SimContext) -> None:
        self._ctx = ctx
        config = ctx.config.rca_rules
        self._evidence_rules: dict[str, RcaEvidenceRule] = {
            rule.id: rule for rule in config.evidence_rules
        }
        self._rules: tuple[RcaRuleSpec, ...] = tuple(config.rules)
        self._fallback = config.fallback_root_cause
        self._reports: dict[str, RcaReport] = {}

    @property
    def reports(self) -> dict[str, RcaReport]:
        return self._reports

    def investigate(
        self,
        deviation: Deviation,
        *,
        now: datetime,
        history: SensorHistory | None,
        category: str | None,
        signals: dict[str, float],
    ) -> RcaReport:
        """Produce a conclusion from the evidence available in the dataset."""
        lookback = self._ctx.config.plant.simulation.rca_lookback_hours
        rca_id = self._ctx.ids.rca()
        started = deviation.detected_at

        self._ctx.bus.publish(
            "RCA_STARTED",
            now,
            unit_id=deviation.unit_id,
            machine_id=deviation.machine_id,
            batch_id=deviation.batch_id,
            payload={"rca_id": rca_id, "deviation_id": deviation.deviation_id},
        )

        # Evidence is gathered over the window ending when the deviation was
        # DETECTED, not when the investigation ran. By RCA time the machine has
        # usually been repaired and is running normally again, which would mask
        # the trend that caused the stop in the first place.
        matched = self._gather_evidence(
            rca_id, history, signals, deviation.detected_at, lookback
        )

        best: tuple[float, RcaRuleSpec] | None = None
        considered: list[str] = []
        for rule in self._rules:
            if rule.categories and category is not None and category not in rule.categories:
                continue
            score = sum(matched[eid].weight for eid in rule.evidence if eid in matched)
            if score <= 0.0:
                continue
            considered.append(f"{rule.root_cause}:{score:.2f}")
            if score >= rule.min_score and (best is None or score > best[0]):
                best = (score, rule)

        if best is None:
            report = RcaReport(
                rca_id=rca_id,
                deviation_id=deviation.deviation_id,
                machine_id=deviation.machine_id,
                batch_id=deviation.batch_id,
                failure_id=deviation.failure_id,
                started_at=started,
                completed_at=now,
                method="5_WHY+CAUSAL_GRAPH",
                root_cause=self._fallback,
                confidence=0.2,
                score=0.0,
                fishbone_category="UNKNOWN",
                five_why=(
                    "Why did the event occur? The available evidence does not "
                    "identify a single cause.",
                ),
                causal_chain=("insufficient evidence",),
                corrective_action="Extend monitoring and repeat the investigation.",
                preventive_action="Add instrumentation or evidence rules for this failure class.",
                evidence=list(matched.values()),
                considered=tuple(sorted(considered)),
                run_id=self._ctx.run_id,
            )
        else:
            score, rule = best
            supporting = [matched[eid] for eid in rule.evidence if eid in matched]
            total_weight = sum(
                self._evidence_rules[eid].weight
                for eid in rule.evidence
                if eid in self._evidence_rules
            )
            confidence = min(0.97, score / total_weight) if total_weight > 0 else 0.5
            report = RcaReport(
                rca_id=rca_id,
                deviation_id=deviation.deviation_id,
                machine_id=deviation.machine_id,
                batch_id=deviation.batch_id,
                failure_id=deviation.failure_id,
                started_at=started,
                completed_at=now,
                method="5_WHY+CAUSAL_GRAPH",
                root_cause=rule.root_cause,
                confidence=confidence,
                score=score,
                fishbone_category=rule.fishbone_category,
                five_why=tuple(rule.five_why),
                causal_chain=self._causal_chain(supporting, rule.root_cause),
                corrective_action=rule.corrective_action,
                preventive_action=rule.preventive_action,
                evidence=supporting,
                considered=tuple(sorted(considered)),
                run_id=self._ctx.run_id,
            )

        self._reports[rca_id] = report
        deviation.rca_id = rca_id
        self._ctx.records.write("rca", report.as_row())
        for item in report.evidence:
            self._ctx.records.write("rca_evidence", item.as_row())

        self._ctx.bus.publish(
            "RCA_COMPLETED",
            now,
            unit_id=deviation.unit_id,
            machine_id=deviation.machine_id,
            batch_id=deviation.batch_id,
            payload={
                "rca_id": rca_id,
                "root_cause": report.root_cause,
                "confidence": round(report.confidence, 4),
            },
        )
        return report

    def _gather_evidence(
        self,
        rca_id: str,
        history: SensorHistory | None,
        signals: dict[str, float],
        until: datetime,
        lookback: float,
    ) -> dict[str, RcaEvidenceItem]:
        """Evaluate every evidence rule against the recorded window."""
        matched: dict[str, RcaEvidenceItem] = {}
        for rule in self._evidence_rules.values():
            observed: float | None = None
            threshold: float | None = None

            if rule.tag is not None and history is not None:
                stats = history.stats(rule.tag, until, lookback)
                if stats is None or rule.min_delta_fraction is None:
                    continue
                threshold = rule.min_delta_fraction
                observed = (
                    stats.variance_ratio
                    if rule.statistic == "variance_ratio"
                    else stats.delta_fraction
                )
                if not self._passes(observed, threshold, rule.statistic):
                    continue
            elif rule.signal is not None:
                if rule.min_value is None:
                    continue
                value = signals.get(rule.signal)
                if value is None or value < rule.min_value:
                    continue
                observed, threshold = value, rule.min_value
            else:
                continue

            matched[rule.id] = RcaEvidenceItem(
                rca_id=rca_id,
                evidence_id=rule.id,
                description=rule.description,
                tag=rule.tag,
                signal=rule.signal,
                observed_value=observed if observed is not None else 0.0,
                threshold=threshold if threshold is not None else 0.0,
                weight=rule.weight,
            )
        return matched

    @staticmethod
    def _passes(observed: float, threshold: float, statistic: str) -> bool:
        """A negative threshold matches a fall, so a decline can be evidence."""
        if statistic == "variance_ratio":
            return observed >= threshold
        if threshold < 0.0:
            return observed <= threshold
        return observed >= threshold

    @staticmethod
    def _causal_chain(evidence: list[RcaEvidenceItem], root_cause: str) -> tuple[str, ...]:
        """Cause-to-effect graph, ordered by evidence strength."""
        ordered = sorted(evidence, key=lambda item: -item.weight)
        chain = [root_cause]
        chain.extend(item.render() for item in ordered)
        chain.append("machine stopped / specification breached")
        return tuple(chain)


class CapaManager:
    """Opens CAPAs from RCA conclusions and closes them on verification."""

    def __init__(self, ctx: SimContext) -> None:
        self._ctx = ctx
        self._capas: dict[str, Capa] = {}
        self._open_by_machine: dict[str, list[str]] = {}

    @property
    def capas(self) -> dict[str, Capa]:
        return self._capas

    def open(self, deviation: Deviation, report: RcaReport, now: datetime) -> Capa:
        required = self._ctx.config.rca_rules.verification_batches
        owner = None
        if deviation.unit_id is not None:
            managers = self._ctx.plant.unit(deviation.unit_id).manager_ids
            owner = managers[0] if managers else None

        capa = Capa(
            capa_id=self._ctx.ids.capa(),
            deviation_id=deviation.deviation_id,
            rca_id=report.rca_id,
            problem=deviation.title,
            root_cause=report.root_cause,
            corrective_action=report.corrective_action,
            preventive_action=report.preventive_action,
            opened_at=now,
            owner_id=owner,
            status="OPEN",
            verification_batches_required=required,
            run_id=self._ctx.run_id,
        )
        self._capas[capa.capa_id] = capa
        deviation.capa_id = capa.capa_id
        if deviation.machine_id:
            self._open_by_machine.setdefault(deviation.machine_id, []).append(capa.capa_id)

        self._ctx.bus.publish(
            "CAPA_CREATED",
            now,
            unit_id=deviation.unit_id,
            machine_id=deviation.machine_id,
            batch_id=deviation.batch_id,
            payload={
                "capa_id": capa.capa_id,
                "rca_id": report.rca_id,
                "corrective_action": capa.corrective_action,
            },
        )
        return capa

    def register_verification_batch(
        self, machine_id: str, batch: Batch, now: datetime
    ) -> list[Capa]:
        """Count a good batch toward any open CAPA on this machine.

        Verification is the step that says whether the corrective action actually
        worked — the question §48 wants the data to be able to answer.
        """
        closed: list[Capa] = []
        capa_ids = self._open_by_machine.get(machine_id)
        if not capa_ids:
            return closed
        if batch.disposition != "RELEASED":
            return closed

        for capa_id in list(capa_ids):
            capa = self._capas[capa_id]
            if capa.status == "CLOSED":
                capa_ids.remove(capa_id)
                continue
            capa.verification_batches_passed += 1
            capa.verified_batch_ids.append(batch.batch_id)
            capa.status = "VERIFICATION"
            if capa.verification_batches_passed >= capa.verification_batches_required:
                capa.status = "CLOSED"
                capa.closed_at = now
                capa_ids.remove(capa_id)
                closed.append(capa)
                self._ctx.bus.publish(
                    "CAPA_CLOSED",
                    now,
                    machine_id=machine_id,
                    batch_id=batch.batch_id,
                    payload={
                        "capa_id": capa.capa_id,
                        "verified_batches": capa.verification_batches_passed,
                    },
                )
        return closed

    def finalise(self) -> None:
        """Persist every CAPA at the end of the run, closed or not."""
        for capa in self._capas.values():
            self._ctx.records.write("capa", capa.as_row())
