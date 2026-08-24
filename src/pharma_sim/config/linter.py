"""Cross-file reference integrity checks for the configuration set.

Because the factory's vocabulary is data rather than Python enums, the type
system cannot catch a reference to a state, sensor tag or failure mode that does
not exist. This module replaces that safety net: it walks every cross-file
reference and reports each dangling one with the file, the YAML path and a hint.

Run it with ``pharma_sim validate``; the simulator also runs it during startup.
"""

from __future__ import annotations

from pharma_sim.config.drivers import (
    DERIVED_SOURCES,
    HAZARD_DRIVERS,
    QC_DRIVERS,
    RCA_SIGNALS,
    SCENARIO_ACTIONS,
)
from pharma_sim.config.errors import ConfigIssue, IssueCollector
from pharma_sim.config.models import (
    FactoryConfig,
    StateRolesConfig,
)
from pharma_sim.registry.sensor_binding import SensorBindingError, resolve_sensor_specs

__all__ = ["lint_config", "ROLE_FIELDS"]

#: Role names a sensor's ``state_factors`` may key on, besides literal state ids.
ROLE_FIELDS: frozenset[str] = frozenset(
    name for name in StateRolesConfig.model_fields if name != "initial"
)


def _check_process_parameters_are_measured(config, collector) -> None:
    """Every product setpoint must be measured by some machine at that stage.

    Without this, a product can declare a setpoint for a stage whose equipment
    has no sensor flagged as a process parameter for that tag. The stage then
    records no achieved values, and every QC transfer that reads one silently
    evaluates at its intercept. The result looks like data: it is the right
    order of magnitude, it varies, and it is completely disconnected from the
    process. That is worse than a crash, and it is exactly what happened when
    the containment suite was added with the flag left off.
    """
    # Which tags each unit can measure as a process parameter.
    measurable: dict[str, set[str]] = {}
    for unit_id, groups in config.machines.layout.items():
        tags: set[str] = set()
        for group in groups:
            equipment = next(
                (item for item in config.machines.equipment_classes
                 if item.id == group.equipment_class),
                None,
            )
            if equipment is None:
                continue
            for sensor in equipment.sensors:
                if sensor.process_parameter:
                    tags.add(sensor.tag)
            if equipment.sensor_profile:
                # profiles maps a profile id straight to its list of sensors.
                for sensor in config.sensors.profiles.get(equipment.sensor_profile, ()):
                    if sensor.process_parameter:
                        tags.add(sensor.tag)
        measurable[unit_id] = tags

    stage_units: dict[str, list[str]] = {}
    for unit in config.units.units:
        stage_units.setdefault(unit.process_stage, []).append(unit.id)

    for product in config.products.products:
        for stage, parameters in product.process_parameters.items():
            units = stage_units.get(stage, [])
            available: set[str] = set()
            for unit_id in units:
                available |= measurable.get(unit_id, set())
            for name in parameters:
                if name not in available:
                    collector.add(
                        "products.yaml",
                        f"products.{product.product_id}.process_parameters.{stage}.{name}",
                        "no machine at this stage measures this parameter",
                        "flag the sensor with `process_parameter: true`, or the stage "
                        "will record no achieved value and QC will silently evaluate "
                        "its transfer at the intercept",
                    )


def _check_duplicates(
    collector: IssueCollector, file: str, path: str, ids: list[str], noun: str
) -> None:
    seen: set[str] = set()
    for value in ids:
        if value in seen:
            collector.add(
                file, path, f"duplicate {noun} id {value!r}", "ids must be unique within a file"
            )
        seen.add(value)


