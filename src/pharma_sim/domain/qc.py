"""Quality control engine.

A QC value is *computed* from the conditions the batch actually experienced. The
inputs come from the telemetry the machine produced during the stage, so if a
degrading bearing pushed compression force up, the hardness result rises for that
reason and not because a separate random draw decided it should (§17).

Two details matter for correctness:

* **Stage-scoped input resolution.** ``inlet_temperature`` exists in drying and in
  coating. A QC parameter resolves its inputs from its own stage first, so
  moisture reads the dryer and coating weight reads the coater.
* **Deferred evaluation.** A parameter whose inputs are not available yet is
  postponed rather than computed against a fallback. Disintegration time depends
  on coating weight, so for a coated tablet it is genuinely measured after
  coating — deferring it is physically right, not a workaround.
"""

from __future__ import annotations

from datetime import datetime
from random import Random

from pharma_sim.config.drivers import QC_DRIVERS
from pharma_sim.config.models import ProductSpec
from pharma_sim.domain.batch import Batch, QcResult
from pharma_sim.domain.environment import Ambient
from pharma_sim.registry.qc import EffectiveQcSpec, QcRegistry

__all__ = ["QcEngine"]


class QcEngine:
    """Computes QC results for a batch from its achieved process conditions."""

    def __init__(self, registry: QcRegistry, run_id: str) -> None:
        self._registry = registry
        self._run_id = run_id
        self._references = registry.reference_values()

    # --------------------------------------------------------------- evaluation
    def evaluate_stage(
        self,
        batch: Batch,
        product: ProductSpec,
        stage: str,
        *,
        now: datetime,
        machine_id: str,
        operator_id: str | None,
        machine_health: float,
        operator_inexperience: float,
        ambient: Ambient,
        rng: Random,
        next_test_id,
    ) -> list[QcResult]:
        """In-process QC for a completed stage.

        Only parameters whose QC inputs are already available are evaluated; the
        rest wait for :meth:`evaluate_final`.
        """
        candidates = [
            spec
            for spec in self._registry.for_product(product, phase="IN_PROCESS")
            if spec.stage == stage and spec.id not in batch.computed_qc_ids()
        ]
        return self._evaluate(
            candidates,
            batch,
            product,
            now=now,
            machine_id=machine_id,
            operator_id=operator_id,
            machine_health=machine_health,
            operator_inexperience=operator_inexperience,
            ambient=ambient,
            rng=rng,
            next_test_id=next_test_id,
            require_inputs=True,
        )

    def evaluate_final(
        self,
        batch: Batch,
        product: ProductSpec,
        *,
        now: datetime,
        operator_id: str | None,
        machine_health: float,
        operator_inexperience: float,
        ambient: Ambient,
        rng: Random,
        next_test_id,
    ) -> list[QcResult]:
        """Everything still outstanding: deferred in-process tests, then release tests."""
        already = batch.computed_qc_ids()
        candidates = [
            spec for spec in self._registry.for_product(product) if spec.id not in already
        ]
        return self._evaluate(
            candidates,
            batch,
            product,
            now=now,
            machine_id=None,
            operator_id=operator_id,
            machine_health=machine_health,
            operator_inexperience=operator_inexperience,
            ambient=ambient,
            rng=rng,
            next_test_id=next_test_id,
            require_inputs=False,
        )

    # ------------------------------------------------------------------ internals
    def _evaluate(
        self,
        candidates: list[EffectiveQcSpec],
        batch: Batch,
        product: ProductSpec,
        *,
        now: datetime,
        machine_id: str | None,
        operator_id: str | None,
        machine_health: float,
        operator_inexperience: float,
        ambient: Ambient,
        rng: Random,
        next_test_id,
        require_inputs: bool,
    ) -> list[QcResult]:
        stage_parameters = batch.computed_parameters()
        merged = self._merge_parameters(stage_parameters, batch.route)
        drivers = {
            "machine_health": machine_health,
            "operator_inexperience": operator_inexperience,
            "ambient_temperature_c": ambient.temperature_c,
            "ambient_humidity_pct": ambient.humidity_pct,
            "material_variability": batch.material_variability,
            "batch_size": float(product.batch_size),
            "stage_duration_min": self._mean_stage_duration(batch),
        }

        results: list[QcResult] = []
        # Registry order is topological, so an input computed in this same pass is
        # available to its consumer.
        for spec in candidates:
            computed = batch.qc_values()
            computed.update({r.parameter: r.actual_value for r in results})

            if require_inputs and not self._inputs_ready(spec, computed, merged, drivers):
                continue

            values = self._input_values(spec, stage_parameters, merged, computed, drivers)
            value = spec.spec.transfer.evaluate(values, self._references)

            # Analytical noise, inflated by a degrading machine: a sick machine
            # produces more variable product, not merely a shifted mean.
            sigma = spec.spec.noise_sigma * (1.0 + 1.5 * machine_health)
            if sigma > 0.0:
                # Averaging over the sample reduces the standard error, as a real
                # multi-tablet determination would.
                sigma /= max(1.0, spec.spec.sample_size**0.5)
                value += rng.gauss(0.0, sigma)
            if spec.spec.health_sensitivity:
                value += spec.spec.health_sensitivity * abs(spec.target) * machine_health

            clip_min = spec.spec.transfer.clip_min
            clip_max = spec.spec.transfer.clip_max
            if clip_min is not None:
                value = max(clip_min, value)
            if clip_max is not None:
                value = min(clip_max, value)

            results.append(
                QcResult(
                    test_id=next_test_id(),
                    batch_id=batch.batch_id,
                    product_id=product.product_id,
                    parameter=spec.id,
                    parameter_name=spec.spec.name,
                    stage=spec.stage,
                    phase=spec.phase,
                    target=spec.target,
                    lower_limit=spec.lower_limit,
                    upper_limit=spec.upper_limit,
                    actual_value=value,
                    result=spec.classify(value),
                    timestamp=now,
                    operator_id=operator_id,
                    machine_id=machine_id,
                    unit=spec.spec.unit,
                    sample_size=spec.spec.sample_size,
                    run_id=self._run_id,
                )
            )
        return results

    @staticmethod
    def _merge_parameters(
        stage_parameters: dict[str, dict[str, float]], route: tuple[str, ...]
    ) -> dict[str, float]:
        """Flatten achieved parameters, later stages taking precedence.

        Route order rather than dict order, so the result does not depend on the
        order stages happened to be recorded in.
        """
        merged: dict[str, float] = {}
        for stage in route:
            merged.update(stage_parameters.get(stage, {}))
        for stage, values in stage_parameters.items():
            if stage not in route:
                merged.update(values)
        return merged

    @staticmethod
    def _mean_stage_duration(batch: Batch) -> float:
        durations = [s.duration_minutes for s in batch.stages if s.duration_minutes > 0.0]
        return sum(durations) / len(durations) if durations else 0.0

    def _inputs_ready(
        self,
        spec: EffectiveQcSpec,
        computed: dict[str, float],
        merged: dict[str, float],
        drivers: dict[str, float],
    ) -> bool:
        """Whether every input this parameter needs is genuinely available."""
        for name in spec.spec.transfer.inputs:
            if name in drivers or name in merged or name in computed:
                continue
            if name in self._registry:
                return False  # a QC input that has not been measured yet
            if name in QC_DRIVERS:
                continue
            return False
        return True

    def _input_values(
        self,
        spec: EffectiveQcSpec,
        stage_parameters: dict[str, dict[str, float]],
        merged: dict[str, float],
        computed: dict[str, float],
        drivers: dict[str, float],
    ) -> dict[str, float]:
        """Resolve inputs, preferring this parameter's own stage.

        Without the stage preference, a coating QC test would read the dryer's
        inlet temperature simply because both tags share a name.
        """
        own_stage = stage_parameters.get(spec.stage, {})
        values: dict[str, float] = {}
        for name in spec.spec.transfer.inputs:
            if name in own_stage:
                values[name] = own_stage[name]
            elif name in computed:
                values[name] = computed[name]
            elif name in merged:
                values[name] = merged[name]
            elif name in drivers:
                values[name] = drivers[name]
        return values

    # ------------------------------------------------------------------ verdicts
    def disposition_for(self, batch: Batch) -> str:
        """Release or reject a batch from its QC results.

        A gross breach rejects outright; a final-phase failure rejects when the
        configuration says so, which is how a policy change stays a config edit.
        """
        from pharma_sim.domain.batch import Disposition

        for result in batch.qc_results:
            if result.result == "OOS":
                return Disposition.REJECTED
        if self._registry.reject_on_final_failure:
            for result in batch.qc_results:
                if result.phase == "FINAL" and result.result == "FAIL":
                    return Disposition.REJECTED
        # An in-process failure that release testing did not catch still warrants
        # holding the batch rather than releasing it.
        if any(r.result == "FAIL" for r in batch.qc_results):
            return Disposition.QUARANTINED
        return Disposition.RELEASED
