"""Unit, process-stage and product registry."""

from __future__ import annotations

from pharma_sim.config.models import (
    ProductsConfig,
    ProductSpec,
    UnitSpec,
    UnitsConfig,
)

__all__ = ["TopologyRegistry"]


class TopologyRegistry:
    """Units in process order, the stages they perform, and the product catalogue."""

    __slots__ = ("_units", "_order", "_stage_units", "_products", "_config", "_products_config")

    def __init__(self, units: UnitsConfig, products: ProductsConfig) -> None:
        self._config = units
        self._products_config = products
        ordered = sorted(units.units, key=lambda spec: spec.sequence)
        self._units: dict[str, UnitSpec] = {spec.id: spec for spec in ordered}
        self._order: tuple[str, ...] = tuple(spec.id for spec in ordered)

        stage_units: dict[str, list[str]] = {}
        for spec in ordered:
            stage_units.setdefault(spec.process_stage, []).append(spec.id)
        self._stage_units: dict[str, tuple[str, ...]] = {
            stage: tuple(unit_ids) for stage, unit_ids in stage_units.items()
        }
        self._products: dict[str, ProductSpec] = {
            spec.product_id: spec for spec in products.products
        }

    # ------------------------------------------------------------------- units
    @property
    def unit_ids(self) -> tuple[str, ...]:
        """Unit ids in process sequence order."""
        return self._order

    def __len__(self) -> int:
        return len(self._units)

    def unit(self, unit_id: str) -> UnitSpec:
        try:
            return self._units[unit_id]
        except KeyError:
            raise KeyError(
                f"unknown unit {unit_id!r}; declared: {sorted(self._units)}"
            ) from None

    def units(self) -> tuple[UnitSpec, ...]:
        return tuple(self._units[unit_id] for unit_id in self._order)

    # ------------------------------------------------------------------ stages
    @property
    def stages(self) -> tuple[str, ...]:
        """Process stages in the order the units performing them appear."""
        seen: list[str] = []
        for unit_id in self._order:
            stage = self._units[unit_id].process_stage
            if stage not in seen:
                seen.append(stage)
        return tuple(seen)

    def units_for_stage(self, stage: str) -> tuple[str, ...]:
        return self._stage_units.get(stage, ())

    def stage_of(self, unit_id: str) -> str:
        return self.unit(unit_id).process_stage

    # ---------------------------------------------------------------- products
    @property
    def product_ids(self) -> tuple[str, ...]:
        return tuple(self._products)

    @property
    def max_concurrent_batches(self) -> int:
        return self._products_config.max_concurrent_batches

    def product(self, product_id: str) -> ProductSpec:
        try:
            return self._products[product_id]
        except KeyError:
            raise KeyError(
                f"unknown product {product_id!r}; declared: {sorted(self._products)}"
            ) from None

    def products(self) -> tuple[ProductSpec, ...]:
        return tuple(self._products.values())

    def demand_weights(self) -> tuple[tuple[str, float], ...]:
        """Product ids with their relative order share, for demand sampling."""
        return tuple((spec.product_id, spec.demand_weight) for spec in self._products.values())

    # -------------------------------------------------------------------- roles
    @property
    def worker_roles(self) -> tuple[str, ...]:
        return tuple(self._config.worker_roles)

    @property
    def manager_role(self) -> str:
        return self._config.manager_role

    @property
    def technician_role(self) -> str:
        return self._config.technician_role

    @property
    def qc_analyst_role(self) -> str:
        return self._config.qc_analyst_role

    @property
    def skill_levels(self) -> tuple[str, ...]:
        return tuple(self._config.skill_levels)

    @property
    def tenure_years(self) -> tuple[float, float]:
        """Shortest and longest site tenure, in years."""
        return (self._config.tenure_years_min, self._config.tenure_years_max)