def _lint_states(config: FactoryConfig, collector: IssueCollector) -> set[str]:
    states = {spec.id for spec in config.states.states}
    _check_duplicates(
        collector, "states.yaml", "states", [s.id for s in config.states.states], "state"
    )

    for source, targets in config.states.transitions.items():
        if source not in states:
            collector.add(
                "states.yaml",
                f"transitions.{source}",
                f"transition source {source!r} is not a declared state",
                f"declared states: {sorted(states)}",
            )
        for target in targets:
            if target not in states:
                collector.add(
                    "states.yaml",
                    f"transitions.{source}",
                    f"transition target {target!r} is not a declared state",
                    f"declared states: {sorted(states)}",
                )

    roles = config.states.roles
    if roles.initial not in states:
        collector.add(
            "states.yaml",
            "roles.initial",
            f"initial state {roles.initial!r} is not a declared state",
            f"declared states: {sorted(states)}",
        )
    for role_name in ROLE_FIELDS:
        for state_id in getattr(roles, role_name):
            if state_id not in states:
                collector.add(
                    "states.yaml",
                    f"roles.{role_name}",
                    f"role references undeclared state {state_id!r}",
                    "every role entry must be a declared state id",
                )

    for required in ("productive", "downtime", "fault"):
        if not getattr(roles, required):
            collector.add(
                "states.yaml",
                f"roles.{required}",
                f"role {required!r} is empty",
                "production, OEE and failure handling all read this role",
            )

    unreachable = states - {roles.initial} - {
        target for targets in config.states.transitions.values() for target in targets
    }
    for state_id in sorted(unreachable):
        collector.add(
            "states.yaml",
            "transitions",
            f"state {state_id!r} is not the initial state and no transition reaches it",
            "add it as a transition target, or remove the state",
        )
    return states


def _lint_event_types(config: FactoryConfig, collector: IssueCollector) -> set[str]:
    ids = [spec.id for spec in config.event_types.event_types]
    _check_duplicates(collector, "event_types.yaml", "event_types", ids, "event type")
    severities = set(config.event_types.severities)
    for spec in config.event_types.event_types:
        if spec.default_severity not in severities:
            collector.add(
                "event_types.yaml",
                f"event_types.{spec.id}.default_severity",
                f"severity {spec.default_severity!r} is not declared",
                f"declared severities: {sorted(severities)}",
            )
    return set(ids)


def _lint_units(config: FactoryConfig, collector: IssueCollector) -> tuple[set[str], set[str]]:
    unit_ids = [spec.id for spec in config.units.units]
    _check_duplicates(collector, "units.yaml", "units", unit_ids, "unit")
    stages = {spec.process_stage for spec in config.units.units}

    sequences = [spec.sequence for spec in config.units.units]
    if len(set(sequences)) != len(sequences):
        collector.add(
            "units.yaml",
            "units.sequence",
            "unit sequence numbers are not unique",
            "sequence defines process order and must be distinct",
        )
    return set(unit_ids), stages


