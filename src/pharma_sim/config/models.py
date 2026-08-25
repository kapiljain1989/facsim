"""Pydantic models for every YAML configuration file.

These models are the *only* place the shape of configuration is described. The
simulation engine never hard-codes a state name, an equipment type, a sensor tag
or a failure mode: it reads whatever these models validated and loaded.

Two conventions matter throughout:

* Identifiers are plain strings, deliberately **not** enums. A new machine state
  or failure mode is a config edit, not a code change. Cross-file consistency is
  checked by :mod:`pharma_sim.config.linter` instead of by the type system.
* Anything the engine needs to compute a number is expressed declaratively
  (see :class:`Transfer`), never as a Python expression to ``eval``.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Ident = Annotated[str, Field(min_length=1, max_length=64)]
Probability = Annotated[float, Field(ge=0.0, le=1.0)]
Positive = Annotated[float, Field(gt=0.0)]
NonNegative = Annotated[float, Field(ge=0.0)]


class Strict(BaseModel):
    """Base model that rejects unknown keys.

    Typos in a 5,000-line config are otherwise silently ignored, which is the
    single most common way a config-driven system misleads its operator.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- #
# Declarative maths
# --------------------------------------------------------------------------- #


class Term(Strict):
    """One additive term of a :class:`Transfer`: ``coef * (input ** power)``."""

    input: Ident
    coef: float
    power: float = 1.0


class Transfer(Strict):
    """A declarative polynomial: ``intercept + sum(coef * input**power)``.

    Used for QC transfer functions (§19) and for failure-factor curves (§14).
    Declarative rather than an eval'd string so a config file can never execute
    code, and so the linter can verify every ``input`` actually exists.
    """

    intercept: float = 0.0
    terms: list[Term] = Field(default_factory=list)
    clip_min: float | None = None
    clip_max: float | None = None

    @property
    def inputs(self) -> set[str]:
        return {t.input for t in self.terms}

    def evaluate(
        self, values: dict[str, float], references: dict[str, float] | None = None
    ) -> float:
        """Evaluate the polynomial.

        An input absent from ``values`` falls back to ``references``, and is
        skipped only if neither supplies it. That fallback is what lets one QC
        transfer serve products whose route omits a stage: a direct-compression
        product has no drying step, so ``moisture_content`` is substituted at its
        nominal value rather than silently dropping the term and shifting the
        result.
        """
        total = self.intercept
        for term in self.terms:
            raw = values.get(term.input)
            if raw is None and references is not None:
                raw = references.get(term.input)
            if raw is None:
                continue
            total += term.coef * (raw**term.power)
        if self.clip_min is not None:
            total = max(self.clip_min, total)
        if self.clip_max is not None:
            total = min(self.clip_max, total)
        return total


# --------------------------------------------------------------------------- #
# plant.yaml
# --------------------------------------------------------------------------- #


class AmbientConfig(Strict):
    """Plant-wide environment, shared by every sensor as a latent driver."""

    temperature_c: float = 22.0
    temperature_diurnal_amplitude_c: NonNegative = 2.5
    humidity_pct: float = 45.0
    humidity_diurnal_amplitude_pct: NonNegative = 5.0
    excursion_probability_per_day: Probability = 0.02
    excursion_temperature_delta_c: float = 6.0
    excursion_duration_hours: Positive = 3.0


class SimulationDefaults(Strict):
    seed: int = 42
    start_time: datetime = datetime(2026, 1, 1, 6, 0, 0)
    speed_sim_minutes_per_real_second: Positive = 60.0
    sensor_sample_interval_s: Positive = 60.0
    live_sensor_sample_interval_s: Positive = 5.0
    hazard_evaluation_interval_min: Positive = 60.0
    production_tick_min: Positive = 5.0
    label_interval_min: Positive = 30.0
    rca_lookback_hours: Positive = 72.0
    rca_investigation_delay_hours: Positive = 4.0


class PlantConfig(Strict):
    plant_id: Ident
    name: str
    location: str = ""
    timezone: str = "UTC"
    plant_manager_name: str = "Plant Manager"
    simulation: SimulationDefaults = Field(default_factory=SimulationDefaults)
    ambient: AmbientConfig = Field(default_factory=AmbientConfig)


# --------------------------------------------------------------------------- #
# states.yaml
# --------------------------------------------------------------------------- #


