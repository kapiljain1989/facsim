"""Loading and validation of ``config/clinical/*.yaml``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from pharma_sim.config.errors import ConfigError, IssueCollector
from pharma_sim.clinical.config import CLINICAL_CONFIG_FILES, ClinicalConfig

__all__ = ["load_clinical_config"]


def _field_model(field_name: str) -> type[BaseModel]:
    annotation = ClinicalConfig.model_fields[field_name].annotation
    assert isinstance(annotation, type) and issubclass(annotation, BaseModel)
    return annotation


def load_clinical_config(config_dir: str | Path) -> ClinicalConfig:
    directory = Path(config_dir)
    collector = IssueCollector()
    if not directory.is_dir():
        raise ConfigError([], f"clinical configuration directory not found: {directory}")

    sections: dict[str, BaseModel] = {}
    for stem, field_name in CLINICAL_CONFIG_FILES.items():
        path = directory / f"{stem}.yaml"
        if not path.exists():
            collector.add(f"{stem}.yaml", "", "required configuration file is missing",
                          f"create {path}")
            continue
        try:
            raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            collector.add(path.name, "", f"YAML is not parseable: {exc}", "check indentation")
            continue
        try:
            sections[field_name] = _field_model(field_name).model_validate(raw)
        except ValidationError as exc:
            for error in exc.errors():
                location = ".".join(str(part) for part in error["loc"]) or "<root>"
                hint = ""
                if error["type"] == "extra_forbidden":
                    hint = "unknown key — check spelling against the schema"
                elif error["type"] == "missing":
                    hint = "required key is absent"
                collector.add(path.name, location, error["msg"], hint)

    collector.raise_if_any(f"clinical configuration in {directory} is invalid")
    config = ClinicalConfig(**sections)  # type: ignore[arg-type]
    _lint(config, collector)
    collector.raise_if_any(f"clinical configuration in {directory} is inconsistent")
    return config


def _lint(config: ClinicalConfig, collector: IssueCollector) -> None:
    """Cross-field checks the type system cannot make."""
    tumour = config.tumour
    protocol = config.protocol

    declared_arms = set(tumour.growth.arms)
    for arm in protocol.arms:
        if arm.arm_id not in declared_arms:
            collector.add(
                "tumour.yaml", f"growth.arms.{arm.arm_id}",
                f"no growth parameters for arm {arm.arm_id} declared in protocol.yaml",
                f"declared arms are: {', '.join(sorted(declared_arms))}",
            )
    protocol_arms = {arm.arm_id for arm in protocol.arms}
    for arm_id in declared_arms - protocol_arms:
        collector.add(
            "tumour.yaml", f"growth.arms.{arm_id}",
            "growth parameters for an arm the protocol does not have", "",
        )

    evaluators = {reader.reader_id for reader in tumour.measurement.readers}
    for endpoint in [*protocol.endpoints.primary, *protocol.endpoints.secondary]:
        if endpoint.evaluator not in evaluators:
            collector.add(
                "protocol.yaml", f"endpoints.{endpoint.endpoint_id}.evaluator",
                f"unknown evaluator {endpoint.evaluator}",
                f"declared readers are: {', '.join(sorted(evaluators))}",
            )

    if not tumour.organs:
        collector.add("tumour.yaml", "organs", "at least one organ must be declared", "")
    if not any(organ.nodal for organ in tumour.organs):
        collector.add(
            "tumour.yaml", "organs",
            "no nodal site declared",
            "RECIST treats nodes differently, so at least one nodal organ is expected",
        )

    roles = {reader.role for reader in tumour.measurement.readers}
    if "INVESTIGATOR" not in roles:
        collector.add("tumour.yaml", "measurement.readers",
                      "no reader with role INVESTIGATOR", "")

    baseline = tumour.baseline
    for name, distribution in (
        ("measurable_lesion_count", baseline.measurable_lesion_count),
        ("target_lesion_count", baseline.target_lesion_count),
        ("non_target_lesion_count", baseline.non_target_lesion_count),
    ):
        if not distribution.minimum <= distribution.mode <= distribution.maximum:
            collector.add("tumour.yaml", f"baseline.{name}",
                          "mode must lie between minimum and maximum", "")
    if baseline.target_lesion_count.maximum > tumour.recist.max_target_lesions:
        collector.add(
            "tumour.yaml", "baseline.target_lesion_count.maximum",
            f"exceeds recist.max_target_lesions ({tumour.recist.max_target_lesions})",
            "RECIST 1.1 permits at most five target lesions",
        )
    if baseline.diameter_mm.minimum < tumour.recist.measurable_min_mm:
        collector.add(
            "tumour.yaml", "baseline.diameter_mm.minimum",
            f"below recist.measurable_min_mm ({tumour.recist.measurable_min_mm})",
            "a target lesion has to be measurable at baseline",
        )
    if baseline.nodal_short_axis_mm.minimum < tumour.recist.nodal_measurable_min_mm:
        collector.add(
            "tumour.yaml", "baseline.nodal_short_axis_mm.minimum",
            f"below recist.nodal_measurable_min_mm "
            f"({tumour.recist.nodal_measurable_min_mm})",
            "a nodal target lesion has to be measurable at baseline",
        )

    if baseline.measurable_lesion_count.maximum < baseline.target_lesion_count.maximum:
        collector.add(
            "tumour.yaml", "baseline.measurable_lesion_count.maximum",
            "fewer measurable lesions than target lesions",
            "a reader cannot select more targets than the subject has lesions",
        )

    schedule = tumour.assessment_schedule
    if schedule.switch_week <= schedule.first_week:
        collector.add("tumour.yaml", "assessment_schedule.switch_week",
                      "must be later than first_week", "")