def _lint_machines(
    config: FactoryConfig,
    collector: IssueCollector,
    unit_ids: set[str],
    state_ids: set[str],
) -> tuple[dict[str, set[str]], set[str]]:
    """Validate equipment classes and layout; return per-class tags and all tags."""
    class_ids = [spec.id for spec in config.machines.equipment_classes]
    _check_duplicates(
        collector, "machines.yaml", "equipment_classes", class_ids, "equipment class"
    )
    classes = {spec.id: spec for spec in config.machines.equipment_classes}
    failure_mode_ids = {spec.id for spec in config.failures.failure_modes}

    class_tags: dict[str, set[str]] = {}
    all_tags: set[str] = set()

    for spec in config.machines.equipment_classes:
        try:
            resolved = resolve_sensor_specs(spec, config.sensors)
        except SensorBindingError as exc:
            collector.add(
                "machines.yaml", f"equipment_classes.{spec.id}.sensors", str(exc)
            )
            class_tags[spec.id] = set()
            continue

        if not resolved:
            collector.add(
                "machines.yaml",
                f"equipment_classes.{spec.id}",
                "equipment class has no sensors",
                "attach a sensor_profile or declare sensors inline",
            )
        class_tags[spec.id] = {sensor.tag for sensor in resolved}
        all_tags |= class_tags[spec.id]

        for sensor in resolved:
            if sensor.derived_from is not None and sensor.derived_from not in DERIVED_SOURCES:
                collector.add(
                    "sensors.yaml",
                    f"{spec.id}.{sensor.tag}.derived_from",
                    f"unknown derived source {sensor.derived_from!r}",
                    f"available sources: {sorted(DERIVED_SOURCES)}",
                )
            for key in sensor.state_factors:
                if key not in state_ids and key not in ROLE_FIELDS:
                    collector.add(
                        "sensors.yaml",
                        f"{spec.id}.{sensor.tag}.state_factors.{key}",
                        f"state_factors key {key!r} is neither a state id nor a role",
                        f"roles: {sorted(ROLE_FIELDS)}",
                    )
            for bound_name, low, high in (
                ("warn", sensor.warn_low, sensor.warn_high),
                ("alarm", sensor.alarm_low, sensor.alarm_high),
            ):
                if low is not None and high is not None and low >= high:
                    collector.add(
                        "sensors.yaml",
                        f"{spec.id}.{sensor.tag}",
                        f"{bound_name}_low ({low}) must be below {bound_name}_high ({high})",
                    )

        for mode_id in spec.failure_modes:
            if mode_id not in failure_mode_ids:
                collector.add(
                    "machines.yaml",
                    f"equipment_classes.{spec.id}.failure_modes",
                    f"unknown failure mode {mode_id!r}",
                    f"declared in failures.yaml: {sorted(failure_mode_ids)}",
                )

    for unit_id, groups in config.machines.layout.items():
        if unit_id not in unit_ids:
            collector.add(
                "machines.yaml",
                f"layout.{unit_id}",
                f"layout references unknown unit {unit_id!r}",
                f"declared units: {sorted(unit_ids)}",
            )
        prefixes: set[str] = set()
        for index, group in enumerate(groups):
            path = f"layout.{unit_id}[{index}]"
            if group.equipment_class not in classes:
                collector.add(
                    "machines.yaml",
                    path,
                    f"unknown equipment_class {group.equipment_class!r}",
                    f"declared classes: {sorted(classes)}",
                )
                continue
            if group.id_prefix in prefixes:
                collector.add(
                    "machines.yaml",
                    path,
                    f"id_prefix {group.id_prefix!r} is used twice in this unit",
                    "machine ids would collide",
                )
            prefixes.add(group.id_prefix)
            if group.commissioned_to < group.commissioned_from:
                collector.add(
                    "machines.yaml",
                    path,
                    "commissioned_to is earlier than commissioned_from",
                )
            try:
                resolve_sensor_specs(
                    classes[group.equipment_class],
                    config.sensors,
                    group.sensors,
                    origin=f"machine group {group.id_prefix!r} in {unit_id}",
                )
            except SensorBindingError as exc:
                collector.add("machines.yaml", f"{path}.sensors", str(exc))

    for unit_id in sorted(unit_ids - set(config.machines.layout)):
        collector.add(
            "machines.yaml",
            "layout",
            f"unit {unit_id!r} has no machines",
            "every declared unit needs at least one machine group",
        )

    return class_tags, all_tags


def _lint_products(
    config: FactoryConfig, collector: IssueCollector, stages: set[str], all_tags: set[str]
) -> tuple[set[str], set[str]]:
    """Validate products; return declared process-parameter names and product ids."""
    product_ids = [spec.product_id for spec in config.products.products]
    _check_duplicates(collector, "products.yaml", "products", product_ids, "product")
    qc_by_id = {spec.id: spec for spec in config.qc_rules.parameters}
    qc_ids = set(qc_by_id)
    process_parameters: set[str] = set()

    for spec in config.products.products:
        for stage in spec.manufacturing_process:
            if stage not in stages:
                collector.add(
                    "products.yaml",
                    f"products.{spec.product_id}.manufacturing_process",
                    f"stage {stage!r} is not performed by any unit",
                    f"declared unit stages: {sorted(stages)}",
                )
        route = set(spec.manufacturing_process)
        for stage, parameters in spec.process_parameters.items():
            if stage not in route:
                collector.add(
                    "products.yaml",
                    f"products.{spec.product_id}.process_parameters.{stage}",
                    f"parameters given for stage {stage!r}, which is not in this "
                    f"product's manufacturing_process",
                )
            for parameter, window in parameters.items():
                process_parameters.add(parameter)
                if parameter not in all_tags:
                    collector.add(
                        "products.yaml",
                        f"products.{spec.product_id}.process_parameters.{stage}.{parameter}",
                        f"process parameter {parameter!r} is not a sensor tag on any "
                        f"equipment class",
                        "process parameters are measured, so they need a sensor",
                    )
                low, high = window.min, window.max
                if low is not None and high is not None and low > high:
                    collector.add(
                        "products.yaml",
                        f"products.{spec.product_id}.process_parameters.{stage}.{parameter}",
                        f"min ({low}) is above max ({high})",
                    )
                if low is not None and window.target < low or high is not None and window.target > high:
                    collector.add(
                        "products.yaml",
                        f"products.{spec.product_id}.process_parameters.{stage}.{parameter}",
                        f"target {window.target} lies outside [{low}, {high}]",
                    )

        for qc_id in spec.qc_specifications:
            qc_spec = qc_by_id.get(qc_id)
            if qc_spec is None:
                collector.add(
                    "products.yaml",
                    f"products.{spec.product_id}.qc_specifications",
                    f"unknown QC parameter {qc_id!r}",
                    f"declared in qc_rules.yaml: {sorted(qc_ids)}",
                )
            elif qc_spec.stage not in route:
                collector.add(
                    "products.yaml",
                    f"products.{spec.product_id}.qc_specifications",
                    f"QC parameter {qc_id!r} belongs to stage {qc_spec.stage!r}, which "
                    f"this product's manufacturing_process does not include",
                    "a test whose stage never runs can never be measured",
                )
        if not spec.qc_specifications:
            collector.add(
                "products.yaml",
                f"products.{spec.product_id}.qc_specifications",
                "product has no QC specifications",
                "a product with no QC tests can never be released or rejected",
            )

        applicable = set(spec.qc_specifications)
        for qc_id, override in spec.qc_overrides.items():
            if qc_id not in applicable:
                collector.add(
                    "products.yaml",
                    f"products.{spec.product_id}.qc_overrides.{qc_id}",
                    f"override given for {qc_id!r}, which is not in this product's "
                    f"qc_specifications",
                    "an override for a test that never runs has no effect",
                )
                continue
            base = qc_by_id[qc_id]
            target = override.target if override.target is not None else base.target
            low = override.lower_limit if override.lower_limit is not None else base.lower_limit
            high = override.upper_limit if override.upper_limit is not None else base.upper_limit
            if low is not None and high is not None and low >= high:
                collector.add(
                    "products.yaml",
                    f"products.{spec.product_id}.qc_overrides.{qc_id}",
                    f"effective lower_limit ({low}) is not below upper_limit ({high})",
                )
            elif low is not None and target < low or high is not None and target > high:
                collector.add(
                    "products.yaml",
                    f"products.{spec.product_id}.qc_overrides.{qc_id}",
                    f"effective target {target} lies outside [{low}, {high}]",
                )
    return process_parameters, set(product_ids)


