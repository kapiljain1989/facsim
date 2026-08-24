"""Loading and validation of ``config/lab/*.yaml``.

Mirrors :mod:`pharma_sim.config.loader`: every problem across every file is
collected before raising, so one run reports the full picture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from pharma_sim.config.errors import ConfigError, IssueCollector
from pharma_sim.lab.config import LAB_CONFIG_FILES, Conditions, LabConfig

__all__ = ["load_lab_config"]


def _field_model(field_name: str) -> type[BaseModel]:
    annotation = LabConfig.model_fields[field_name].annotation
    assert isinstance(annotation, type) and issubclass(annotation, BaseModel)
    return annotation


def _read_yaml(path: Path, collector: IssueCollector) -> dict[str, Any] | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        collector.add(path.name, "", f"YAML is not parseable: {exc}", "check indentation")
        return None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        collector.add(
            path.name,
            "",
            f"top level must be a mapping, found {type(raw).__name__}",
            "every config file starts with 'key: value' entries",
        )
        return None
    return raw


def _describe(exc: ValidationError, file_name: str, collector: IssueCollector) -> None:
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        hint = ""
        if error["type"] == "extra_forbidden":
            hint = "unknown key — check spelling against the schema"
        elif error["type"] == "missing":
            hint = "required key is absent"
        collector.add(file_name, location, error["msg"], hint)


def load_lab_config(config_dir: str | Path) -> LabConfig:
    """Load every laboratory configuration file in ``config_dir``.

    Raises:
        ConfigError: if a file is missing, unparseable, fails validation, or
            holds a reference that does not resolve.
    """
    directory = Path(config_dir)
    collector = IssueCollector()

    if not directory.is_dir():
        raise ConfigError([], f"laboratory configuration directory not found: {directory}")

    sections: dict[str, BaseModel] = {}
    for stem, field_name in LAB_CONFIG_FILES.items():
        model = _field_model(field_name)
        path = directory / f"{stem}.yaml"
        if not path.exists():
            path = directory / f"{stem}.yml"
        if not path.exists():
            collector.add(f"{stem}.yaml", "", "required configuration file is missing",
                          f"create {directory / (stem + '.yaml')}")
            continue
        raw = _read_yaml(path, collector)
        if raw is None:
            continue
        try:
            sections[field_name] = model.model_validate(raw)
        except ValidationError as exc:
            _describe(exc, path.name, collector)

    collector.raise_if_any(f"laboratory configuration in {directory} is invalid")

    try:
        config = LabConfig(**sections)  # type: ignore[arg-type]
    except ValidationError as exc:
        _describe(exc, "<assembled>", collector)
        collector.raise_if_any(f"laboratory configuration in {directory} is invalid")
        raise

    _lint(config, collector)
    collector.raise_if_any(f"laboratory configuration in {directory} is inconsistent")
    return config


def _lint(config: LabConfig, collector: IssueCollector) -> None:
    """Cross-file reference checks.

    The type system cannot catch these, because every identifier is a plain
    string by design. This is the safety net that makes that design workable.
    """
    substances = {s.substance_id for s in config.substances.substances}
    methods = {m.method_id for m in config.methods.methods}
    instruments = {i.instrument_id for i in config.instruments.instruments}
    analysts = {a.analyst_id for a in config.instruments.analysts}
    columns = {c.column_id for c in config.instruments.columns}
    column_types = {c.column_type_id for c in config.instruments.columns}

    for substance in config.substances.substances:
        if substance.parent and substance.parent not in substances:
            collector.add("substances.yaml", f"{substance.substance_id}.parent",
                          f"unknown substance {substance.parent}", "")
        for pathway in substance.degradation_pathways:
            if pathway.product not in substances:
                collector.add("substances.yaml",
                              f"{substance.substance_id}.degradation_pathways",
                              f"unknown product {pathway.product}", "")

    for method in config.methods.methods:
        if method.column.column_type_id not in column_types:
            collector.add("methods.yaml", f"{method.method_id}.column.column_type_id",
                          f"no column of type {method.column.column_type_id} exists",
                          "declare one under `columns:` in instruments.yaml")
        conditions = set(Conditions.model_fields)
        for analyte in method.analytes:
            if analyte.analyte_id not in substances:
                collector.add("methods.yaml", f"{method.method_id}.{analyte.analyte_id}",
                              "analyte is not a declared substance", "")
            for label, sensitivity in (
                ("retention_sensitivity", analyte.retention_sensitivity),
                ("efficiency_sensitivity", analyte.efficiency_sensitivity),
                ("tailing_sensitivity", analyte.tailing_sensitivity),
            ):
                for factor in sensitivity:
                    if factor not in conditions:
                        collector.add(
                            "methods.yaml",
                            f"{method.method_id}.{analyte.analyte_id}.{label}",
                            f"{factor} is not a chromatographic condition",
                            f"one of: {', '.join(sorted(conditions))}",
                        )

    for method_id, spec in config.cds.system_suitability.items():
        if method_id not in methods:
            collector.add("cds.yaml", f"system_suitability.{method_id}",
                          "unknown method", "")
            continue
        method = config.methods.by_id(method_id)
        assert method is not None
        declared = {a.analyte_id for a in method.analytes}
        for criterion in spec.criteria:
            for reference in (criterion.analyte_id, criterion.versus):
                if reference is not None and reference not in declared:
                    collector.add("cds.yaml", f"system_suitability.{method_id}",
                                  f"{reference} is not an analyte of {method_id}", "")

    for method_id, mixture in config.cds.resolution_solution.items():
        if method_id not in methods:
            collector.add("cds.yaml", f"resolution_solution.{method_id}",
                          "unknown method", "")
            continue
        method = config.methods.by_id(method_id)
        assert method is not None
        declared = {a.analyte_id for a in method.analytes}
        for analyte_id in mixture:
            if analyte_id not in declared:
                collector.add("cds.yaml", f"resolution_solution.{method_id}",
                              f"{analyte_id} is not an analyte of {method_id}", "")

    for validation in config.validations.validations:
        if validation.method_id not in methods:
            collector.add("validation.yaml", f"{validation.validation_id}.method_id",
                          f"unknown method {validation.method_id}", "")
        if validation.instrument_id not in instruments:
            collector.add("validation.yaml", f"{validation.validation_id}.instrument_id",
                          f"unknown instrument {validation.instrument_id}", "")
        if validation.lead_analyst not in analysts:
            collector.add("validation.yaml", f"{validation.validation_id}.lead_analyst",
                          f"unknown analyst {validation.lead_analyst}", "")
        if validation.column_id not in columns:
            collector.add("validation.yaml", f"{validation.validation_id}.column_id",
                          f"unknown column {validation.column_id}", "")

        method = config.methods.by_id(validation.method_id)
        robustness = validation.experiments.robustness
        if method is not None and robustness is not None:
            conditions = set(Conditions.model_fields)
            for condition in robustness.conditions:
                if condition.factor not in conditions:
                    collector.add(
                        "validation.yaml",
                        f"{validation.validation_id}.robustness",
                        f"{condition.factor} is not a chromatographic condition",
                        f"one of: {', '.join(sorted(conditions))}",
                    )
        precision = validation.experiments.intermediate_precision
        if precision is not None:
            if precision.analyst_id and precision.analyst_id not in analysts:
                collector.add("validation.yaml",
                              f"{validation.validation_id}.intermediate_precision.analyst_id",
                              f"unknown analyst {precision.analyst_id}", "")
            if precision.instrument_id and precision.instrument_id not in instruments:
                collector.add("validation.yaml",
                              f"{validation.validation_id}.intermediate_precision.instrument_id",
                              f"unknown instrument {precision.instrument_id}", "")
