"""QC parameter registry.

Two responsibilities beyond lookup:

* **Evaluation order.** QC transfers read each other (hardness feeds
  disintegration, which feeds dissolution), so parameters must be evaluated in
  dependency order. The order is computed once here by topological sort.
* **Per-product effective limits.** Specifications are genuinely product-relative,
  so a product's ``qc_overrides`` are merged on top of the base spec.
"""

from __future__ import annotations

from dataclasses import dataclass

from pharma_sim.config.models import ProductSpec, QcParamSpec, QcRulesConfig

__all__ = ["QcRegistry", "EffectiveQcSpec"]


@dataclass(frozen=True, slots=True)
class EffectiveQcSpec:
    """A QC parameter's specification as it applies to one product."""

    spec: QcParamSpec
    target: float
    lower_limit: float | None
    upper_limit: float | None

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def stage(self) -> str:
        return self.spec.stage

    @property
    def phase(self) -> str:
        return self.spec.phase

    def classify(self, value: float) -> str:
        """Map a measured value onto ``PASS`` / ``FAIL`` / ``OOS`` / ``OOT``.

        ``OOS`` marks a clear specification breach, ``FAIL`` a marginal one just
        outside a limit, and ``OOT`` a value still inside limits but drifting into
        the outer band — the distinction quality systems actually make, and a
        useful label for anomaly detection.
        """
        below = self.lower_limit is not None and value < self.lower_limit
        above = self.upper_limit is not None and value > self.upper_limit
        if below or above:
            width = self._spec_width()
            if width > 0.0:
                excess = (
                    (self.lower_limit - value) if below else (value - self.upper_limit)  # type: ignore[operator]
                )
                # A small breach is reported as FAIL; a gross one as OOS.
                return "FAIL" if excess <= 0.10 * width else "OOS"
            return "FAIL"

        if self.spec.oot_fraction > 0.0:
            width = self._spec_width()
            if width > 0.0:
                margin = self.spec.oot_fraction * width
                if self.lower_limit is not None and value < self.lower_limit + margin:
                    return "OOT"
                if self.upper_limit is not None and value > self.upper_limit - margin:
                    return "OOT"
        return "PASS"

    def _spec_width(self) -> float:
        if self.lower_limit is not None and self.upper_limit is not None:
            return self.upper_limit - self.lower_limit
        if self.upper_limit is not None:
            return abs(self.upper_limit - self.target) * 2.0
        if self.lower_limit is not None:
            return abs(self.target - self.lower_limit) * 2.0
        return 0.0


class QcRegistry:
    """Declared QC parameters, their evaluation order and per-product limits."""

    __slots__ = ("_params", "_order", "_config", "_effective_cache")

    def __init__(self, config: QcRulesConfig) -> None:
        self._config = config
        self._params: dict[str, QcParamSpec] = {spec.id: spec for spec in config.parameters}
        self._order: tuple[str, ...] = self._topological_order()
        self._effective_cache: dict[tuple[str, str], EffectiveQcSpec] = {}

    def _topological_order(self) -> tuple[str, ...]:
        """Order parameters so every QC input is computed before its consumer."""
        own = set(self._params)
        dependencies = {
            param_id: sorted(spec.transfer.inputs & own)
            for param_id, spec in self._params.items()
        }
        ordered: list[str] = []
        placed: set[str] = set()
        # Deterministic: iterate declaration order, and emit dependencies first.
        remaining = list(self._params)
        while remaining:
            progressed = False
            for param_id in list(remaining):
                if all(dep in placed for dep in dependencies[param_id]):
                    ordered.append(param_id)
                    placed.add(param_id)
                    remaining.remove(param_id)
                    progressed = True
            if not progressed:
                # The linter rejects cycles before this point; this guards against
                # a registry built from unlinted config.
                raise ValueError(
                    f"QC parameters form a dependency cycle among {sorted(remaining)}"
                )
        return tuple(ordered)

    def __len__(self) -> int:
        return len(self._params)

    def __contains__(self, param_id: object) -> bool:
        return param_id in self._params

    @property
    def evaluation_order(self) -> tuple[str, ...]:
        """Parameter ids in dependency order."""
        return self._order

    @property
    def results(self) -> tuple[str, ...]:
        return tuple(self._config.results)

    @property
    def reject_on_final_failure(self) -> bool:
        return self._config.reject_on_final_failure

    def get(self, param_id: str) -> QcParamSpec:
        try:
            return self._params[param_id]
        except KeyError:
            raise KeyError(
                f"unknown QC parameter {param_id!r}; declared: {sorted(self._params)}"
            ) from None

    def effective(self, product: ProductSpec, param_id: str) -> EffectiveQcSpec:
        """The parameter's specification as it applies to ``product``."""
        key = (product.product_id, param_id)
        cached = self._effective_cache.get(key)
        if cached is not None:
            return cached
        base = self.get(param_id)
        override = product.qc_overrides.get(param_id)
        effective = EffectiveQcSpec(
            spec=base,
            target=override.target if override and override.target is not None else base.target,
            lower_limit=(
                override.lower_limit
                if override and override.lower_limit is not None
                else base.lower_limit
            ),
            upper_limit=(
                override.upper_limit
                if override and override.upper_limit is not None
                else base.upper_limit
            ),
        )
        self._effective_cache[key] = effective
        return effective

    def for_product(self, product: ProductSpec, phase: str | None = None) -> tuple[EffectiveQcSpec, ...]:
        """A product's applicable parameters, in evaluation order.

        Filtered to the product's route, because a test whose stage never runs
        cannot be measured.
        """
        route = set(product.manufacturing_process)
        applicable = set(product.qc_specifications)
        return tuple(
            self.effective(product, param_id)
            for param_id in self._order
            if param_id in applicable
            and self._params[param_id].stage in route
            and (phase is None or self._params[param_id].phase == phase)
        )

    def reference_values(self) -> dict[str, float]:
        """Nominal value per QC parameter, used when an input is unavailable.

        Substituting the nominal keeps a transfer meaningful for a product whose
        route omits the upstream stage, instead of dropping the term and silently
        shifting the result.
        """
        return {param_id: spec.target for param_id, spec in self._params.items()}