def _lint_qc_rules(
    config: FactoryConfig,
    collector: IssueCollector,
    stages: set[str],
    process_parameters: set[str],
) -> None:
    qc_ids = [spec.id for spec in config.qc_rules.parameters]
    _check_duplicates(collector, "qc_rules.yaml", "parameters", qc_ids, "QC parameter")
    known = set(qc_ids) | process_parameters | QC_DRIVERS

    for spec in config.qc_rules.parameters:
        if spec.stage not in stages:
            collector.add(
                "qc_rules.yaml",
                f"parameters.{spec.id}.stage",
                f"stage {spec.stage!r} is not performed by any unit",
                f"declared unit stages: {sorted(stages)}",
            )
        for input_name in sorted(spec.transfer.inputs):
            if input_name not in known:
                collector.add(
                    "qc_rules.yaml",
                    f"parameters.{spec.id}.transfer",
                    f"transfer input {input_name!r} is not a process parameter, "
                    f"another QC parameter, or an engine driver",
                    f"engine drivers: {sorted(QC_DRIVERS)}",
                )
        low, high = spec.lower_limit, spec.upper_limit
        if low is not None and high is not None and low >= high:
            collector.add(
                "qc_rules.yaml",
                f"parameters.{spec.id}",
                f"lower_limit ({low}) must be below upper_limit ({high})",
            )
        if low is not None and spec.target < low or high is not None and spec.target > high:
            collector.add(
                "qc_rules.yaml",
                f"parameters.{spec.id}",
                f"target {spec.target} lies outside [{low}, {high}]",
            )
        if low is None and high is None:
            collector.add(
                "qc_rules.yaml",
                f"parameters.{spec.id}",
                "parameter has neither a lower nor an upper limit",
                "a test with no limits can never fail",
            )
        if not spec.transfer.terms:
            collector.add(
                "qc_rules.yaml",
                f"parameters.{spec.id}.transfer",
                "no transfer terms: this QC value would not depend on the process",
                "QC results must be a consequence of process conditions",
            )

    for cycle in _qc_dependency_cycles(config):
        collector.add(
            "qc_rules.yaml",
            "parameters.transfer",
            f"QC parameters form a dependency cycle: {' -> '.join(cycle)}",
            "transfer inputs must form a directed acyclic graph so they can be evaluated in order",
        )


