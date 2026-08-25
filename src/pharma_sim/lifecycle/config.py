"""Pydantic models for ``config/lifecycle/links.yaml``."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Ident = Annotated[str, Field(min_length=1, max_length=64)]
Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
Positive = Annotated[float, Field(gt=0.0)]

__all__ = [
    "LifecycleConfig",
    "DrugProduct",
    "QcMethodLink",
    "ImpConfig",
    "ArmProduct",
    "load_lifecycle_config",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Normal(Strict):
    mean: float
    sd: float


class DrugProduct(Strict):
    product_id: Ident
    role: str
    description: str
    strength_mg: float
    formulation: Ident


class SetpointLink(Strict):
    process_parameter: Ident
    doe_factor: Ident


class ProcessDevelopment(Strict):
    doe_study: Ident
    formulation: Ident
    product_id: Ident
    setpoints: list[SetpointLink]


class QcMethodLink(Strict):
    qc_parameter: Ident
    method_id: Ident
    analyte_id: Ident


class ShipmentConfig(Strict):
    initial_kits_per_site: int
    resupply_kits: int
    resupply_trigger_kits: int
    lead_time_days: Normal
    temperature_excursion_probability: Fraction


class ImpConfig(Strict):
    tablets_per_kit: int
    kits_per_lot: int
    lot_expiry_months: int
    shelf_life_protocol: Ident | None = None
    releasable_dispositions: list[str]
    shipment: ShipmentConfig


class ArmProduct(Strict):
    arm_id: Ident
    role: str


class RandomisationConfig(Strict):
    kits_per_subject_per_cycle: int
    blinded_kit_pool: bool = True
    arm_products: list[ArmProduct] = Field(default_factory=list)

    def role_for(self, arm_id: str) -> str | None:
        return next((a.role for a in self.arm_products if a.arm_id == arm_id), None)


class StubBatches(Strict):
    count: int
    batch_size_tablets: int
    first_release_offset_weeks: float
    release_interval_weeks: Positive


class LifecycleConfig(Strict):
    substance: Ident
    drug_products: list[DrugProduct]
    process_development: ProcessDevelopment
    qc_methods: list[QcMethodLink]
    imp: ImpConfig
    randomisation: RandomisationConfig
    stub_batches: StubBatches

    def product(self, role: str) -> DrugProduct | None:
        return next((p for p in self.drug_products if p.role == role), None)


def load_lifecycle_config(path):
    """Load and validate the links file."""
    from pathlib import Path

    import yaml
    from pydantic import ValidationError

    from pharma_sim.config.errors import ConfigError, IssueCollector

    directory = Path(path)
    file = directory / "links.yaml" if directory.is_dir() else directory
    collector = IssueCollector()
    if not file.exists():
        raise ConfigError([], f"lifecycle links not found: {file}")
    raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    try:
        config = LifecycleConfig.model_validate(raw)
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "<root>"
            collector.add(file.name, location, error["msg"], "")
        collector.raise_if_any(f"{file} is invalid")
        raise

    declared = {product.role for product in config.drug_products}
    for mapping in config.randomisation.arm_products:
        if mapping.role not in declared:
            collector.add(
                file.name, f"randomisation.arm_products.{mapping.arm_id}",
                f"role {mapping.role} is not a declared drug product role",
                f"declared: {', '.join(sorted(declared))}",
            )

    roles = [product.role for product in config.drug_products]
    if "ACTIVE" not in roles:
        collector.add(file.name, "drug_products", "no product with role ACTIVE", "")
    if "PLACEBO" not in roles:
        collector.add(
            file.name, "drug_products",
            "no product with role PLACEBO",
            "a double-blind study needs a matching placebo to pack against",
        )
    collector.raise_if_any(f"{file} is inconsistent")
    return config
