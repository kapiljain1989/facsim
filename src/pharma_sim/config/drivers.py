"""Named quantities the engine supplies to declarative :class:`Transfer` maths.

Config authors may reference these inputs in QC transfer functions and hazard
factors without declaring them anywhere. Keeping the list here means the linter
can tell a legitimate engine driver apart from a typo.
"""

from __future__ import annotations

from typing import Final

#: Inputs available to QC transfer functions (§19), beyond process parameters
#: and previously-computed QC results.
QC_DRIVERS: Final[frozenset[str]] = frozenset(
    {
        "machine_health",  # 0 = healthy, 1 = fully degraded
        "operator_inexperience",  # 0 = expert, 1 = novice
        "ambient_temperature_c",
        "ambient_humidity_pct",
        "material_variability",  # lot-to-lot deviation of the raw materials
        "stage_duration_min",
        "batch_size",
    }
)

#: Inputs available to hazard factor transfers (§14).
HAZARD_DRIVERS: Final[frozenset[str]] = frozenset(
    {
        "age_years",
        "operating_khours",  # operating hours / 1000
        "pm_overdue_ratio",  # 0 = on schedule, 1 = a full interval overdue
        "load_factor",  # 0 = idle, 1 = nominal throughput
        "environment_stress",  # 0 = nominal, 1 = excursion
        "operator_inexperience",
    }
)

#: Non-sensor signals an RCA evidence rule may inspect.
RCA_SIGNALS: Final[frozenset[str]] = frozenset(
    {
        "pm_overdue_hours",
        "hours_since_last_maintenance",
        "corrective_repairs_90d",
        "operator_inexperience",
        "warning_duration_hours",
        "alarm_count",
        "sensor_quality_bad_fraction",
        "reject_rate_increase",
        "ambient_excursion_hours",
        "material_wait_hours",
        "setup_error_count",
        "parameter_deviation_count",
        "missed_inspection_count",
        "qc_failure_count",
        "batch_reject_rate",
    }
)

#: Live machine quantities a sensor tag may mirror instead of generating its own
#: value. A counter that drifted independently of the production it supposedly
#: counts would be the most obvious synthetic-data artefact of all.
DERIVED_SOURCES: Final[frozenset[str]] = frozenset(
    {
        "production_rate",  # units/hour currently being produced
        "good_count",  # cumulative good units this batch
        "reject_count",  # cumulative rejects this batch
        "total_count",  # cumulative units this batch
        "batch_counter",  # batches completed since commissioning
        "energy_kw",  # instantaneous draw implied by the state's energy factor
        "health_index",  # 0 = healthy, 1 = fully degraded
        "run_state_code",  # ordinal of the current state, as a PLC state word
        "operator_present",  # 1 when a certified operator is clocked in
    }
)

#: Action types understood by the scenario engine (§39).
SCENARIO_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "inject_failure",
        "inject_failure_random",
        "force_state",
        "material_shortage",
        "ambient_excursion",
        "power_interruption",
        "operator_error",
        "sensor_malfunction",
        "defer_maintenance",
        "set_demand",
    }
)