def _qc_dependency_cycles(config: FactoryConfig) -> list[list[str]]:
    """Find cycles among QC parameters that read each other's results.

    QC values are computed in dependency order; a cycle would make that order
    undefined, so it is rejected up front rather than discovered at runtime.
    """
    qc_ids = {spec.id for spec in config.qc_rules.parameters}
    graph = {
        spec.id: sorted(spec.transfer.inputs & qc_ids) for spec in config.qc_rules.parameters
    }

    cycles: list[list[str]] = []
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(graph, WHITE)

    def visit(node: str, stack: list[str]) -> None:
        colour[node] = GREY
        stack.append(node)
        for dependency in graph[node]:
            if colour[dependency] == GREY:
                start = stack.index(dependency)
                cycles.append([*stack[start:], dependency])
            elif colour[dependency] == WHITE:
                visit(dependency, stack)
        stack.pop()
        colour[node] = BLACK

    for node in sorted(graph):
        if colour[node] == WHITE:
            visit(node, [])
    return cycles


def _lint_failures(
    config: FactoryConfig,
    collector: IssueCollector,
    class_tags: dict[str, set[str]],
    severities: set[str],
) -> set[str]:
    mode_ids = [spec.id for spec in config.failures.failure_modes]
    _check_duplicates(collector, "failures.yaml", "failure_modes", mode_ids, "failure mode")
    profiles = set(config.sensors.profiles)
    classes = {spec.id: spec for spec in config.machines.equipment_classes}
    root_causes: set[str] = set()

    for spec in config.failures.failure_modes:
        root_causes.add(spec.root_cause)
        if spec.severity not in severities:
            collector.add(
                "failures.yaml",
                f"failure_modes.{spec.id}.severity",
                f"severity {spec.severity!r} is not declared in event_types.yaml",
                f"declared severities: {sorted(severities)}",
            )
        for class_id in spec.equipment_classes:
            if class_id not in classes:
                collector.add(
                    "failures.yaml",
                    f"failure_modes.{spec.id}.equipment_classes",
                    f"unknown equipment class {class_id!r}",
                    f"declared classes: {sorted(classes)}",
                )
        for profile in spec.sensor_profiles:
            if profile not in profiles:
                collector.add(
                    "failures.yaml",
                    f"failure_modes.{spec.id}.sensor_profiles",
                    f"unknown sensor profile {profile!r}",
                    f"declared profiles: {sorted(profiles)}",
                )

        applicable = _applicable_classes(spec, classes)
        if not applicable:
            collector.add(
                "failures.yaml",
                f"failure_modes.{spec.id}",
                "no equipment class matches this failure mode",
                "check equipment_classes / sensor_profiles filters",
            )
        # A detectable mode that resolves to no precursors at all on one of its
        # applicable classes is silent on that equipment: it would fault with no
        # warning and nothing to diagnose. That is almost always a mis-scoped
        # applicability filter rather than an intent.
        if spec.detectable and spec.precursors:
            declared = {p.tag for p in spec.precursors}
            for class_id in applicable:
                if not (declared & class_tags.get(class_id, set())):
                    collector.add(
                        "failures.yaml",
                        f"failure_modes.{spec.id}",
                        f"mode applies to equipment class {class_id!r} but none of its "
                        f"precursor tags {sorted(declared)} exist there, so it would "
                        f"fault silently on that equipment",
                        "narrow equipment_classes/sensor_profiles, or add the tag",
                    )

        for precursor in spec.precursors:
            carriers = [cid for cid in applicable if precursor.tag in class_tags.get(cid, set())]
            if not carriers:
                collector.add(
                    "failures.yaml",
                    f"failure_modes.{spec.id}.precursors",
                    f"precursor tag {precursor.tag!r} exists on none of the equipment "
                    f"classes this mode applies to ({sorted(applicable)})",
                    "a precursor the data never shows cannot be detected or diagnosed",
                )
            if precursor.delta_fraction == 0.0 and precursor.delta_absolute == 0.0:
                collector.add(
                    "failures.yaml",
                    f"failure_modes.{spec.id}.precursors.{precursor.tag}",
                    "precursor has neither delta_fraction nor delta_absolute",
                    "it would produce no observable signal",
                )
        if spec.detectable and not spec.precursors:
            collector.add(
                "failures.yaml",
                f"failure_modes.{spec.id}",
                "mode is marked detectable but declares no precursors",
                "either add precursors or set detectable: false",
            )

    for driver_transfer, name in (
        (config.failures.hazard_factors.age, "age"),
        (config.failures.hazard_factors.operating_hours, "operating_hours"),
        (config.failures.hazard_factors.maintenance_debt, "maintenance_debt"),
        (config.failures.hazard_factors.load, "load"),
        (config.failures.hazard_factors.environment, "environment"),
        (config.failures.hazard_factors.operator, "operator"),
    ):
        for input_name in sorted(driver_transfer.inputs):
            if input_name not in HAZARD_DRIVERS:
                collector.add(
                    "failures.yaml",
                    f"hazard_factors.{name}",
                    f"unknown hazard driver {input_name!r}",
                    f"available drivers: {sorted(HAZARD_DRIVERS)}",
                )
    return root_causes