class StateSpec(Strict):
    id: Ident
    description: str = ""
    #: Multiplies nominal production rate while the machine sits in this state.
    production_rate_factor: NonNegative = 0.0
    #: Added to the reject fraction while in this state.
    reject_rate_add: NonNegative = 0.0
    #: Relative energy draw, for consumption accounting.
    energy_factor: NonNegative = 0.1


class StateRolesConfig(Strict):
    """What states *mean*, so the engine never compares against a state name.

    Every role is a list of state ids. Rename ``RUNNING`` to ``IN_PRODUCTION``
    and downstream production/OEE/downtime logic keeps working because it reads
    these roles rather than the name.
    """

    initial: Ident
    productive: list[Ident] = Field(default_factory=list)
    downtime: list[Ident] = Field(default_factory=list)
    idle: list[Ident] = Field(default_factory=list)
    warning: list[Ident] = Field(default_factory=list)
    fault: list[Ident] = Field(default_factory=list)
    maintenance: list[Ident] = Field(default_factory=list)
    cleaning: list[Ident] = Field(default_factory=list)
    changeover: list[Ident] = Field(default_factory=list)
    offline: list[Ident] = Field(default_factory=list)
    starting: list[Ident] = Field(default_factory=list)
    requires_operator: list[Ident] = Field(default_factory=list)
    #: Counted as planned (not unplanned) downtime in OEE availability.
    planned_stop: list[Ident] = Field(default_factory=list)


class StatesConfig(Strict):
    states: list[StateSpec] = Field(min_length=1)
    transitions: dict[Ident, list[Ident]]
    roles: StateRolesConfig


# --------------------------------------------------------------------------- #
# event_types.yaml
# --------------------------------------------------------------------------- #


class EventTypeSpec(Strict):
    id: Ident
    category: Ident
    default_severity: Ident = "INFO"
    description: str = ""
    #: Payload keys an emitter must supply; enforced by the event bus.
    required_fields: list[str] = Field(default_factory=list)
    #: Whether this event type is forwarded to streaming sinks.
    stream: bool = True


class EventTypesConfig(Strict):
    severities: list[Ident] = Field(default_factory=lambda: ["INFO", "MINOR", "MAJOR", "CRITICAL"])
    event_types: list[EventTypeSpec] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# sensors.yaml
# --------------------------------------------------------------------------- #


class MalfunctionSpec(Strict):
    """Sensor-instrument faults, independent of the machine's own health."""

    stuck_probability_per_day: Probability = 0.0
    stuck_duration_min: Positive = 30.0
    dropout_probability: Probability = 0.0
    spike_probability: Probability = 0.0
    spike_sigma_multiple: NonNegative = 8.0
    noise_burst_probability: Probability = 0.0
    noise_burst_sigma_multiple: NonNegative = 4.0


class StateFactor(Strict):
    """How one machine state shifts a tag's distribution."""

    mult: float = 1.0
    offset: float = 0.0
    sigma_mult: NonNegative = 1.0


class SensorSpec(Strict):
    """A single measured tag and the stochastic process that generates it."""

    tag: Ident
    unit: str = ""
    baseline: float
    #: Measurement noise standard deviation.
    sigma: NonNegative = 0.0
    #: AR(1) coefficient. 0 = white noise, →1 = strongly autocorrelated.
    rho: Annotated[float, Field(ge=0.0, lt=1.0)] = 0.85
    #: Bounded random-walk drift, expressed per simulated day.
    drift_per_day: float = 0.0
    drift_limit: NonNegative = 0.0
    #: Diurnal/ambient coupling amplitude.
    diurnal_amplitude: NonNegative = 0.0
    ambient_coupling: float = 0.0
    #: Which ambient channel this tag responds to.
    ambient_source: Literal["temperature", "humidity"] = "temperature"
    #: Multiplier on the machine's health index contribution (generic wear).
    health_sensitivity: float = 0.0
    #: Hard instrument range; values are clamped to it.
    hard_min: float | None = None
    hard_max: float | None = None
    rate_s: Positive | None = None
    #: PLC memory area this tag lives in.
    plc_area: Literal["AI", "AO", "DI", "DO", "COUNTER"] = "AI"
    warn_low: float | None = None
    warn_high: float | None = None
    alarm_low: float | None = None
    alarm_high: float | None = None
    #: Keyed by state id *or* role name; state id wins when both match.
    state_factors: dict[Ident, StateFactor] = Field(default_factory=dict)
    malfunction: MalfunctionSpec = Field(default_factory=MalfunctionSpec)
    #: Marks a tag as a process parameter that QC transfer functions may read.
    process_parameter: bool = False
    #: When set, the tag mirrors real machine state (a counter or rate) instead
    #: of being generated stochastically. See ``drivers.DERIVED_SOURCES``.
    derived_from: Ident | None = None


