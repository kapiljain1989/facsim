"""Loading and fingerprinting of the YAML configuration set."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from pharma_sim.config.errors import ConfigError, IssueCollector
from pharma_sim.config.models import (
    CONFIG_FILES,
    OPTIONAL_CONFIG_FILES,
    FactoryConfig,
)

__all__ = ["load_config", "config_fingerprint", "diff_fingerprints", "canonical_payload"]

logger = logging.getLogger(__name__)


def _field_model(field_name: str) -> type[BaseModel]:
    annotation = FactoryConfig.model_fields[field_name].annotation
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


def _describe_validation_error(
    exc: ValidationError, file_name: str, collector: IssueCollector
) -> None:
    for error in exc.errors():
        path = ".".join(str(part) for part in error["loc"]) or "<root>"
        message = error["msg"]
        hint = ""
        if error["type"] == "extra_forbidden":
            hint = "unknown key — check spelling against the schema"
        elif error["type"] == "missing":
            hint = "required key is absent"
        collector.add(file_name, path, message, hint)


def load_config(config_dir: str | Path) -> FactoryConfig:
    """Load, validate and assemble every configuration file in ``config_dir``.

    Every problem across every file is collected before raising, so a single run
    reports the full picture instead of one error at a time.

    Raises:
        ConfigError: if a file is missing, unparseable or fails validation.
    """
    directory = Path(config_dir)
    collector = IssueCollector()

    if not directory.is_dir():
        raise ConfigError(
            [], f"configuration directory not found: {directory}"
        ) from FileNotFoundError(directory)

    sections: dict[str, BaseModel] = {}
    for stem, field_name in CONFIG_FILES.items():
        model = _field_model(field_name)
        path = directory / f"{stem}.yaml"
        if not path.exists():
            path = directory / f"{stem}.yml"
        if not path.exists():
            if stem in OPTIONAL_CONFIG_FILES:
                sections[field_name] = model()
                continue
            collector.add(
                f"{stem}.yaml",
                "",
                "required configuration file is missing",
                f"create {directory / (stem + '.yaml')}",
            )
            continue

        raw = _read_yaml(path, collector)
        if raw is None:
            continue
        try:
            sections[field_name] = model.model_validate(raw)
        except ValidationError as exc:
            _describe_validation_error(exc, path.name, collector)

    collector.raise_if_any(f"configuration in {directory} is invalid")

    try:
        config = FactoryConfig(**sections)  # type: ignore[arg-type]
        _apply_env_overrides(config)
        return config
    except ValidationError as exc:  # pragma: no cover - defensive
        _describe_validation_error(exc, "<assembled>", collector)
        collector.raise_if_any(f"configuration in {directory} is invalid")
        raise


#: Environment overrides, for containers and CI where editing YAML is awkward.
#: Deliberately limited to deployment concerns — endpoints, seeds, cadence —
#: rather than anything that changes the factory's behaviour, which belongs in
#: version-controlled configuration.
_ENV_OVERRIDES: dict[str, tuple[str, str, type]] = {
    "PHARMA_SEED": ("plant.simulation", "seed", int),
    "PHARMA_SENSOR_INTERVAL_S": ("plant.simulation", "sensor_sample_interval_s", float),
    "PHARMA_TRANSACTIONAL_BACKEND": ("storage.transactional", "backend", str),
    "PHARMA_TRANSACTIONAL_DSN": ("storage.transactional", "dsn", str),
    "PHARMA_TIMESERIES_BACKEND": ("storage.timeseries", "backend", str),
    "PHARMA_TIMESERIES_DSN": ("storage.timeseries", "dsn", str),
    "PHARMA_TIMESERIES_DATABASE": ("storage.timeseries", "database", str),
    "PHARMA_EVALUATION_BACKEND": ("storage.evaluation", "backend", str),
    "PHARMA_EVALUATION_DSN": ("storage.evaluation", "dsn", str),
}


def _resolve(config: FactoryConfig, path: str) -> Any:
    target: Any = config
    for part in path.split("."):
        target = getattr(target, part)
    return target


def _apply_env_overrides(config: FactoryConfig) -> list[str]:
    """Apply ``PHARMA_*`` environment overrides to the loaded config.

    Returns the names applied, so a caller can report them: an override that
    silently changed where data landed would be a debugging trap.
    """
    import os

    applied: list[str] = []
    for variable, (path, field, caster) in _ENV_OVERRIDES.items():
        raw = os.environ.get(variable)
        if raw is None or raw == "":
            continue
        target = _resolve(config, path)
        # Config models are frozen; deployment overrides go through explicitly.
        object.__setattr__(target, field, caster(raw))
        applied.append(variable)

    host = os.environ.get("PHARMA_MQTT_HOST")
    if host:
        for sink in config.sinks.sinks:
            if sink.type == "mqtt":
                object.__setattr__(sink.mqtt, "host", host)
        applied.append("PHARMA_MQTT_HOST")

    if applied:
        logger.info("applied environment overrides: %s", ", ".join(applied))
    return applied


def canonical_payload(config: FactoryConfig) -> str:
    """Serialise config deterministically for hashing and diffing."""
    return json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def config_fingerprint(config: FactoryConfig) -> str:
    """Stable SHA-256 over the whole configuration.

    Identical configuration always yields the same fingerprint, so runs can be
    tagged and compared even after the config evolves.
    """
    return hashlib.sha256(canonical_payload(config).encode("utf-8")).hexdigest()


def _flatten(prefix: str, value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, sub in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), sub, out)
    elif isinstance(value, list):
        out[prefix] = f"<{len(value)} items>"
        for index, sub in enumerate(value):
            _flatten(f"{prefix}[{index}]", sub, out)
    else:
        out[prefix] = value


def diff_fingerprints(previous: FactoryConfig, current: FactoryConfig) -> list[str]:
    """Human-readable summary of what changed between two configurations."""
    old: dict[str, Any] = {}
    new: dict[str, Any] = {}
    _flatten("", previous.model_dump(mode="json"), old)
    _flatten("", current.model_dump(mode="json"), new)

    changes: list[str] = []
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key, "<absent>"), new.get(key, "<absent>")
        if before != after:
            changes.append(f"{key}: {before} -> {after}")
    return changes