def _applicable_classes(spec, classes: dict[str, object]) -> list[str]:
    """Equipment classes a failure mode can apply to, honouring both filters."""
    result: list[str] = []
    for class_id, class_spec in classes.items():
        if spec.equipment_classes and class_id not in spec.equipment_classes:
            continue
        profile = getattr(class_spec, "sensor_profile", None)
        if spec.sensor_profiles and profile not in spec.sensor_profiles:
            continue
        result.append(class_id)
    return result


def _lint_rca_rules(
    config: FactoryConfig,
    collector: IssueCollector,
    all_tags: set[str],
    failure_root_causes: set[str],
) -> None:
    evidence_ids = [rule.id for rule in config.rca_rules.evidence_rules]
    _check_duplicates(
        collector, "rca_rules.yaml", "evidence_rules", evidence_ids, "evidence rule"
    )
    rule_ids = [rule.id for rule in config.rca_rules.rules]
    _check_duplicates(collector, "rca_rules.yaml", "rules", rule_ids, "RCA rule")

    for rule in config.rca_rules.evidence_rules:
        if rule.tag is None and rule.signal is None:
            collector.add(
                "rca_rules.yaml",
                f"evidence_rules.{rule.id}",
                "evidence rule declares neither a sensor tag nor a signal",
            )
        if rule.tag is not None and rule.tag not in all_tags:
            collector.add(
                "rca_rules.yaml",
                f"evidence_rules.{rule.id}.tag",
                f"tag {rule.tag!r} is not a sensor on any equipment class",
            )
        if rule.signal is not None and rule.signal not in RCA_SIGNALS:
            collector.add(
                "rca_rules.yaml",
                f"evidence_rules.{rule.id}.signal",
                f"unknown RCA signal {rule.signal!r}",
                f"available signals: {sorted(RCA_SIGNALS)}",
            )

    known_evidence = set(evidence_ids)
    covered: set[str] = set()
    for rule in config.rca_rules.rules:
        covered.add(rule.root_cause)
        for evidence_id in rule.evidence:
            if evidence_id not in known_evidence:
                collector.add(
                    "rca_rules.yaml",
                    f"rules.{rule.id}.evidence",
                    f"unknown evidence rule {evidence_id!r}",
                    f"declared evidence rules: {sorted(known_evidence)}",
                )
        if not rule.evidence:
            collector.add(
                "rca_rules.yaml",
                f"rules.{rule.id}",
                "RCA rule has no evidence requirements",
                "it would match every failure",
            )

    for root_cause in sorted(failure_root_causes - covered):
        collector.add(
            "rca_rules.yaml",
            "rules",
            f"no RCA rule can ever conclude root cause {root_cause!r}",
            "a failure mode whose cause no rule covers is undiagnosable",
        )