class SensorBinding(Strict):
    """A per-equipment-class adjustment to an inherited sensor profile.

    Exactly one of the three modes applies: ``remove`` drops an inherited tag,
    ``override`` patches selected fields of one, and otherwise the binding is a
    full inline sensor definition.
    """

    tag: Ident
    remove: bool = False
    override: dict[str, Any] | None = None
    unit: str | None = None
    baseline: float | None = None
    sigma: NonNegative | None = None
    rho: Annotated[float, Field(ge=0.0, lt=1.0)] | None = None
    drift_per_day: float | None = None
    drift_limit: NonNegative | None = None
    diurnal_amplitude: NonNegative | None = None
    ambient_coupling: float | None = None
    ambient_source: Literal["temperature", "humidity"] | None = None
    health_sensitivity: float | None = None
    hard_min: float | None = None
    hard_max: float | None = None
    rate_s: Positive | None = None
    plc_area: Literal["AI", "AO", "DI", "DO", "COUNTER"] | None = None
    warn_low: float | None = None
    warn_high: float | None = None
    alarm_low: float | None = None
    alarm_high: float | None = None
    state_factors: dict[Ident, StateFactor] | None = None
    malfunction: MalfunctionSpec | None = None
    process_parameter: bool | None = None
    derived_from: Ident | None = None

    @model_validator(mode="after")
    def _remove_is_exclusive(self) -> SensorBinding:
        if self.remove:
            populated = [
                name
                for name, value in self.__dict__.items()
                if name not in {"tag", "remove"} and value is not None
            ]
            if populated:
                raise ValueError(
                    f"sensor binding {self.tag!r} sets remove=true but also defines "
                    f"{sorted(populated)}; remove must appear alone"
                )
        return self

    def inline_fields(self) -> dict[str, Any]:
        """Explicitly-set fields, excluding the control keys."""
        data = self.model_dump(exclude_none=True, exclude={"tag", "remove", "override"})
        if self.override:
            data.update(self.override)
        return data


class SensorsConfig(Strict):
    #: Reusable named profiles; an equipment class may use one, none, or extend one.
    profiles: dict[Ident, list[SensorSpec]] = Field(default_factory=dict)
    default_rate_s: Positive = 60.0


# --------------------------------------------------------------------------- #
# machines.yaml
# --------------------------------------------------------------------------- #


#: How a machine earns its running time. Not every machine in a plant waits for
#: a batch to be routed to it, and modelling them as if they did leaves utilities
#: permanently idle — which is the opposite of how a real plant behaves.
#:
#: ``batch``      takes batch stages; productive only while holding one.
#: ``continuous`` a utility on continuous duty (HVAC, purified water, compressed
#:                air). Runs unattended around the clock and stops only for
#:                failure or maintenance. Never receives a batch.
#: ``coupled``    inline support tied to the line it sits on (deduster, metal
#:                detector, cartoner, dust extraction). Runs whenever any
#:                batch-duty machine in its unit is producing.
Duty = Literal["batch", "continuous", "coupled"]


class EquipmentClassSpec(Strict):
    id: Ident
    name: str
    #: See :data:`Duty`. Defaults to ``batch`` so existing configs are unchanged.
    duty: Duty = "batch"
    #: Optional profile to inherit sensors from.
    sensor_profile: Ident | None = None
    #: Inline additions / overrides / removals on top of the profile.
    sensors: list[SensorBinding] = Field(default_factory=list)
    nominal_rate_per_hour: Positive = 1000.0
    #: Fixed stage duration in minutes. Set this for equipment whose stage length
    #: is governed by process time rather than throughput — a dryer runs for its
    #: drying time regardless of batch size, whereas a tablet press takes as long
    #: as the batch divided by its rate. Left unset, duration is derived from
    #: ``nominal_rate_per_hour``.
    stage_duration_min: Positive | None = None
    pm_interval_hours: Positive = 720.0
    pm_duration_hours: Positive = 4.0
    #: Restrict which failure modes can apply; empty means "all applicable".
    failure_modes: list[Ident] = Field(default_factory=list)
    #: Baseline reject fraction under nominal conditions.
    base_reject_rate: Probability = 0.004
    setup_duration_min: Positive = 20.0
    cleaning_duration_min: Positive = 30.0
    changeover_duration_min: Positive = 45.0
    startup_duration_min: Positive = 5.0


