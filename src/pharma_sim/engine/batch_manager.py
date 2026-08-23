"""Batch orchestration.

A batch walks its product's route stage by stage. For each stage the manager
picks a unit, picks a machine in it, points that machine's tags at the product's
setpoints, and lets it run. When the stage ends the *measured* mean of each
process parameter becomes the achieved value, and QC is computed from those
numbers.

That ordering is the whole point: quality is downstream of telemetry. A degrading
bearing shifts compression force in the sensor stream, the stage records the
shifted force, and the hardness result moves accordingly — rather than a separate
random draw deciding the batch failed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from pharma_sim.config.models import ProductSpec
from pharma_sim.domain.batch import Batch, BatchStage, Disposition, StageResult
from pharma_sim.domain.machine import Machine
from pharma_sim.domain.qc import QcEngine
from pharma_sim.engine.context import SimContext
from pharma_sim.engine.scheduler import Priority

__all__ = ["BatchManager", "BatchStats"]

logger = logging.getLogger(__name__)

#: How many times a stage waits for its machine to come back from a repair
#: before it gives up and completes with whatever was produced.
_MAX_REPAIR_WAITS = 8


@dataclass(slots=True)
class BatchStats:
    orders_created: int = 0
    batches_started: int = 0
    batches_completed: int = 0
    released: int = 0
    rejected: int = 0
    quarantined: int = 0
    stages_completed: int = 0
    stages_failed: int = 0
    qc_tests: int = 0
    qc_failures: int = 0
    setup_errors: int = 0
    stage_waits: int = 0


class BatchManager:
    """Creates production orders and drives batches through their route."""

    def __init__(
        self,
        ctx: SimContext,
        qc: QcEngine,
        *,
        current_shift: Callable[[], str | None],
        on_setup_error: Callable[[Machine, datetime], None] | None = None,
        on_batch_complete: Callable[[Batch], None] | None = None,
    ) -> None:
        self._ctx = ctx
        self._qc = qc
        self._current_shift = current_shift
        self._on_setup_error = on_setup_error
        self._on_batch_complete = on_batch_complete
        self._active: dict[str, Batch] = {}
        self._completed: list[Batch] = []
        self._stats = BatchStats()
        self._demand_multiplier = 1.0
        self._horizon: datetime | None = None
        self._blocked_materials: set[str] = set()

    @property
    def stats(self) -> BatchStats:
        return self._stats

    @property
    def active(self) -> dict[str, Batch]:
        return self._active

    @property
    def completed(self) -> list[Batch]:
        return self._completed

    def all_batches(self) -> list[Batch]:
        return [*self._completed, *self._active.values()]

    def set_demand_multiplier(self, multiplier: float) -> None:
        self._demand_multiplier = max(0.0, multiplier)

    def set_horizon(self, horizon: datetime | None) -> None:
        """Stop starting work that cannot finish inside the run."""
        self._horizon = horizon

    def block_material(self, material_id: str) -> None:
        self._blocked_materials.add(material_id)

    def unblock_material(self, material_id: str) -> None:
        self._blocked_materials.discard(material_id)

    # ------------------------------------------------------------------- ordering
    def replenish(self, now: datetime) -> int:
        """Top the plant up to its configured concurrent-batch limit."""
        limit = max(
            1,
            int(
                round(
                    self._ctx.registries.topology.max_concurrent_batches
                    * self._demand_multiplier
                )
            ),
        )
        created = 0
        while len(self._active) < limit:
            if self._horizon is not None and now >= self._horizon:
                break
            if not self._create_batch(now):
                break
            created += 1
        return created

    def _create_batch(self, now: datetime) -> bool:
        topology = self._ctx.registries.topology
        weights = topology.demand_weights()
        if not weights:
            return False

        rng = self._ctx.rngs.child("orders")
        product_id = rng.choices(
            [pid for pid, _ in weights], weights=[w for _, w in weights], k=1
        )[0]
        product = topology.product(product_id)

        order_id = self._ctx.ids.order()
        batch = Batch(
            batch_id=self._ctx.ids.batch(),
            order_id=order_id,
            product_id=product_id,
            plant_id=self._ctx.plant_id,
            planned_quantity=product.target_quantity,
            created_at=now,
            run_id=self._ctx.run_id,
            route=tuple(product.manufacturing_process),
        )
        # Lot-to-lot material variability, drawn once per batch: this is the
        # material contribution to assay and content uniformity.
        material_rng = self._ctx.rngs.child("material", batch.batch_id)
        if product.raw_materials:
            batch.material_variability = sum(
                material_rng.gauss(0.0, material.variability)
                for material in product.raw_materials
            ) / len(product.raw_materials)
            for material in product.raw_materials:
                batch.raw_material_lots[material.material_id] = (
                    f"LOT-{material.material_id}-{material_rng.randint(1000, 9999)}"
                )
                if material.material_id in self._blocked_materials:
                    self._ctx.bus.publish(
                        "MATERIAL_SHORTAGE",
                        now,
                        batch_id=None,
                        severity="MAJOR",
                        payload={"material_id": material.material_id},
                    )
                    return False

        self._active[batch.batch_id] = batch
        self._stats.orders_created += 1
        self._stats.batches_started += 1

        # The batch row is written at creation, not completion: events reference
        # it while it is in process, and the foreign key is enforced.
        self._ctx.records.write("batches", batch.as_row())

        self._ctx.bus.publish(
            "PRODUCTION_ORDER_CREATED",
            now,
            batch_id=batch.batch_id,
            payload={
                "order_id": order_id,
                "product_id": product_id,
                "quantity": product.target_quantity,
            },
        )
        batch.started_at = now
        self._ctx.bus.publish(
            "BATCH_STARTED",
            now,
            batch_id=batch.batch_id,
            payload={"product_id": product_id, "order_id": order_id},
        )
        self._start_stage(batch, product, now)
        return True

    # --------------------------------------------------------------------- stages
    def _start_stage(self, batch: Batch, product: ProductSpec, now: datetime) -> None:
        stage = batch.current_stage
        if stage is None:
            self._finish_batch(batch, product, now)
            return

        machine = self._select_machine(stage, product)
        if machine is None:
            # Nothing free right now. Wait rather than dropping the batch, so a
            # busy plant shows queueing instead of vanished work.
            self._stats.stage_waits += 1
            self._ctx.scheduler.at(
                now + timedelta(minutes=20),
                lambda moment: self._start_stage(batch, product, moment),
                priority=Priority.BATCH,
                label=f"stage-wait:{batch.batch_id}",
            )
            return

        setpoints = {
            name: window.target
            for name, window in product.process_parameters.get(stage, {}).items()
        }
        machine.accrue_time(now)
        machine.begin_stage(batch.batch_id, stage, product.product_id)
        machine.apply_setpoints(setpoints)

        record = BatchStage(
            batch_id=batch.batch_id,
            stage=stage,
            sequence=batch.stage_index + 1,
            unit_id=machine.unit_id,
            machine_id=machine.machine_id,
            started_at=now,
            operator_ids=list(machine.assigned_operators),
            shift_instance_id=self._current_shift(),
        )
        batch.record_stage(record)

        self._perform_setup(machine, now)

        running_state = self._ctx.registries.states.first("productive")
        machine.force_route_to(running_state, now, f"BATCH:{batch.batch_id}")

        self._ctx.bus.publish(
            "BATCH_STAGE_STARTED",
            now,
            unit_id=machine.unit_id,
            machine_id=machine.machine_id,
            batch_id=batch.batch_id,
            payload={"stage": stage, "machine_ids": [machine.machine_id]},
        )

        duration = self._stage_duration(machine, product, batch)
        self._ctx.scheduler.at(
            now + timedelta(minutes=duration),
            self._make_stage_end(batch, product, machine, record, 0),
            priority=Priority.BATCH,
            label=f"stage-end:{batch.batch_id}:{stage}",
        )

    def _stage_duration(self, machine: Machine, product: ProductSpec, batch: Batch) -> float:
        """Minutes this stage will take.

        Process-time equipment uses its configured duration; throughput equipment
        derives it from batch size and rate. Both are jittered a little, because
        two runs of the same recipe never take exactly the same time.
        """
        rng = self._ctx.rngs.child("stage", batch.batch_id, machine.machine_id)
        if machine.spec.stage_duration_min is not None:
            base = machine.spec.stage_duration_min
        else:
            base = (batch.planned_quantity / machine.spec.nominal_rate_per_hour) * 60.0
        # A less experienced operator is a little slower.
        base *= 1.0 + 0.12 * machine.operator_inexperience
        return max(5.0, base * rng.uniform(0.9, 1.15))

    def _perform_setup(self, machine: Machine, now: datetime) -> None:
        """Setup, with an error probability driven by operator experience (§7)."""
        rng = self._ctx.rngs.child("setup", machine.machine_id)
        # A junior operator is several times likelier to mis-set a machine than a
        # senior one, which is the §7 requirement that operator behaviour
        # influences outcomes probabilistically rather than deterministically.
        error_probability = 0.004 + 0.030 * machine.operator_inexperience
        errored = rng.random() < error_probability
        self._ctx.bus.publish(
            "SETUP_PERFORMED",
            now,
            unit_id=machine.unit_id,
            machine_id=machine.machine_id,
            batch_id=machine.current_batch_id,
            employee_id=machine.assigned_operators[0] if machine.assigned_operators else None,
            payload={
                "duration_min": machine.spec.setup_duration_min,
                "setup_error": errored,
            },
        )
        if errored:
            self._stats.setup_errors += 1
            machine.record_incident("SETUP_ERROR", now)
            if self._on_setup_error is not None:
                self._on_setup_error(machine, now)

    def _select_machine(self, stage: str, product: ProductSpec) -> Machine | None:
        """Pick a free machine that can perform this stage.

        Preference goes to equipment that actually measures the stage's process
        parameters, so a tablet press rather than a deduster runs compression.
        """
        states = self._ctx.registries.states
        wanted = set(product.process_parameters.get(stage, {}))
        candidates: list[tuple[int, Machine]] = []

        for unit_id in self._ctx.registries.topology.units_for_stage(stage):
            for machine in self._ctx.plant.machines_in(unit_id):
                if machine.current_batch_id is not None:
                    continue
                if machine.maintenance_pending:
                    continue
                if states.is_downtime(machine.state) or states.is_offline(machine.state):
                    continue
                if not machine.has_operator:
                    continue
                tags = set(machine.sensors)
                score = len(wanted & tags)
                candidates.append((score, machine))

        if not candidates:
            return None
        best_score = max(score for score, _ in candidates)
        if wanted and best_score == 0:
            # Every machine that actually measures this stage's parameters is
            # busy. Waiting is correct: running compression on a deduster would
            # produce a stage with no process values at all, and QC downstream
            # would silently fall back to nominal.
            return None
        pool = [machine for score, machine in candidates if score == best_score]
        # Least-used first, so load spreads across identical machines.
        return min(pool, key=lambda m: (m.operating_hours, m.machine_id))

    def _make_stage_end(
        self,
        batch: Batch,
        product: ProductSpec,
        machine: Machine,
        record: BatchStage,
        waits: int,
    ) -> Callable[[datetime], None]:
        def callback(now: datetime) -> None:
            states = self._ctx.registries.states
            # If the machine is down, the stage genuinely waits for the repair.
            if states.is_downtime(machine.state) and waits < _MAX_REPAIR_WAITS:
                self._stats.stage_waits += 1
                self._ctx.scheduler.at(
                    now + timedelta(hours=1),
                    self._make_stage_end(batch, product, machine, record, waits + 1),
                    priority=Priority.BATCH,
                    label=f"stage-wait-repair:{batch.batch_id}",
                )
                return
            self._complete_stage(batch, product, machine, record, now)

        return callback

    def _complete_stage(
        self,
        batch: Batch,
        product: ProductSpec,
        machine: Machine,
        record: BatchStage,
        now: datetime,
    ) -> None:
        machine.accrue_time(now)
        health = machine.health_at(now)
        achieved = machine.end_stage()

        record.completed_at = now
        record.duration_minutes = (now - record.started_at).total_seconds() / 60.0
        record.machine_health = health
        record.parameters = achieved

        windows = product.process_parameters.get(record.stage, {})
        for name, window in windows.items():
            value = achieved.get(name)
            if value is None:
                continue
            if (window.min is not None and value < window.min) or (
                window.max is not None and value > window.max
            ):
                record.deviating_parameters.append(name)

        active_failures = [
            episode
            for episode in machine.episodes
            if episode.faulted_at is not None
            and record.started_at <= episode.faulted_at <= now
        ]
        if active_failures:
            record.interrupted_by_failure = active_failures[-1].failure_id
            batch.link_failure(active_failures[-1].failure_id)
            record.result = StageResult.FAIL
            self._stats.stages_failed += 1
        elif record.deviating_parameters:
            record.result = StageResult.WARNING
        else:
            record.result = StageResult.PASS

        self._ctx.records.write("batch_stages", record.as_row())
        self._stats.stages_completed += 1

        self._ctx.bus.publish(
            "BATCH_STAGE_COMPLETED",
            now,
            unit_id=machine.unit_id,
            machine_id=machine.machine_id,
            batch_id=batch.batch_id,
            payload={
                "stage": record.stage,
                "result": record.result,
                "deviating_parameters": record.deviating_parameters,
            },
        )

        self._run_in_process_qc(batch, product, machine, record, health, now)

        # Release the machine: clean down where the state model has a cleaning
        # state, otherwise straight back to idle.
        cleaning_state = self._ctx.registries.states.first_or_none("cleaning")
        idle_state = self._ctx.registries.states.first("idle")
        if cleaning_state is not None and machine.force_route_to(
            cleaning_state, now, "POST_STAGE_CLEANING"
        ):
            self._ctx.bus.publish(
                "CLEANING_PERFORMED",
                now,
                unit_id=machine.unit_id,
                machine_id=machine.machine_id,
                payload={"duration_min": machine.spec.cleaning_duration_min},
            )
            self._ctx.scheduler.at(
                now + timedelta(minutes=machine.spec.cleaning_duration_min),
                self._make_cleaning_end(machine),
                priority=Priority.MACHINE,
                label=f"clean-end:{machine.machine_id}",
            )
        else:
            machine.force_route_to(idle_state, now, "STAGE_COMPLETE")

        batch.stage_index += 1
        if batch.complete:
            self._finish_batch(batch, product, now)
        else:
            self._start_stage(batch, product, now)

    def _make_cleaning_end(self, machine: Machine) -> Callable[[datetime], None]:
        idle_state = self._ctx.registries.states.first("idle")

        def callback(now: datetime) -> None:
            machine.accrue_time(now)
            machine.force_route_to(idle_state, now, "CLEANING_COMPLETE")

        return callback

    def _run_in_process_qc(
        self,
        batch: Batch,
        product: ProductSpec,
        machine: Machine,
        record: BatchStage,
        health: float,
        now: datetime,
    ) -> None:
        analyst = self._pick_analyst()
        self._ctx.bus.publish(
            "QC_SAMPLE_COLLECTION",
            now,
            unit_id=machine.unit_id,
            machine_id=machine.machine_id,
            batch_id=batch.batch_id,
            employee_id=analyst,
            payload={"stage": record.stage, "sample_count": 3},
        )
        results = self._qc.evaluate_stage(
            batch,
            product,
            record.stage,
            now=now,
            machine_id=machine.machine_id,
            operator_id=analyst,
            machine_health=health,
            operator_inexperience=machine.operator_inexperience,
            ambient=self._ctx.environment.at(now),
            rng=self._ctx.rngs.child("qc", batch.batch_id, record.stage),
            next_test_id=self._ctx.ids.qc_test,
        )
        self._record_qc(batch, results, machine.unit_id, machine.machine_id, now, "IN_PROCESS")

    def _record_qc(
        self,
        batch: Batch,
        results: list,
        unit_id: str | None,
        machine_id: str | None,
        now: datetime,
        phase: str,
    ) -> None:
        if not results:
            return
        passed = 0
        failed = 0
        for result in results:
            batch.add_qc_result(result)
            self._ctx.records.write("qc_results", result.as_row())
            self._stats.qc_tests += 1
            if result.failed:
                failed += 1
                self._stats.qc_failures += 1
                self._ctx.bus.publish(
                    "QC_FAILED",
                    now,
                    unit_id=unit_id,
                    machine_id=machine_id,
                    batch_id=batch.batch_id,
                    severity="MAJOR",
                    payload={
                        "parameter": result.parameter,
                        "actual_value": round(result.actual_value, 4),
                        "result": result.result,
                        "target": result.target,
                        "lower_limit": result.lower_limit,
                        "upper_limit": result.upper_limit,
                        "test_id": result.test_id,
                    },
                )
            else:
                passed += 1

        self._ctx.bus.publish(
            "QC_TEST_COMPLETED",
            now,
            unit_id=unit_id,
            machine_id=machine_id,
            batch_id=batch.batch_id,
            payload={"phase": phase, "passed": passed, "failed": failed},
        )

    def _pick_analyst(self) -> str | None:
        analysts = self._ctx.plant.technicians(
            self._ctx.registries.topology.qc_analyst_role
        )
        if not analysts:
            return None
        rng = self._ctx.rngs.child("qc-analyst")
        return rng.choice(analysts).employee_id

    # ---------------------------------------------------------------- completion
    def _finish_batch(self, batch: Batch, product: ProductSpec, now: datetime) -> None:
        analyst = self._pick_analyst()
        health = max(
            (stage.machine_health for stage in batch.stages),
            default=0.0,
        )

        self._ctx.bus.publish(
            "QC_TEST_STARTED",
            now,
            batch_id=batch.batch_id,
            employee_id=analyst,
            payload={
                "phase": "FINAL",
                "parameter_count": len(product.qc_specifications),
            },
        )
        results = self._qc.evaluate_final(
            batch,
            product,
            now=now,
            operator_id=analyst,
            machine_health=health,
            operator_inexperience=0.3,
            ambient=self._ctx.environment.at(now),
            rng=self._ctx.rngs.child("qc-final", batch.batch_id),
            next_test_id=self._ctx.ids.qc_test,
        )
        self._record_qc(batch, results, None, None, now, "FINAL")

        batch.disposition = self._qc.disposition_for(batch)
        batch.completed_at = now

        # A released batch counts as good product; a rejected or quarantined one
        # is written off in full, which is how a QC failure turns into the
        # production loss the ground-truth record attributes to its root cause.
        released = batch.disposition == Disposition.RELEASED
        batch.good_quantity = float(batch.planned_quantity) if released else 0.0
        batch.reject_quantity = 0.0 if released else float(batch.planned_quantity)

        self._active.pop(batch.batch_id, None)
        self._completed.append(batch)
        self._stats.batches_completed += 1
        if batch.disposition == Disposition.RELEASED:
            self._stats.released += 1
        elif batch.disposition == Disposition.REJECTED:
            self._stats.rejected += 1
        else:
            self._stats.quarantined += 1

        for machine_id in batch.machines_used:
            self._ctx.plant.machine(machine_id).batches_completed += 1

        self._ctx.records.write("batches", batch.as_row())
        self._ctx.bus.publish(
            "BATCH_COMPLETED",
            now,
            batch_id=batch.batch_id,
            payload={
                "product_id": batch.product_id,
                "good_quantity": batch.good_quantity,
                "reject_quantity": batch.reject_quantity,
            },
        )
        self._ctx.bus.publish(
            "BATCH_DISPOSITION",
            now,
            batch_id=batch.batch_id,
            severity="MAJOR" if batch.disposition != Disposition.RELEASED else "INFO",
            payload={"disposition": batch.disposition},
        )
        if self._on_batch_complete is not None:
            self._on_batch_complete(batch)
