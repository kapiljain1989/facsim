"""Batch model and genealogy.

A batch carries its whole lifecycle: the route it took, the machine and operator
for every stage, the process values actually achieved, the QC results those
values produced, and every failure and deviation that touched it. That record is
what makes the traceability of §20 work in both directions — batch to root cause,
and failure to affected batches.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

__all__ = ["BatchStage", "QcResult", "Batch", "Disposition", "StageResult"]


class Disposition:
    IN_PROCESS = "IN_PROCESS"
    RELEASED = "RELEASED"
    REJECTED = "REJECTED"
    QUARANTINED = "QUARANTINED"


class StageResult:
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"


@dataclass(slots=True)
class BatchStage:
    """One stage of one batch, on one machine."""

    batch_id: str
    stage: str
    sequence: int
    unit_id: str
    machine_id: str
    started_at: datetime
    completed_at: datetime | None = None
    result: str = StageResult.PASS
    operator_ids: list[str] = field(default_factory=list)
    shift_instance_id: str | None = None
    #: Mean achieved value per process parameter, taken from the telemetry.
    parameters: dict[str, float] = field(default_factory=dict)
    #: Parameters that finished outside their configured window.
    deviating_parameters: list[str] = field(default_factory=list)
    machine_health: float = 0.0
    interrupted_by_failure: str | None = None
    duration_minutes: float = 0.0

    def as_row(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "stage": self.stage,
            "sequence": self.sequence,
            "unit_id": self.unit_id,
            "machine_id": self.machine_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": self.result,
            "operator_ids": ",".join(self.operator_ids),
            "shift_instance_id": self.shift_instance_id,
            "parameters": {k: round(v, 4) for k, v in self.parameters.items()},
            "deviating_parameters": ",".join(self.deviating_parameters),
            "machine_health": round(self.machine_health, 4),
            "interrupted_by_failure": self.interrupted_by_failure,
            "duration_minutes": round(self.duration_minutes, 2),
        }


@dataclass(slots=True)
class QcResult:
    """One QC determination, carrying the full §18 field set."""

    test_id: str
    batch_id: str
    product_id: str
    parameter: str
    parameter_name: str
    stage: str
    phase: str
    target: float
    lower_limit: float | None
    upper_limit: float | None
    actual_value: float
    result: str
    timestamp: datetime
    operator_id: str | None
    machine_id: str | None
    unit: str
    sample_size: int
    run_id: str
    #: Provenance: which analytical method produced this number, and the
    #: precision it demonstrated in validation. None where the parameter is a
    #: physical in-process measurement rather than an analytical determination.
    method_id: str | None = None
    analytical_rsd: float | None = None

    @property
    def failed(self) -> bool:
        return self.result in {"FAIL", "OOS"}

    def as_row(self) -> dict[str, Any]:
        return {
            "test_id": self.test_id,
            "batch_id": self.batch_id,
            "product_id": self.product_id,
            "parameter": self.parameter,
            "parameter_name": self.parameter_name,
            "stage": self.stage,
            "phase": self.phase,
            "target": self.target,
            "lower_limit": self.lower_limit,
            "upper_limit": self.upper_limit,
            "actual_value": round(self.actual_value, 4),
            "result": self.result,
            "timestamp": self.timestamp,
            "operator_id": self.operator_id,
            "machine_id": self.machine_id,
            "unit": self.unit,
            "sample_size": self.sample_size,
            "method_id": self.method_id,
            "analytical_rsd": self.analytical_rsd,
            "run_id": self.run_id,
        }


@dataclass(slots=True)
class Batch:
    """A production batch and its complete genealogy."""

    batch_id: str
    order_id: str
    product_id: str
    plant_id: str
    planned_quantity: int
    created_at: datetime
    run_id: str
    route: tuple[str, ...] = ()
    started_at: datetime | None = None
    completed_at: datetime | None = None
    disposition: str = Disposition.IN_PROCESS
    stage_index: int = 0
    stages: list[BatchStage] = field(default_factory=list)
    qc_results: list[QcResult] = field(default_factory=list)
    raw_material_lots: dict[str, str] = field(default_factory=dict)
    material_variability: float = 0.0
    machines_used: list[str] = field(default_factory=list)
    units_used: list[str] = field(default_factory=list)
    operators_involved: list[str] = field(default_factory=list)
    shift_instances: list[str] = field(default_factory=list)
    failure_ids: list[str] = field(default_factory=list)
    deviation_ids: list[str] = field(default_factory=list)
    good_quantity: float = 0.0
    reject_quantity: float = 0.0

    # ------------------------------------------------------------------ progress
    @property
    def current_stage(self) -> str | None:
        if self.stage_index >= len(self.route):
            return None
        return self.route[self.stage_index]

    @property
    def complete(self) -> bool:
        return self.stage_index >= len(self.route)

    @property
    def active_stage(self) -> BatchStage | None:
        if self.stages and self.stages[-1].completed_at is None:
            return self.stages[-1]
        return None

    def stage_for(self, stage: str) -> BatchStage | None:
        for record in self.stages:
            if record.stage == stage:
                return record
        return None

    # -------------------------------------------------------------- accumulation
    def record_stage(self, record: BatchStage) -> None:
        self.stages.append(record)
        if record.machine_id not in self.machines_used:
            self.machines_used.append(record.machine_id)
        if record.unit_id not in self.units_used:
            self.units_used.append(record.unit_id)
        for operator in record.operator_ids:
            if operator not in self.operators_involved:
                self.operators_involved.append(operator)
        if record.shift_instance_id and record.shift_instance_id not in self.shift_instances:
            self.shift_instances.append(record.shift_instance_id)

    def link_failure(self, failure_id: str) -> None:
        if failure_id not in self.failure_ids:
            self.failure_ids.append(failure_id)

    def link_deviation(self, deviation_id: str) -> None:
        if deviation_id not in self.deviation_ids:
            self.deviation_ids.append(deviation_id)

    def add_qc_result(self, result: QcResult) -> None:
        self.qc_results.append(result)

    def computed_parameters(self) -> dict[str, dict[str, float]]:
        """Achieved process values, keyed by stage then parameter."""
        return {record.stage: dict(record.parameters) for record in self.stages}

    def qc_values(self) -> dict[str, float]:
        """QC results computed so far, for use as transfer inputs."""
        return {result.parameter: result.actual_value for result in self.qc_results}

    def computed_qc_ids(self) -> set[str]:
        return {result.parameter for result in self.qc_results}

    @property
    def failed_qc(self) -> list[QcResult]:
        return [result for result in self.qc_results if result.failed]

    @property
    def stage_summary(self) -> list[tuple[str, str]]:
        """Stage-by-stage outcome, the §4 view of a batch."""
        return [(record.stage, record.result) for record in self.stages]

    def as_row(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "order_id": self.order_id,
            "product_id": self.product_id,
            "plant_id": self.plant_id,
            "planned_quantity": self.planned_quantity,
            "good_quantity": round(self.good_quantity, 2),
            "reject_quantity": round(self.reject_quantity, 2),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "disposition": self.disposition,
            "route": ",".join(self.route),
            "stages_completed": len([s for s in self.stages if s.completed_at]),
            "machines_used": ",".join(self.machines_used),
            "units_used": ",".join(self.units_used),
            "operators_involved": ",".join(self.operators_involved),
            "shift_instances": ",".join(self.shift_instances),
            "failure_ids": ",".join(self.failure_ids),
            "deviation_ids": ",".join(self.deviation_ids),
            "qc_test_count": len(self.qc_results),
            "qc_failure_count": len(self.failed_qc),
            "material_variability": round(self.material_variability, 5),
            "run_id": self.run_id,
        }