class MachineGroupSpec(Strict):
    """A run of identical machines instantiated inside one unit."""

    equipment_class: Ident
    count: Annotated[int, Field(ge=1)]
    id_prefix: Ident
    commissioned_from: date = date(2019, 1, 1)
    commissioned_to: date = date(2025, 6, 30)
    #: Per-instance overrides applied to every machine in this group.
    sensors: list[SensorBinding] = Field(default_factory=list)


class MachinesConfig(Strict):
    equipment_classes: list[EquipmentClassSpec] = Field(min_length=1)
    #: unit id -> machine groups in that unit.
    layout: dict[Ident, list[MachineGroupSpec]]


# --------------------------------------------------------------------------- #
# units.yaml
# --------------------------------------------------------------------------- #


class UnitSpec(Strict):
    id: Ident
    name: str
    sequence: Annotated[int, Field(ge=1)]
    #: Process stage this unit performs; products reference these in their route.
    process_stage: Ident
    worker_count: Annotated[int, Field(ge=0)] = 10
    manager_count: Annotated[int, Field(ge=0)] = 1
    #: Cleanroom-ish environment sensitivity, feeds the failure environment factor.
    environment_sensitivity: NonNegative = 1.0


class UnitsConfig(Strict):
    units: list[UnitSpec] = Field(min_length=1)
    worker_roles: list[Ident] = Field(default_factory=lambda: ["OPERATOR"])
    manager_role: Ident = "UNIT_MANAGER"
    technician_role: Ident = "TECHNICIAN"
    qc_analyst_role: Ident = "QC_ANALYST"
    skill_levels: list[Ident] = Field(default_factory=lambda: ["JUNIOR", "INTERMEDIATE", "SENIOR"])
    #: Site tenure in years. ``hired_on`` is the run start minus a draw from this
    #: range, capped by each person's own ``experience_years`` -- nobody has
    #: worked at this site longer than they have worked at all. Keep the maximum
    #: at or below the site's age; the earliest ``commissioned_from`` in
    #: machines.yaml is the practical bound.
    tenure_years_min: NonNegative = 0.25
    tenure_years_max: Positive = 7.0


# --------------------------------------------------------------------------- #
# shifts.yaml
# --------------------------------------------------------------------------- #


class BreakSpec(Strict):
    label: str
    start: time
    duration_min: Positive


class ShiftSpec(Strict):
    code: Ident
    name: str
    start: time
    end: time
    breaks: list[BreakSpec] = Field(default_factory=list)

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start


class ShiftsConfig(Strict):
    shifts: list[ShiftSpec] = Field(min_length=1)
    absenteeism_rate: Probability = 0.04
    overtime_probability: Probability = 0.08
    overtime_duration_min: Positive = 60.0
    clock_in_jitter_min: NonNegative = 8.0
    clock_out_jitter_min: NonNegative = 6.0


# --------------------------------------------------------------------------- #
# products.yaml
# --------------------------------------------------------------------------- #


class ParameterTarget(Strict):
    """A process parameter setpoint and its acceptable operating window."""

    target: float
    min: float | None = None
    max: float | None = None
    unit: str = ""


class RawMaterialSpec(Strict):
    material_id: Ident
    name: str
    quantity_kg: Positive
    #: Lot-to-lot variability, as a fraction of nominal.
    variability: NonNegative = 0.02


class QcLimitOverride(Strict):
    """Per-product replacement of a QC parameter's target and/or limits.

    Necessary because specifications are genuinely product-relative: a 550 mg
    and a 640 mg tablet cannot share one weight limit. Only the fields given are
    replaced, so a product can tighten one bound and inherit the rest.
    """

    target: float | None = None
    lower_limit: float | None = None
    upper_limit: float | None = None