def _lint_deviations_and_scenarios(
    config: FactoryConfig,
    collector: IssueCollector,
    event_type_ids: set[str],
    severities: set[str],
    unit_ids: set[str],
) -> None:
    _check_duplicates(
        collector,
        "deviations.yaml",
        "rules",
        [rule.id for rule in config.deviations.rules],
        "deviation rule",
    )
    for rule in config.deviations.rules:
        if rule.trigger_event not in event_type_ids:
            collector.add(
                "deviations.yaml",
                f"rules.{rule.id}.trigger_event",
                f"unknown event type {rule.trigger_event!r}",
                f"declared in event_types.yaml: {sorted(event_type_ids)}",
            )
        if rule.severity not in severities:
            collector.add(
                "deviations.yaml",
                f"rules.{rule.id}.severity",
                f"severity {rule.severity!r} is not declared",
                f"declared severities: {sorted(severities)}",
            )

    mode_ids = {spec.id for spec in config.failures.failure_modes}
    class_ids = {spec.id for spec in config.machines.equipment_classes}
    state_ids = {spec.id for spec in config.states.states}
    _check_duplicates(
        collector,
        "scenarios.yaml",
        "scenarios",
        [spec.id for spec in config.scenarios.scenarios],
        "scenario",
    )
    for scenario in config.scenarios.scenarios:
        for index, action in enumerate(scenario.actions):
            path = f"scenarios.{scenario.id}.actions[{index}]"
            if action.type not in SCENARIO_ACTIONS:
                collector.add(
                    "scenarios.yaml",
                    path,
                    f"unknown action type {action.type!r}",
                    f"available actions: {sorted(SCENARIO_ACTIONS)}",
                )
            if action.failure_mode is not None and action.failure_mode not in mode_ids:
                collector.add(
                    "scenarios.yaml",
                    f"{path}.failure_mode",
                    f"unknown failure mode {action.failure_mode!r}",
                )
            if action.equipment_class is not None and action.equipment_class not in class_ids:
                collector.add(
                    "scenarios.yaml",
                    f"{path}.equipment_class",
                    f"unknown equipment class {action.equipment_class!r}",
                )
            if action.unit_id is not None and action.unit_id not in unit_ids:
                collector.add(
                    "scenarios.yaml", f"{path}.unit_id", f"unknown unit {action.unit_id!r}"
                )
            if action.type == "force_state":
                target = action.params.get("state")
                if target not in state_ids:
                    collector.add(
                        "scenarios.yaml",
                        f"{path}.params.state",
                        f"force_state needs params.state to be a declared state, got {target!r}",
                    )
            if action.at_hours > scenario.duration_hours:
                collector.add(
                    "scenarios.yaml",
                    f"{path}.at_hours",
                    f"action fires at {action.at_hours}h, after the scenario ends "
                    f"({scenario.duration_hours}h)",
                )


def _lint_sinks(config: FactoryConfig, collector: IssueCollector) -> None:
    _check_duplicates(
        collector, "sinks.yaml", "sinks", [sink.name for sink in config.sinks.sinks], "sink"
    )
    for sink in config.sinks.sinks:
        if sink.type == "mqtt":
            for field_name, template in (
                ("telemetry_topic", sink.mqtt.telemetry_topic),
                ("event_topic", sink.mqtt.event_topic),
            ):
                unknown = _unknown_placeholders(template)
                if unknown:
                    collector.add(
                        "sinks.yaml",
                        f"sinks.{sink.name}.mqtt.{field_name}",
                        f"topic template uses unknown placeholder(s) {sorted(unknown)}",
                        "available: plant_id, unit_id, machine_id, sensor_id, event_type",
                    )


_TOPIC_PLACEHOLDERS = frozenset({"plant_id", "unit_id", "machine_id", "sensor_id", "event_type"})


def _unknown_placeholders(template: str) -> set[str]:
    import string

    found = {
        name for _, name, _, _ in string.Formatter().parse(template) if name
    }
    return found - _TOPIC_PLACEHOLDERS


def lint_config(config: FactoryConfig) -> list[ConfigIssue]:
    """Return every cross-file reference problem found in ``config``.

    An empty list means the configuration is internally consistent: every state,
    tag, stage, QC parameter, failure mode and event type referenced anywhere
    actually exists, and nothing declared is unusable.
    """
    collector = IssueCollector()

    state_ids = _lint_states(config, collector)
    event_type_ids = _lint_event_types(config, collector)
    severities = set(config.event_types.severities)
    unit_ids, stages = _lint_units(config, collector)
    class_tags, all_tags = _lint_machines(config, collector, unit_ids, state_ids)
    process_parameters, _ = _lint_products(config, collector, stages, all_tags)
    _lint_qc_rules(config, collector, stages, process_parameters)
    root_causes = _lint_failures(config, collector, class_tags, severities)
    _lint_rca_rules(config, collector, all_tags, root_causes)
    _lint_deviations_and_scenarios(config, collector, event_type_ids, severities, unit_ids)
    _lint_sinks(config, collector)
    _check_process_parameters_are_measured(config, collector)

    return collector.issues
