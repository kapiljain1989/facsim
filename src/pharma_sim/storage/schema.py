"""Single source of truth for the relational schema.

One declaration drives SQLite DDL, PostgreSQL DDL, the schema reconciler and the
CSV/Parquet export column order. Writing it once is the only way three backends
stay genuinely equivalent rather than drifting apart.

``TABLE_ORDER`` matters: foreign keys are enforced, so dimensions are flushed
before the facts that reference them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["Column", "Table", "TABLES", "TABLE_ORDER", "EVAL_TABLES", "TELEMETRY_TABLE"]

ColumnType = Literal["TEXT", "INTEGER", "REAL", "BOOLEAN", "TIMESTAMP", "DATE", "JSON"]


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: ColumnType
    nullable: bool = True
    primary_key: bool = False
    references: str | None = None  # "table.column"

    def sqlite_type(self) -> str:
        return {
            "TEXT": "TEXT",
            "INTEGER": "INTEGER",
            "REAL": "REAL",
            "BOOLEAN": "INTEGER",
            "TIMESTAMP": "TEXT",
            "DATE": "TEXT",
            "JSON": "TEXT",
        }[self.type]

    def postgres_type(self) -> str:
        return {
            "TEXT": "TEXT",
            "INTEGER": "BIGINT",
            "REAL": "DOUBLE PRECISION",
            "BOOLEAN": "BOOLEAN",
            "TIMESTAMP": "TIMESTAMPTZ",
            "DATE": "DATE",
            "JSON": "JSONB",
        }[self.type]


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    #: Composite key when no single column is the primary key.
    composite_key: tuple[str, ...] = ()
    indexes: tuple[tuple[str, ...], ...] = ()

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(column.name for column in self.columns)

    @property
    def key_columns(self) -> tuple[str, ...]:
        if self.composite_key:
            return self.composite_key
        return tuple(column.name for column in self.columns if column.primary_key)

    def column(self, name: str) -> Column | None:
        for column in self.columns:
            if column.name == name:
                return column
        return None


def _c(
    name: str,
    type_: ColumnType,
    *,
    null: bool = True,
    pk: bool = False,
    fk: str | None = None,
) -> Column:
    return Column(name=name, type=type_, nullable=null, primary_key=pk, references=fk)


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #

_CONFIG_VERSIONS = Table(
    "config_versions",
    (
        _c("fingerprint", "TEXT", null=False, pk=True),
        _c("created_at", "TIMESTAMP", null=False),
        _c("config_dir", "TEXT"),
        _c("change_count", "INTEGER"),
        _c("change_summary", "TEXT"),
    ),
)

_RUNS = Table(
    "runs",
    (
        _c("run_id", "TEXT", null=False, pk=True),
        _c("config_fingerprint", "TEXT", null=False, fk="config_versions.fingerprint"),
        _c("seed", "INTEGER", null=False),
        _c("mode", "TEXT"),
        _c("started_at", "TIMESTAMP"),
        _c("ended_at", "TIMESTAMP"),
        _c("sim_start", "TIMESTAMP"),
        _c("sim_end", "TIMESTAMP"),
        _c("simulated_hours", "REAL"),
        _c("event_count", "INTEGER"),
        _c("telemetry_count", "INTEGER"),
        _c("notes", "TEXT"),
    ),
)

# --------------------------------------------------------------------------- #
# Registry dimensions — the vocabulary itself is data, so a new state or event
# type is a row rather than a migration.
# --------------------------------------------------------------------------- #

_STATES = Table(
    "states",
    (
        _c("state_id", "TEXT", null=False, pk=True),
        _c("description", "TEXT"),
        _c("production_rate_factor", "REAL"),
        _c("reject_rate_add", "REAL"),
        _c("energy_factor", "REAL"),
        _c("roles", "TEXT"),
        _c("allowed_transitions", "TEXT"),
    ),
)

_EVENT_TYPES = Table(
    "event_types",
    (
        _c("event_type", "TEXT", null=False, pk=True),
        _c("category", "TEXT", null=False),
        _c("default_severity", "TEXT"),
        _c("description", "TEXT"),
        _c("required_fields", "TEXT"),
        _c("streamed", "BOOLEAN"),
    ),
)

_EQUIPMENT_CLASSES = Table(
    "equipment_classes",
    (
        _c("equipment_class", "TEXT", null=False, pk=True),
        _c("name", "TEXT"),
        _c("sensor_profile", "TEXT"),
        _c("nominal_rate_per_hour", "REAL"),
        _c("pm_interval_hours", "REAL"),
        _c("base_reject_rate", "REAL"),
        _c("sensor_count", "INTEGER"),
    ),
)

# --------------------------------------------------------------------------- #
# Topology
# --------------------------------------------------------------------------- #

_PLANTS = Table(
    "plants",
    (
        _c("plant_id", "TEXT", null=False, pk=True),
        _c("name", "TEXT"),
        _c("location", "TEXT"),
        _c("timezone", "TEXT"),
        _c("unit_count", "INTEGER"),
        _c("machine_count", "INTEGER"),
        _c("employee_count", "INTEGER"),
        _c("plant_manager_id", "TEXT"),
    ),
)

_UNITS = Table(
    "units",
    (
        _c("unit_id", "TEXT", null=False, pk=True),
        _c("plant_id", "TEXT", null=False, fk="plants.plant_id"),
        _c("name", "TEXT"),
        _c("sequence", "INTEGER"),
        _c("process_stage", "TEXT"),
        _c("worker_count", "INTEGER"),
        _c("manager_count", "INTEGER"),
        _c("machine_count", "INTEGER"),
        _c("environment_sensitivity", "REAL"),
    ),
)

_MACHINES = Table(
    "machines",
    (
        _c("machine_id", "TEXT", null=False, pk=True),
        _c("unit_id", "TEXT", null=False, fk="units.unit_id"),
        _c("plant_id", "TEXT", null=False, fk="plants.plant_id"),
        _c("equipment_class", "TEXT", null=False, fk="equipment_classes.equipment_class"),
        _c("name", "TEXT"),
        _c("commissioned_on", "DATE"),
        _c("nominal_rate_per_hour", "REAL"),
        _c("pm_interval_hours", "REAL"),
        _c("sensor_profile", "TEXT"),
        _c("plc_id", "TEXT"),
        _c("sensor_count", "INTEGER"),
    ),
    indexes=(("unit_id",), ("equipment_class",)),
)

_SENSORS = Table(
    "sensors",
    (
        _c("sensor_id", "TEXT", null=False, pk=True),
        _c("machine_id", "TEXT", null=False, fk="machines.machine_id"),
        _c("unit_id", "TEXT", null=False, fk="units.unit_id"),
        _c("plant_id", "TEXT", null=False, fk="plants.plant_id"),
        _c("tag", "TEXT", null=False),
        _c("unit", "TEXT"),
        _c("plc_area", "TEXT"),
        _c("baseline", "REAL"),
        _c("sample_interval_s", "REAL"),
        _c("warn_low", "REAL"),
        _c("warn_high", "REAL"),
        _c("alarm_low", "REAL"),
        _c("alarm_high", "REAL"),
        _c("is_process_parameter", "BOOLEAN"),
        _c("derived_from", "TEXT"),
    ),
    indexes=(("machine_id",), ("tag",)),
)

_PLC_TAGS = Table(
    "plc_tags",
    (
        _c("plc_id", "TEXT", null=False),
        _c("machine_id", "TEXT", null=False, fk="machines.machine_id"),
        _c("tag_name", "TEXT", null=False),
        _c("area", "TEXT"),
        _c("address", "TEXT"),
        _c("unit", "TEXT"),
    ),
    composite_key=("plc_id", "tag_name"),
)

_EMPLOYEES = Table(
    "employees",
    (
        _c("employee_id", "TEXT", null=False, pk=True),
        _c("plant_id", "TEXT", null=False, fk="plants.plant_id"),
        _c("unit_id", "TEXT", fk="units.unit_id"),
        _c("name", "TEXT"),
        _c("role", "TEXT"),
        _c("skill_level", "TEXT"),
        _c("shift_code", "TEXT"),
        _c("experience_years", "REAL"),
        _c("attendance_probability", "REAL"),
        _c("machine_certifications", "TEXT"),
        _c("inexperience", "REAL"),
        _c("hired_on", "TIMESTAMP"),
    ),
    indexes=(("unit_id",), ("shift_code",)),
)

_PRODUCTS = Table(
    "products",
    (
        _c("product_id", "TEXT", null=False, pk=True),
        _c("product_name", "TEXT"),
        _c("dosage_form", "TEXT"),
        _c("batch_size", "INTEGER"),
        _c("target_quantity", "INTEGER"),
        _c("manufacturing_process", "TEXT"),
        _c("raw_materials", "TEXT"),
        _c("qc_specifications", "TEXT"),
        _c("demand_weight", "REAL"),
    ),
)

_SHIFTS = Table(
    "shifts",
    (
        _c("shift_code", "TEXT", null=False, pk=True),
        _c("name", "TEXT"),
        _c("start_time", "TEXT"),
        _c("end_time", "TEXT"),
        _c("crosses_midnight", "BOOLEAN"),
        _c("breaks", "TEXT"),
    ),
)

_SHIFT_INSTANCES = Table(
    "shift_instances",
    (
        _c("shift_instance_id", "TEXT", null=False, pk=True),
        _c("shift_code", "TEXT", null=False, fk="shifts.shift_code"),
        _c("plant_id", "TEXT", null=False, fk="plants.plant_id"),
        _c("business_date", "DATE"),
        _c("start_time", "TIMESTAMP"),
        _c("end_time", "TIMESTAMP"),
        _c("duration_hours", "REAL"),
        _c("roster_size", "INTEGER"),
        _c("present_count", "INTEGER"),
        _c("absent_count", "INTEGER"),
    ),
    indexes=(("business_date",),),
)

# --------------------------------------------------------------------------- #
# Facts
# --------------------------------------------------------------------------- #

_EVENTS = Table(
    "events",
    (
        _c("event_id", "TEXT", null=False, pk=True),
        _c("timestamp", "TIMESTAMP", null=False),
        _c("event_type", "TEXT", null=False, fk="event_types.event_type"),
        _c("category", "TEXT"),
        _c("severity", "TEXT"),
        _c("plant_id", "TEXT", fk="plants.plant_id"),
        _c("unit_id", "TEXT", fk="units.unit_id"),
        _c("machine_id", "TEXT", fk="machines.machine_id"),
        _c("batch_id", "TEXT", fk="batches.batch_id"),
        _c("employee_id", "TEXT", fk="employees.employee_id"),
        _c("shift_instance_id", "TEXT", fk="shift_instances.shift_instance_id"),
        _c("run_id", "TEXT", fk="runs.run_id"),
        _c("payload", "JSON"),
    ),
    indexes=(("timestamp",), ("event_type",), ("machine_id",), ("batch_id",)),
)

_MACHINE_STATE_HISTORY = Table(
    "machine_state_history",
    (
        _c("machine_id", "TEXT", null=False, fk="machines.machine_id"),
        _c("sequence", "INTEGER", null=False),
        _c("entered_at", "TIMESTAMP", null=False),
        _c("state", "TEXT", null=False, fk="states.state_id"),
        _c("exited_at", "TIMESTAMP"),
        _c("seconds", "REAL"),
        _c("reason", "TEXT"),
        _c("batch_id", "TEXT"),
        _c("run_id", "TEXT", fk="runs.run_id"),
    ),
    composite_key=("machine_id", "sequence"),
    indexes=(("machine_id",), ("state",)),
)

_PRODUCTION_RECORDS = Table(
    "production_records",
    (
        _c("machine_id", "TEXT", null=False, fk="machines.machine_id"),
        _c("shift_instance_id", "TEXT", null=False, fk="shift_instances.shift_instance_id"),
        _c("unit_id", "TEXT", null=False, fk="units.unit_id"),
        _c("equipment_class", "TEXT"),
        _c("planned_quantity", "REAL"),
        _c("actual_quantity", "REAL"),
        _c("good_quantity", "REAL"),
        _c("reject_quantity", "REAL"),
        _c("scrap_quantity", "REAL"),
        _c("runtime_seconds", "REAL"),
        _c("idle_seconds", "REAL"),
        _c("downtime_seconds", "REAL"),
        _c("planned_stop_seconds", "REAL"),
        _c("offline_seconds", "REAL"),
        _c("unscheduled_seconds", "REAL"),
        _c("energy_kwh", "REAL"),
        _c("cycle_count", "INTEGER"),
        _c("availability", "REAL"),
        _c("performance", "REAL"),
        _c("quality", "REAL"),
        _c("oee", "REAL"),
        _c("utilisation", "REAL"),
        _c("run_id", "TEXT", fk="runs.run_id"),
    ),
    composite_key=("machine_id", "shift_instance_id"),
    indexes=(("shift_instance_id",), ("unit_id",)),
)

_OEE_SNAPSHOTS = Table(
    "oee_snapshots",
    (
        _c("scope", "TEXT", null=False),
        _c("scope_id", "TEXT", null=False),
        _c("shift_instance_id", "TEXT", null=False),
        _c("availability", "REAL"),
        _c("performance", "REAL"),
        _c("quality", "REAL"),
        _c("oee", "REAL"),
        _c("utilisation", "REAL"),
        _c("loading_seconds", "REAL"),
        _c("runtime_seconds", "REAL"),
        _c("downtime_seconds", "REAL"),
        _c("unscheduled_seconds", "REAL"),
        _c("good_quantity", "REAL"),
        _c("actual_quantity", "REAL"),
        _c("planned_quantity", "REAL"),
        _c("run_id", "TEXT", fk="runs.run_id"),
    ),
    composite_key=("scope", "scope_id", "shift_instance_id"),
)

_BATCHES = Table(
    "batches",
    (
        _c("batch_id", "TEXT", null=False, pk=True),
        _c("order_id", "TEXT"),
        _c("product_id", "TEXT", null=False, fk="products.product_id"),
        _c("plant_id", "TEXT", fk="plants.plant_id"),
        _c("planned_quantity", "INTEGER"),
        _c("good_quantity", "REAL"),
        _c("reject_quantity", "REAL"),
        _c("created_at", "TIMESTAMP"),
        _c("started_at", "TIMESTAMP"),
        _c("completed_at", "TIMESTAMP"),
        _c("disposition", "TEXT"),
        _c("route", "TEXT"),
        _c("stages_completed", "INTEGER"),
        _c("machines_used", "TEXT"),
        _c("units_used", "TEXT"),
        _c("operators_involved", "TEXT"),
        _c("shift_instances", "TEXT"),
        _c("failure_ids", "TEXT"),
        _c("deviation_ids", "TEXT"),
        _c("qc_test_count", "INTEGER"),
        _c("qc_failure_count", "INTEGER"),
        _c("material_variability", "REAL"),
        _c("run_id", "TEXT", fk="runs.run_id"),
    ),
    indexes=(("product_id",), ("disposition",)),
)

_BATCH_STAGES = Table(
    "batch_stages",
    (
        _c("batch_id", "TEXT", null=False, fk="batches.batch_id"),
        _c("sequence", "INTEGER", null=False),
        _c("stage", "TEXT", null=False),
        _c("unit_id", "TEXT", fk="units.unit_id"),
        _c("machine_id", "TEXT", fk="machines.machine_id"),
        _c("started_at", "TIMESTAMP"),
        _c("completed_at", "TIMESTAMP"),
        _c("result", "TEXT"),
        _c("operator_ids", "TEXT"),
        _c("shift_instance_id", "TEXT"),
        _c("parameters", "JSON"),
        _c("deviating_parameters", "TEXT"),
        _c("machine_health", "REAL"),
        _c("interrupted_by_failure", "TEXT"),
        _c("duration_minutes", "REAL"),
    ),
    composite_key=("batch_id", "sequence"),
    indexes=(("machine_id",), ("stage",)),
)

_QC_RESULTS = Table(
    "qc_results",
    (
        _c("test_id", "TEXT", null=False, pk=True),
        _c("batch_id", "TEXT", null=False, fk="batches.batch_id"),
        _c("product_id", "TEXT", null=False, fk="products.product_id"),
        _c("parameter", "TEXT", null=False),
        _c("parameter_name", "TEXT"),
        _c("stage", "TEXT"),
        _c("phase", "TEXT"),
        _c("target", "REAL"),
        _c("lower_limit", "REAL"),
        _c("upper_limit", "REAL"),
        _c("actual_value", "REAL"),
        _c("result", "TEXT"),
        _c("timestamp", "TIMESTAMP"),
        _c("operator_id", "TEXT"),
        _c("machine_id", "TEXT"),
        _c("unit", "TEXT"),
        _c("sample_size", "INTEGER"),
        _c("run_id", "TEXT", fk="runs.run_id"),
    ),
    indexes=(("batch_id",), ("parameter",), ("result",)),
)

_FAILURES = Table(
    "failures",
    (
        _c("failure_id", "TEXT", null=False, pk=True),
        _c("machine_id", "TEXT", null=False, fk="machines.machine_id"),
        _c("unit_id", "TEXT", fk="units.unit_id"),
        _c("equipment_class", "TEXT"),
        # Note: category and symptom only. The failure MODE and its root cause
        # are ground truth and deliberately absent from operational data (§25).
        _c("category", "TEXT"),
        _c("severity", "TEXT"),
        _c("symptom", "TEXT"),
        _c("detected_at", "TIMESTAMP"),
        _c("resolved_at", "TIMESTAMP"),
        _c("alarm_count", "INTEGER"),
        _c("state_before", "TEXT"),
        _c("batch_id", "TEXT"),
        _c("shift_instance_id", "TEXT"),
        _c("operator_ids", "TEXT"),
        _c("downtime_minutes", "REAL"),
        _c("production_loss_units", "REAL"),
        _c("affected_batches", "TEXT"),
        _c("maintenance_id", "TEXT"),
        _c("deviation_id", "TEXT"),
        _c("run_id", "TEXT", fk="runs.run_id"),
    ),
    indexes=(("machine_id",), ("detected_at",), ("category",)),
)

_MAINTENANCE = Table(
    "maintenance",
    (
        _c("maintenance_id", "TEXT", null=False, pk=True),
        _c("machine_id", "TEXT", null=False, fk="machines.machine_id"),
        _c("unit_id", "TEXT", fk="units.unit_id"),
        _c("maintenance_type", "TEXT"),
        _c("scheduled_time", "TIMESTAMP"),
        _c("actual_time", "TIMESTAMP"),
        _c("completed_time", "TIMESTAMP"),
        _c("technician_id", "TEXT"),
        _c("failure_id", "TEXT"),
        _c("parts_replaced", "TEXT"),
        _c("duration_hours", "REAL"),
        _c("cost", "REAL"),
        _c("status", "TEXT"),
        _c("triggered_by", "TEXT"),
        _c("run_id", "TEXT", fk="runs.run_id"),
    ),
    indexes=(("machine_id",), ("maintenance_type",)),
)

_DEVIATIONS = Table(
    "deviations",
    (
        _c("deviation_id", "TEXT", null=False, pk=True),
        _c("rule_id", "TEXT"),
        _c("title", "TEXT"),
        _c("severity", "TEXT"),
        _c("status", "TEXT"),
        _c("detected_at", "TIMESTAMP"),
        _c("closed_at", "TIMESTAMP"),
        _c("plant_id", "TEXT", fk="plants.plant_id"),
        _c("unit_id", "TEXT", fk="units.unit_id"),
        _c("machine_id", "TEXT"),
        _c("batch_id", "TEXT"),
        _c("trigger_event", "TEXT"),
        _c("trigger_event_id", "TEXT"),
        _c("failure_id", "TEXT"),
        _c("description", "TEXT"),
        _c("requires_rca", "BOOLEAN"),
        _c("requires_capa", "BOOLEAN"),
        _c("rca_id", "TEXT"),
        _c("capa_id", "TEXT"),
        _c("run_id", "TEXT", fk="runs.run_id"),
    ),
    indexes=(("machine_id",), ("batch_id",), ("severity",)),
)

_RCA = Table(
    "rca",
    (
        _c("rca_id", "TEXT", null=False, pk=True),
        _c("deviation_id", "TEXT", null=False, fk="deviations.deviation_id"),
        _c("machine_id", "TEXT"),
        _c("batch_id", "TEXT"),
        _c("failure_id", "TEXT"),
        _c("started_at", "TIMESTAMP"),
        _c("completed_at", "TIMESTAMP"),
        _c("method", "TEXT"),
        _c("root_cause", "TEXT"),
        _c("confidence", "REAL"),
        _c("score", "REAL"),
        _c("fishbone_category", "TEXT"),
        _c("five_why", "TEXT"),
        _c("causal_chain", "TEXT"),
        _c("corrective_action", "TEXT"),
        _c("preventive_action", "TEXT"),
        _c("evidence_summary", "TEXT"),
        _c("evidence_count", "INTEGER"),
        _c("alternatives_considered", "TEXT"),
        _c("run_id", "TEXT", fk="runs.run_id"),
    ),
    indexes=(("deviation_id",), ("root_cause",)),
)

_RCA_EVIDENCE = Table(
    "rca_evidence",
    (
        _c("rca_id", "TEXT", null=False, fk="rca.rca_id"),
        _c("evidence_id", "TEXT", null=False),
        _c("description", "TEXT"),
        _c("tag", "TEXT"),
        _c("signal", "TEXT"),
        _c("observed_value", "REAL"),
        _c("threshold", "REAL"),
        _c("weight", "REAL"),
    ),
    composite_key=("rca_id", "evidence_id"),
)

_CAPA = Table(
    "capa",
    (
        _c("capa_id", "TEXT", null=False, pk=True),
        _c("deviation_id", "TEXT", null=False, fk="deviations.deviation_id"),
        _c("rca_id", "TEXT", fk="rca.rca_id"),
        _c("problem", "TEXT"),
        _c("root_cause", "TEXT"),
        _c("corrective_action", "TEXT"),
        _c("preventive_action", "TEXT"),
        _c("opened_at", "TIMESTAMP"),
        _c("closed_at", "TIMESTAMP"),
        _c("owner_id", "TEXT"),
        _c("status", "TEXT"),
        _c("verification_batches_required", "INTEGER"),
        _c("verification_batches_passed", "INTEGER"),
        _c("verified_batch_ids", "TEXT"),
        _c("run_id", "TEXT", fk="runs.run_id"),
    ),
    indexes=(("deviation_id",), ("status",)),
)

_EMPLOYEE_EVENTS = Table(
    "employee_events",
    (
        _c("event_id", "TEXT", null=False, pk=True),
        _c("timestamp", "TIMESTAMP", null=False),
        _c("employee_id", "TEXT", null=False, fk="employees.employee_id"),
        _c("event_type", "TEXT", null=False),
        _c("shift_instance_id", "TEXT", fk="shift_instances.shift_instance_id"),
        _c("unit_id", "TEXT", fk="units.unit_id"),
        _c("machine_id", "TEXT"),
        _c("payload", "JSON"),
        _c("run_id", "TEXT", fk="runs.run_id"),
    ),
    indexes=(("employee_id",), ("shift_instance_id",), ("timestamp",)),
)

_MACHINE_EVENTS = Table(
    "machine_events",
    (
        _c("event_id", "TEXT", null=False, pk=True),
        _c("timestamp", "TIMESTAMP", null=False),
        _c("machine_id", "TEXT", null=False, fk="machines.machine_id"),
        _c("unit_id", "TEXT", fk="units.unit_id"),
        _c("event_type", "TEXT", null=False),
        _c("severity", "TEXT"),
        _c("batch_id", "TEXT"),
        _c("payload", "JSON"),
        _c("run_id", "TEXT", fk="runs.run_id"),
    ),
    indexes=(("machine_id",), ("timestamp",), ("event_type",)),
)

#: Insertion order. Dimensions precede the facts that reference them, so
#: database-level foreign keys can stay enabled.
TABLE_ORDER: tuple[str, ...] = (
    "config_versions",
    "runs",
    "states",
    "event_types",
    "equipment_classes",
    "plants",
    "units",
    "machines",
    "sensors",
    "plc_tags",
    "employees",
    "products",
    "shifts",
    "shift_instances",
    "batches",
    "batch_stages",
    "qc_results",
    "failures",
    "maintenance",
    "deviations",
    "rca",
    "rca_evidence",
    "capa",
    "machine_state_history",
    "production_records",
    "oee_snapshots",
    "employee_events",
    "machine_events",
    "events",
)

TABLES: dict[str, Table] = {
    table.name: table
    for table in (
        _CONFIG_VERSIONS,
        _RUNS,
        _STATES,
        _EVENT_TYPES,
        _EQUIPMENT_CLASSES,
        _PLANTS,
        _UNITS,
        _MACHINES,
        _SENSORS,
        _PLC_TAGS,
        _EMPLOYEES,
        _PRODUCTS,
        _SHIFTS,
        _SHIFT_INSTANCES,
        _EVENTS,
        _MACHINE_STATE_HISTORY,
        _PRODUCTION_RECORDS,
        _OEE_SNAPSHOTS,
        _BATCHES,
        _BATCH_STAGES,
        _QC_RESULTS,
        _FAILURES,
        _MAINTENANCE,
        _DEVIATIONS,
        _RCA,
        _RCA_EVIDENCE,
        _CAPA,
        _EMPLOYEE_EVENTS,
        _MACHINE_EVENTS,
    )
}

# --------------------------------------------------------------------------- #
# Evaluation store — separate by construction so it cannot leak into an
# operational query or export (§25).
# --------------------------------------------------------------------------- #

_GROUND_TRUTH = Table(
    "ground_truth_events",
    (
        _c("ground_truth_id", "TEXT", null=False, pk=True),
        _c("episode_id", "TEXT", null=False),
        _c("failure_id", "TEXT"),
        _c("machine_id", "TEXT", null=False),
        _c("unit_id", "TEXT"),
        _c("equipment_class", "TEXT"),
        _c("failure_mode", "TEXT"),
        _c("failure_category", "TEXT"),
        _c("root_cause", "TEXT"),
        _c("root_cause_description", "TEXT"),
        _c("onset_at", "TIMESTAMP"),
        _c("scheduled_fault_at", "TIMESTAMP"),
        _c("faulted_at", "TIMESTAMP"),
        _c("warned_at", "TIMESTAMP"),
        _c("averted_at", "TIMESTAMP"),
        _c("resolved_at", "TIMESTAMP"),
        _c("incubation_hours", "REAL"),
        _c("detectable", "BOOLEAN"),
        _c("injected", "BOOLEAN"),
        _c("precursor_tags", "TEXT"),
        _c("severity", "TEXT"),
        _c("outcome", "TEXT"),
        _c("affected_batches", "TEXT"),
        _c("affected_qc_failures", "TEXT"),
        _c("production_loss_units", "REAL"),
        _c("downtime_minutes", "REAL"),
        _c("run_id", "TEXT"),
    ),
)

_PREDICTION_LABELS = Table(
    "prediction_labels",
    (
        _c("machine_id", "TEXT", null=False),
        _c("timestamp", "TIMESTAMP", null=False),
        _c("unit_id", "TEXT"),
        _c("equipment_class", "TEXT"),
        _c("health_index", "REAL"),
        _c("degrading", "BOOLEAN"),
        _c("failure_mode", "TEXT"),
        _c("failure_category", "TEXT"),
        _c("root_cause", "TEXT"),
        # NOT NULL with the NO_EPISODE sentinel: negative labels have no
        # episode, but the key must still be unique and NULL cannot be part
        # of a primary key.
        _c("episode_id", "TEXT", null=False),
        _c("rul_hours", "REAL"),
        _c("will_fail_24h", "BOOLEAN"),
        _c("will_fail_72h", "BOOLEAN"),
        _c("will_fail_168h", "BOOLEAN"),
        _c("degradation_stage", "TEXT"),
        _c("averted", "BOOLEAN"),
        _c("detectable", "BOOLEAN"),
        _c("run_id", "TEXT"),
    ),
    composite_key=("machine_id", "timestamp", "episode_id"),
)

EVAL_TABLES: dict[str, Table] = {
    table.name: table for table in (_GROUND_TRUTH, _PREDICTION_LABELS)
}

# --------------------------------------------------------------------------- #
# Time-series store — deliberately narrow, append-only and free of foreign keys,
# so a new sensor tag never requires DDL in any backend.
# --------------------------------------------------------------------------- #

TELEMETRY_TABLE = Table(
    "sensor_readings",
    (
        _c("timestamp", "TIMESTAMP", null=False),
        _c("machine_id", "TEXT", null=False),
        _c("sensor_id", "TEXT", null=False),
        _c("tag", "TEXT", null=False),
        _c("value", "REAL"),
        _c("unit", "TEXT"),
        _c("quality", "TEXT"),
        _c("unit_id", "TEXT"),
        _c("run_id", "TEXT"),
    ),
)