class ProductSpec(Strict):
    product_id: Ident
    product_name: str
    dosage_form: Ident
    batch_size: Annotated[int, Field(ge=1)]
    target_quantity: Annotated[int, Field(ge=1)]
    #: Ordered process stages; each must be a declared unit stage.
    manufacturing_process: list[Ident] = Field(min_length=1)
    raw_materials: list[RawMaterialSpec] = Field(default_factory=list)
    #: stage -> parameter -> target window.
    process_parameters: dict[Ident, dict[Ident, ParameterTarget]] = Field(default_factory=dict)
    #: QC parameter ids applicable to this product.
    qc_specifications: list[Ident] = Field(default_factory=list)
    #: Product-specific target/limit replacements, keyed by QC parameter id.
    qc_overrides: dict[Ident, QcLimitOverride] = Field(default_factory=dict)
    #: Relative share of production orders.
    demand_weight: Positive = 1.0


class ProductsConfig(Strict):
    products: list[ProductSpec] = Field(min_length=1)
    #: Concurrent batches the plant will keep in flight.
    max_concurrent_batches: Annotated[int, Field(ge=1)] = 6


# --------------------------------------------------------------------------- #
# qc_rules.yaml
# --------------------------------------------------------------------------- #


class QcParamSpec(Strict):
    """A QC parameter whose value is *computed* from process conditions.

    ``transfer`` reads process parameters and upstream QC results, so a QC
    failure is always the consequence of process history rather than an
    independent random draw (§17).
    """

    id: Ident
    name: str
    stage: Ident
    phase: Literal["IN_PROCESS", "FINAL"] = "IN_PROCESS"
    unit: str = ""
    target: float
    lower_limit: float | None = None
    upper_limit: float | None = None
    #: Analytical measurement noise.
    #: The analytical method that measures this parameter, from the laboratory
    #: domain. Declared rather than inferred, and checked against that method's
    #: actual validation by ``verify-spine``.
    method_id: Ident | None = None
    #: Relative standard deviation the method demonstrated in validation. The
    #: observed variability of a QC result is the process contribution and this
    #: combined in quadrature, because a result carries the error of the
    #: measurement as well as the variability of the product. Revalidating the
    #: method tighter makes the plant's QC tighter, which is the point.
    analytical_rsd: NonNegative | None = None
    noise_sigma: NonNegative = 0.0
    transfer: Transfer = Field(default_factory=Transfer)
    #: Extra variance contributed by machine ill-health during the stage.
    health_sensitivity: float = 0.0
    #: Number of individual determinations (e.g. 10 tablets for uniformity).
    sample_size: Annotated[int, Field(ge=1)] = 1
    #: Out-of-trend band as a fraction of the spec width.
    oot_fraction: Probability = 0.15


class QcRulesConfig(Strict):
    parameters: list[QcParamSpec] = Field(min_length=1)
    results: list[Ident] = Field(default_factory=lambda: ["PASS", "FAIL", "OOS", "OOT"])
    #: A batch is rejected when any FINAL parameter fails.
    reject_on_final_failure: bool = True


# --------------------------------------------------------------------------- #
# failures.yaml
# --------------------------------------------------------------------------- #


class PrecursorSpec(Strict):
    """How a developing failure shows up in a sensor tag before the fault.

    This is the observable causal chain of §16: the same degradation index
    drives every precursor, so the tags move together for a reason.
    """

    tag: Ident
    #: Fractional change at full degradation (0.47 == +47%).
    delta_fraction: float = 0.0
    #: Absolute change at full degradation, applied after ``delta_fraction``.
    delta_absolute: float = 0.0
    #: Shape of the progression from onset to fault.
    curve: Literal["linear", "exponential", "quadratic", "late_knee"] = "exponential"
    #: Extra measurement variance as degradation proceeds.
    sigma_growth: NonNegative = 0.0


class RepairSpec(Strict):
    duration_hours: Positive = 4.0
    cost: NonNegative = 500.0
    parts: list[str] = Field(default_factory=list)
    technicians: Annotated[int, Field(ge=1)] = 1


