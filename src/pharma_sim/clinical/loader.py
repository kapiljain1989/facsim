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

    _lint_sites(config, collector)
    _lint_crf(config, collector)
    _lint_monitoring(config, collector)
    _lint_tmf(config, collector)


def _lint_sites(config: ClinicalConfig, collector: IssueCollector) -> None:
    sites = config.sites
    countries = {country.country for country in sites.countries}
    archetypes = {archetype.archetype for archetype in sites.archetypes}
    milestones = {milestone.milestone for milestone in sites.milestones}

    for site in sites.sites:
        if site.country not in countries:
            collector.add("sites.yaml", f"sites.{site.site_id}.country",
                          f"unknown country {site.country}", "")
        if site.archetype not in archetypes:
            collector.add("sites.yaml", f"sites.{site.site_id}.archetype",
                          f"unknown archetype {site.archetype}",
                          f"declared: {', '.join(sorted(archetypes))}")

    seen: set[str] = set()
    for milestone in sites.milestones:
        predecessors = milestone.predecessor
        if predecessors is None:
            names: list[str] = []
        elif isinstance(predecessors, str):
            names = [predecessors]
        else:
            names = list(predecessors)
        for name in names:
            if name not in milestones:
                collector.add("sites.yaml", f"milestones.{milestone.milestone}.predecessor",
                              f"unknown milestone {name}", "")
            elif name not in seen:
                # Declaration order is the resolution order, so a milestone whose
                # predecessor comes later can never be computed.
                collector.add(
                    "sites.yaml", f"milestones.{milestone.milestone}.predecessor",
                    f"{name} is declared after this milestone",
                    "list milestones in the order they occur",
                )
        if milestone.weeks is None and milestone.source is None:
            collector.add("sites.yaml", f"milestones.{milestone.milestone}",
                          "needs either weeks or source", "")
        seen.add(milestone.milestone)


def _lint_crf(config: ClinicalConfig, collector: IssueCollector) -> None:
    crf = config.crf
    forms = {form.form_id: form for form in crf.forms}
    all_items = {item.item_id for form in crf.forms for item in form.items}

    for form in crf.forms:
        for item in form.items:
            if item.codelist and item.codelist not in crf.codelists:
                collector.add("crf.yaml", f"forms.{form.form_id}.{item.item_id}.codelist",
                              f"unknown codelist {item.codelist}",
                              f"declared: {', '.join(sorted(crf.codelists))}")

    for item_id in sorted(all_items):
        if item_id not in crf.value_sources:
            collector.add("crf.yaml", f"value_sources.{item_id}",
                          "no value source declared for this item",
                          "every item on a form needs one, or the form is blank")
    for item_id, source in crf.value_sources.items():
        if item_id not in all_items:
            collector.add("crf.yaml", f"value_sources.{item_id}",
                          "value source for an item that is not on any form", "")
        if source.kind == "WEIGHTED" and not source.weights:
            collector.add("crf.yaml", f"value_sources.{item_id}",
                          "a WEIGHTED source needs weights", "")
        if source.kind == "CONSTANT" and source.value is None:
            collector.add("crf.yaml", f"value_sources.{item_id}",
                          "a CONSTANT source needs value", "")
        if source.kind == "FROM_STUDY" and source.field is None:
            collector.add("crf.yaml", f"value_sources.{item_id}",
                          "a FROM_STUDY source needs field", "")
        if source.kind == "NORMAL" and (source.mean is None or source.sd is None):
            collector.add("crf.yaml", f"value_sources.{item_id}",
                          "a NORMAL source needs mean and sd", "")

    for check in crf.edit_checks:
        form = forms.get(check.form_id)
        if form is None:
            collector.add("crf.yaml", f"edit_checks.{check.check_id}.form_id",
                          f"unknown form {check.form_id}", "")
            continue
        if form.item(check.item_id) is None:
            collector.add("crf.yaml", f"edit_checks.{check.check_id}.item_id",
                          f"{check.item_id} is not an item on form {check.form_id}", "")
        if check.kind == "RANGE" and (check.low is None or check.high is None):
            collector.add("crf.yaml", f"edit_checks.{check.check_id}",
                          "a RANGE check needs both low and high", "")
        if check.kind == "EXPECTED_VALUE" and check.expected is None:
            collector.add("crf.yaml", f"edit_checks.{check.check_id}",
                          "an EXPECTED_VALUE check needs expected", "")
        if check.kind == "DATE_ORDER":
            if check.before is None:
                collector.add("crf.yaml", f"edit_checks.{check.check_id}",
                              "a DATE_ORDER check needs before", "")
            elif check.before not in all_items:
                collector.add("crf.yaml", f"edit_checks.{check.check_id}.before",
                              f"unknown item {check.before}", "")


def _lint_monitoring(config: ClinicalConfig, collector: IssueCollector) -> None:
    monitoring = config.monitoring
    milestones = {milestone.milestone for milestone in config.sites.milestones}
    all_items = {item.item_id for form in config.crf.forms for item in form.items}

    for visit_type in monitoring.visit_types:
        if visit_type.trigger == "MILESTONE":
            if visit_type.milestone not in milestones:
                collector.add("monitoring.yaml", f"visit_types.{visit_type.visit_type}.milestone",
                              f"unknown milestone {visit_type.milestone}", "")
        if visit_type.trigger == "PERIODIC" and visit_type.interval_weeks is None:
            collector.add("monitoring.yaml", f"visit_types.{visit_type.visit_type}",
                          "a PERIODIC visit type needs interval_weeks", "")

    for item_id in monitoring.source_data_verification.critical_items:
        if item_id not in all_items:
            collector.add(
                "monitoring.yaml", "source_data_verification.critical_items",
                f"{item_id} is not an item on any form",
                "critical data has to exist before it can be verified",
            )


def _lint_tmf(config: ClinicalConfig, collector: IssueCollector) -> None:
    tmf = config.tmf
    zones = {zone.zone for zone in tmf.zones}
    milestones = {milestone.milestone for milestone in config.sites.milestones}
    #: Points in the study's life an artifact can become expected, beyond the
    #: site milestone chain.
    study_events = {
        "STUDY_START", "END_OF_STUDY", "DATABASE_LOCK", "AMENDMENT", "IDMC_REVIEW",
    }
    levels = {"TRIAL", "COUNTRY", "SITE", "MONITORING_VISIT"}

    for artifact in tmf.artifacts:
        if artifact.zone not in zones:
            collector.add("tmf_model.yaml", f"artifacts.{artifact.artifact}.zone",
                          f"unknown zone {artifact.zone}", "")
        if artifact.level not in levels:
            collector.add("tmf_model.yaml", f"artifacts.{artifact.artifact}.level",
                          f"unknown level {artifact.level}",
                          f"one of: {', '.join(sorted(levels))}")
        if artifact.level == "MONITORING_VISIT":
            continue
        if artifact.expected_at is None:
            collector.add("tmf_model.yaml", f"artifacts.{artifact.artifact}",
                          "needs expected_at unless its level is MONITORING_VISIT", "")
        elif artifact.expected_at not in milestones | study_events:
            collector.add(
                "tmf_model.yaml", f"artifacts.{artifact.artifact}.expected_at",
                f"{artifact.expected_at} is neither a site milestone nor a study event",
                f"study events are: {', '.join(sorted(study_events))}",
            )