class FailureEffects(Strict):
    """Production and quality consequences once the fault is active."""

    production_rate_factor: NonNegative = 0.0
    reject_rate_add: Probability = 0.0
    #: Process parameter shifts applied while degrading, e.g. tablet weight drift.
    process_parameter_shifts: dict[Ident, float] = Field(default_factory=dict)
    #: Fractional inflation of process parameter variability.
    process_variability_gain: NonNegative = 0.0


class FailureModeSpec(Strict):
    id: Ident
    category: Ident
    description: str = ""
    #: Equipment classes this mode can affect; empty means every class.
    equipment_classes: list[Ident] = Field(default_factory=list)
    #: Sensor profiles this mode can affect; empty means every profile.
    sensor_profiles: list[Ident] = Field(default_factory=list)
    #: Mean time between failures in *operating* hours.
    mtbf_operating_hours: Positive = 4000.0
    #: Weibull shape: >1 wear-out, ==1 memoryless/random.
    weibull_beta: Positive = 1.0
    incubation_hours_min: Positive = 6.0
    incubation_hours_max: Positive = 240.0
    precursors: list[PrecursorSpec] = Field(default_factory=list)
    effects: FailureEffects = Field(default_factory=FailureEffects)
    repair: RepairSpec = Field(default_factory=RepairSpec)
    severity: Ident = "MAJOR"
    #: The hidden ground-truth root cause for this mode.
    root_cause: Ident
    root_cause_description: str = ""
    #: Whether a warning state is raised before the fault.
    detectable: bool = True
    #: Degradation fraction at which the machine enters its warning state.
    warning_threshold: Probability = 0.55
    #: Preventive maintenance resets degradation; how strongly.
    pm_effectiveness: Probability = 0.9

    @model_validator(mode="after")
    def _incubation_ordered(self) -> FailureModeSpec:
        if self.incubation_hours_max < self.incubation_hours_min:
            raise ValueError(
                f"failure mode {self.id!r}: incubation_hours_max "
                f"({self.incubation_hours_max}) < incubation_hours_min "
                f"({self.incubation_hours_min})"
            )
        return self


class HazardFactors(Strict):
    """Multipliers turning a base hazard into a context-dependent one (§14).

    Each is a :class:`Transfer` over a named driver, so the shape of every factor
    is configuration rather than code.
    """

    age: Transfer = Field(
        default_factory=lambda: Transfer(intercept=1.0, terms=[Term(input="age_years", coef=0.06)])
    )
    operating_hours: Transfer = Field(
        default_factory=lambda: Transfer(
            intercept=1.0, terms=[Term(input="operating_khours", coef=0.05)]
        )
    )
    maintenance_debt: Transfer = Field(
        default_factory=lambda: Transfer(
            intercept=1.0, terms=[Term(input="pm_overdue_ratio", coef=0.8)], clip_max=4.0
        )
    )
    load: Transfer = Field(
        default_factory=lambda: Transfer(
            intercept=0.6, terms=[Term(input="load_factor", coef=0.4)]
        )
    )
    environment: Transfer = Field(
        default_factory=lambda: Transfer(
            intercept=1.0, terms=[Term(input="environment_stress", coef=0.3)]
        )
    )
    operator: Transfer = Field(
        default_factory=lambda: Transfer(
            intercept=1.0, terms=[Term(input="operator_inexperience", coef=0.25)]
        )
    )


class FailuresConfig(Strict):
    failure_modes: list[FailureModeSpec] = Field(min_length=1)
    hazard_factors: HazardFactors = Field(default_factory=HazardFactors)
    #: Global scaling knob for tuning overall failure frequency.
    hazard_scale: Positive = 1.0
    #: Ceiling on simultaneously degrading modes per machine.
    max_concurrent_modes: Annotated[int, Field(ge=1)] = 2


# --------------------------------------------------------------------------- #
# maintenance.yaml
# --------------------------------------------------------------------------- #


class MaintenanceConfig(Strict):
    types: list[Ident] = Field(
        default_factory=lambda: ["PREVENTIVE", "CORRECTIVE", "EMERGENCY", "PREDICTIVE"]
    )
    technician_pool: Annotated[int, Field(ge=1)] = 8
    #: PM is scheduled this far before the interval elapses.
    pm_lead_time_hours: NonNegative = 24.0
    #: Probability PM is skipped/deferred, which accrues maintenance debt.
    pm_deferral_probability: Probability = 0.18
    pm_deferral_hours: Positive = 72.0
    #: A warning state triggers predictive maintenance with this probability.
    predictive_response_probability: Probability = 0.45
    predictive_response_delay_hours: Positive = 8.0
    corrective_delay_hours: Positive = 1.0
    hourly_labour_cost: NonNegative = 60.0
    #: Fraction of degradation removed by a corrective repair.
    corrective_effectiveness: Probability = 1.0


# --------------------------------------------------------------------------- #
# rca_rules.yaml
# --------------------------------------------------------------------------- #


class RcaEvidenceRule(Strict):
    """One piece of quantified evidence the RCA engine looks for."""

    id: Ident
    description: str
    #: Sensor tag whose change is measured across the lookback window.
    tag: Ident | None = None
    #: How to summarise the tag over the window.
    #:
    #: ``delta`` compares the mean of the window's second half against its first
    #: half; ``variance_ratio`` compares their standard deviations, which is what
    #: distinguishes an erratic instrument from a genuine trend.
    statistic: Literal["delta", "variance_ratio"] = "delta"
    #: Threshold for the chosen statistic. A negative value means the evidence
    #: matches when the statistic falls *below* it, so a drop can be evidence too.
    min_delta_fraction: float | None = None
    #: Signal drawn from maintenance history rather than sensors.
    signal: Ident | None = None
    min_value: float | None = None
    weight: Positive = 1.0


class RcaRuleSpec(Strict):
    """Maps a pattern of evidence onto a candidate root cause."""

    id: Ident
    root_cause: Ident
    #: Failure categories this rule is applicable to; empty means any.
    categories: list[Ident] = Field(default_factory=list)
    evidence: list[Ident] = Field(default_factory=list)
    five_why: list[str] = Field(default_factory=list)
    fishbone_category: Ident = "MACHINE"
    corrective_action: str = ""
    preventive_action: str = ""
    #: Minimum accumulated evidence weight before this rule may be selected.
    min_score: NonNegative = 1.0


class RcaRulesConfig(Strict):
    evidence_rules: list[RcaEvidenceRule] = Field(default_factory=list)
    rules: list[RcaRuleSpec] = Field(min_length=1)
    #: Root cause reported when no rule reaches its threshold.
    fallback_root_cause: Ident = "UNDETERMINED"
    verification_batches: Annotated[int, Field(ge=1)] = 3


# --------------------------------------------------------------------------- #
# deviations.yaml (severity policy) — kept inside qc/deviation config
# --------------------------------------------------------------------------- #


class DeviationRule(Strict):
    id: Ident
    #: Event type that opens a deviation.
    trigger_event: Ident
    severity: Ident = "MAJOR"
    title: str = ""
    requires_rca: bool = True
    requires_capa: bool = True


class DeviationsConfig(Strict):
    rules: list[DeviationRule] = Field(min_length=1)
    statuses: list[Ident] = Field(
        default_factory=lambda: ["OPEN", "INVESTIGATION", "CAPA_PENDING", "CLOSED"]
    )


# --------------------------------------------------------------------------- #
# scenarios.yaml
# --------------------------------------------------------------------------- #


class ScenarioAction(Strict):
    """One scripted intervention inside a scenario."""

    type: Ident
    at_hours: NonNegative = 0.0
    machine_id: Ident | None = None
    unit_id: Ident | None = None
    equipment_class: Ident | None = None
    failure_mode: Ident | None = None
    severity: Ident | None = None
    count: Annotated[int, Field(ge=1)] = 1
    #: Free-form knobs consumed by the specific action type.
    params: dict[str, Any] = Field(default_factory=dict)


class ScenarioSpec(Strict):
    id: Ident
    description: str = ""
    duration_hours: Positive = 24.0
    #: Config overrides applied for the scenario's duration, dotted-path keyed.
    overrides: dict[str, Any] = Field(default_factory=dict)
    actions: list[ScenarioAction] = Field(default_factory=list)


class ScenariosConfig(Strict):
    scenarios: list[ScenarioSpec] = Field(min_length=1)


# --------------------------------------------------------------------------- #
# sinks.yaml
# --------------------------------------------------------------------------- #


class MqttSinkOptions(Strict):
    host: str = "localhost"
    port: Annotated[int, Field(ge=1, le=65535)] = 1883
    username: str | None = None
    password: str | None = None
    client_id: str = "pharma-sim"
    qos: Literal[0, 1, 2] = 0
    retain: bool = False
    keepalive: Annotated[int, Field(ge=1)] = 60
    telemetry_topic: str = "pharma/{plant_id}/{unit_id}/{machine_id}/telemetry"
    event_topic: str = "pharma/{plant_id}/{unit_id}/{machine_id}/events"
    #: Messages buffered while the broker is unreachable.
    offline_buffer: Annotated[int, Field(ge=0)] = 50_000
    connect_timeout_s: Positive = 5.0


class JsonlSinkOptions(Strict):
    #: ``-`` means stdout.
    path: str = "-"
    rotate_mb: NonNegative = 0.0
    include_telemetry: bool = True
    include_events: bool = True


class SinkSpec(Strict):
    name: Ident
    type: Literal["jsonl", "mqtt"]
    enabled: bool = False
    #: Bounded queue depth; overflow drops oldest and is counted, never silent.
    queue_size: Annotated[int, Field(ge=1)] = 100_000
    batch_size: Annotated[int, Field(ge=1)] = 500
    flush_interval_s: Positive = 1.0
    jsonl: JsonlSinkOptions = Field(default_factory=JsonlSinkOptions)
    mqtt: MqttSinkOptions = Field(default_factory=MqttSinkOptions)


class SinksConfig(Strict):
    sinks: list[SinkSpec] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# storage.yaml
# --------------------------------------------------------------------------- #


class TransactionalStorage(Strict):
    backend: Literal["sqlite", "postgres"] = "sqlite"
    dsn: str = "./data/factory.db"
    schema_name: str = "oltp"
    batch_size: Annotated[int, Field(ge=1)] = 1000


class TimeseriesStorage(Strict):
    backend: Literal["parquet", "clickhouse", "timescale"] = "parquet"
    dsn: str = "./data/telemetry"
    database: str = "pharma_ts"
    table: str = "sensor_readings"
    schema_name: str = "ts"
    partition_by: list[Ident] = Field(default_factory=lambda: ["date"])
    #: Rows buffered before a columnar flush.
    batch_size: Annotated[int, Field(ge=1)] = 50_000


class EvaluationStorage(Strict):
    """Ground truth and prediction labels, deliberately kept apart (§25)."""

    backend: Literal["parquet", "postgres", "clickhouse"] = "parquet"
    dsn: str = "./data/eval"
    schema_name: str = "eval"
    batch_size: Annotated[int, Field(ge=1)] = 10_000


class StorageConfig(Strict):
    transactional: TransactionalStorage = Field(default_factory=TransactionalStorage)
    timeseries: TimeseriesStorage = Field(default_factory=TimeseriesStorage)
    evaluation: EvaluationStorage = Field(default_factory=EvaluationStorage)


# --------------------------------------------------------------------------- #
# The whole configuration
# --------------------------------------------------------------------------- #


class FactoryConfig(Strict):
    """Every configuration file, validated and bundled."""

    plant: PlantConfig
    states: StatesConfig
    event_types: EventTypesConfig
    units: UnitsConfig
    machines: MachinesConfig
    sensors: SensorsConfig
    shifts: ShiftsConfig
    products: ProductsConfig
    qc_rules: QcRulesConfig
    failures: FailuresConfig
    maintenance: MaintenanceConfig
    rca_rules: RcaRulesConfig
    deviations: DeviationsConfig
    scenarios: ScenariosConfig
    sinks: SinksConfig
    storage: StorageConfig


#: Maps each config file stem to the field it populates on :class:`FactoryConfig`.
CONFIG_FILES: dict[str, str] = {
    "plant": "plant",
    "states": "states",
    "event_types": "event_types",
    "units": "units",
    "machines": "machines",
    "sensors": "sensors",
    "shifts": "shifts",
    "products": "products",
    "qc_rules": "qc_rules",
    "failures": "failures",
    "maintenance": "maintenance",
    "rca_rules": "rca_rules",
    "deviations": "deviations",
    "scenarios": "scenarios",
    "sinks": "sinks",
    "storage": "storage",
}

#: Files that may be omitted, falling back to model defaults.
OPTIONAL_CONFIG_FILES: frozenset[str] = frozenset({"sinks", "storage", "maintenance", "scenarios"})
