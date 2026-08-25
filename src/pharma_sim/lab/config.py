"""Pydantic models for the laboratory configuration files.

Same two conventions as the manufacturing config
(:mod:`pharma_sim.config.models`): identifiers are plain strings rather than
enums, so a new degradant or a new suitability criterion is a config edit; and
anything the engine needs to compute a number is declared rather than evaluated.

The models are the only place the shape of ``config/lab/*.yaml`` is described.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Ident = Annotated[str, Field(min_length=1, max_length=64)]
Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
Positive = Annotated[float, Field(gt=0.0)]
NonNegative = Annotated[float, Field(ge=0.0)]

__all__ = [
    "LabConfig",
    "SubstancesConfig",
    "MethodsConfig",
    "InstrumentsConfig",
    "CdsConfig",
    "ValidationsConfig",
    "Method",
    "MethodAnalyte",
    "Conditions",
    "Instrument",
    "Analyst",
    "ColumnUnit",
    "SuitabilityCriterion",
    "Validation",
    "StabilityConfig",
    "StabilityProtocol",
    "StorageCondition",
    "FormulationsConfig",
    "Prototype",
    "DoeConfig",
    "Factor",
    "ResponseSpec",
    "LAB_CONFIG_FILES",
]


class Strict(BaseModel):
    """Rejects unknown keys, so a typo in a long config is not silently ignored."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- #
# substances.yaml
# --------------------------------------------------------------------------- #


class SolubilityPoint(Strict):
    ph: float
    solubility: NonNegative


class Stereo(Strict):
    stereocentres: int = 0
    atropisomeric_axes: int = 0
    atropisomer_interconversion_half_life_h_25c: float | None = None
    atropisomer_interconversion_half_life_h_60c: float | None = None


class Polymorph(Strict):
    form: str
    nature: str
    relative_stability: Fraction
    melting_point_c: float
    preferred: bool = False


class DegradationPathway(Strict):
    pathway: str
    product: Ident
    relative_rate: NonNegative


class Substance(Strict):
    substance_id: Ident
    code: str
    name: str
    substance_class: str
    modality: str | None = None
    mechanism: str | None = None
    origin: str | None = None
    parent: Ident | None = None
    molecular_weight: Positive
    molecular_formula: str
    pka: list[float] = Field(default_factory=list)
    logp: float | None = None
    bcs_class: int | None = None
    ph_solubility_mg_ml: list[SolubilityPoint] = Field(default_factory=list)
    hygroscopicity: str | None = None
    occupational_exposure_band: int | None = None
    permitted_daily_exposure_ug: float | None = None
    stereo: Stereo | None = None
    polymorphs: list[Polymorph] = Field(default_factory=list)
    degradation_pathways: list[DegradationPathway] = Field(default_factory=list)


class Excipient(Strict):
    excipient_id: Ident
    name: str
    function: str


class SubstancesConfig(Strict):
    substances: list[Substance] = Field(default_factory=list)
    excipients: list[Excipient] = Field(default_factory=list)

    def by_id(self, substance_id: str) -> Substance | None:
        for substance in self.substances:
            if substance.substance_id == substance_id:
                return substance
        return None


# --------------------------------------------------------------------------- #
# methods.yaml
# --------------------------------------------------------------------------- #


class Conditions(Strict):
    """Chromatographic operating conditions.

    Also used to express a deliberately varied condition in a robustness study,
    via :meth:`varied`.
    """

    flow_rate_ml_min: Positive
    column_temperature_c: float
    organic_percent: float
    mobile_phase_ph: float
    detection_nm: Positive
    injection_volume_ul: Positive

    def varied(self, factor: str, delta: float) -> Conditions:
        """A copy with one factor shifted, as a robustness condition."""
        if factor not in type(self).model_fields:
            raise KeyError(f"{factor} is not a chromatographic condition")
        values = self.model_dump()
        values[factor] = values[factor] + delta
        return Conditions(**values)

    def as_dict(self) -> dict[str, float]:
        return self.model_dump()


class MethodColumn(Strict):
    column_type_id: Ident
    description: str
    expected_lifetime_injections: int


class Detector(Strict):
    noise_sigma: NonNegative = 0.0
    baseline_offset: float = 0.0
    baseline_drift_per_min: float = 0.0
    baseline_wander: float = 0.0
    baseline_wander_cycles: float = 1.5


class Variability(Strict):
    """Sources of scatter, from which validated precision emerges."""

    sample_preparation_rsd: NonNegative = 0.0
    injection_rsd: NonNegative = 0.0
    retention_jitter_min: NonNegative = 0.0
    analyst_bias_sd: NonNegative = 0.0
    day_bias_sd: NonNegative = 0.0


class ColumnAgeing(Strict):
    sigma_per_injection: float = 0.0
    tau_per_injection: float = 0.0
    retention_per_injection: float = 0.0


class MethodAnalyte(Strict):
    analyte_id: Ident
    peak_name: str
    role: str
    retention_time_min: Positive
    sigma_min: Positive
    tau_min: NonNegative
    #: Response per unit concentration, in the method's declared concentration units.
    response_factor: Positive
    specification_percent: float | None = None
    #: Fractional change in retention per unit deviation from the reference
    #: condition. ``flow_rate_ml_min`` is an exponent, not a fraction.
    retention_sensitivity: dict[str, float] = Field(default_factory=dict)
    efficiency_sensitivity: dict[str, float] = Field(default_factory=dict)
    tailing_sensitivity: dict[str, float] = Field(default_factory=dict)


class Method(Strict):
    method_id: Ident
    name: str
    purpose: str
    technique: str
    cds: str
    matrix: str
    column: MethodColumn
    nominal_conditions: Conditions
    run_time_min: Positive
    sampling_hz: Positive
    standard_concentration_ug_ml: Positive
    detector: Detector = Detector()
    variability: Variability = Variability()
    critical_pair: tuple[Ident, Ident] | None = None
    analytes: list[MethodAnalyte]
    column_ageing: ColumnAgeing = ColumnAgeing()

    @model_validator(mode="after")
    def _check_references(self) -> Method:
        ids = {analyte.analyte_id for analyte in self.analytes}
        if len(ids) != len(self.analytes):
            raise ValueError("duplicate analyte_id within a method")
        if self.critical_pair:
            missing = [a for a in self.critical_pair if a not in ids]
            if missing:
                raise ValueError(
                    f"critical_pair references analytes not in the method: {missing}"
                )
        return self

    def analyte(self, analyte_id: str) -> MethodAnalyte | None:
        for analyte in self.analytes:
            if analyte.analyte_id == analyte_id:
                return analyte
        return None

    @property
    def assay_analyte(self) -> MethodAnalyte:
        for analyte in self.analytes:
            if analyte.role == "ASSAY":
                return analyte
        return self.analytes[0]


class MethodsConfig(Strict):
    methods: list[Method] = Field(default_factory=list)

    def by_id(self, method_id: str) -> Method | None:
        for method in self.methods:
            if method.method_id == method_id:
                return method
        return None


# --------------------------------------------------------------------------- #
# instruments.yaml
# --------------------------------------------------------------------------- #


class Qualification(Strict):
    status: str
    iq_oq_date: date
    pq_date: date
    requalification_interval_days: int


class Calibration(Strict):
    interval_days: int
    last_calibrated: date
    response_drift_per_day: float = 0.0


class Instrument(Strict):
    instrument_id: Ident
    name: str
    instrument_class: str
    vendor: str
    model: str
    cds: str
    cds_project: str
    location: str
    qualification: Qualification
    calibration: Calibration
    #: Systematic response offset, as a fraction.
    bias: float = 0.0
    precision_rsd: NonNegative = 0.0


class ColumnUnit(Strict):
    column_id: Ident
    column_type_id: Ident
    serial: str
    installed: date
    injections_at_start: int = 0


class Analyst(Strict):
    analyst_id: Ident
    name: str
    grade: str
    bias: float = 0.0
    precision_rsd: NonNegative = 0.0


class ReferenceStandard(Strict):
    standard_id: Ident
    substance_id: Ident
    purity_percent: Positive
    lot: str
    expiry: date


class InstrumentsConfig(Strict):
    instruments: list[Instrument] = Field(default_factory=list)
    columns: list[ColumnUnit] = Field(default_factory=list)
    analysts: list[Analyst] = Field(default_factory=list)
    reference_standards: list[ReferenceStandard] = Field(default_factory=list)

    def instrument(self, instrument_id: str) -> Instrument | None:
        for instrument in self.instruments:
            if instrument.instrument_id == instrument_id:
                return instrument
        return None

    def column(self, column_id: str) -> ColumnUnit | None:
        for column in self.columns:
            if column.column_id == column_id:
                return column
        return None

    def analyst(self, analyst_id: str) -> Analyst | None:
        for analyst in self.analysts:
            if analyst.analyst_id == analyst_id:
                return analyst
        return None


# --------------------------------------------------------------------------- #
# cds.yaml
# --------------------------------------------------------------------------- #

Operator = Literal["LE", "GE", "EQ", "BETWEEN"]


class SuitabilityCriterion(Strict):
    metric: str
    analyte_id: Ident | None = None
    versus: Ident | None = None
    operator: Operator
    limit: float | list[float]

    def passes(self, value: float | None) -> bool:
        """Evaluate the criterion. ``None`` is a failure, never a skip.

        An unmeasurable descriptor means the method did not demonstrate what the
        criterion asks for -- an unresolved critical pair has no resolution to
        report, and treating that as "not evaluated" would pass a failing set.
        """
        if value is None:
            return False
        if self.operator == "LE":
            return value <= float(self.limit)  # type: ignore[arg-type]
        if self.operator == "GE":
            return value >= float(self.limit)  # type: ignore[arg-type]
        if self.operator == "EQ":
            return value == float(self.limit)  # type: ignore[arg-type]
        low, high = self.limit  # type: ignore[misc]
        return low <= value <= high


class SuitabilitySpec(Strict):
    replicate_injections: int
    criteria: list[SuitabilityCriterion]


class AuditEvent(Strict):
    code: str
    category: str
    signature_required: bool = False
    reason_required: bool = False


class AuditTrailConfig(Strict):
    events: list[AuditEvent] = Field(default_factory=list)
    manual_integration_rate: Fraction = 0.0
    reprocessing_rate: Fraction = 0.0
    aborted_injection_rate: Fraction = 0.0


class CdsConfig(Strict):
    system_suitability: dict[str, SuitabilitySpec] = Field(default_factory=dict)
    suitability_first_attempt_failure_rate: Fraction = 0.0
    max_suitability_attempts: int = 3
    resolution_solution: dict[str, dict[str, float]] = Field(default_factory=dict)
    audit_trail: AuditTrailConfig = AuditTrailConfig()


# --------------------------------------------------------------------------- #
# validation.yaml
# --------------------------------------------------------------------------- #


class Criterion(Strict):
    metric: str
    operator: Operator
    limit: float | list[float]

    def passes(self, value: float | None) -> bool:
        return SuitabilityCriterion(
            metric=self.metric, operator=self.operator, limit=self.limit
        ).passes(value)


class StressCondition(Strict):
    condition: str
    description: str
    target_degradation_percent: NonNegative


class SpecificityExperiment(Strict):
    replicates: int
    stress_conditions: list[StressCondition] = Field(default_factory=list)
    criteria: list[Criterion] = Field(default_factory=list)


class LevelsExperiment(Strict):
    levels_percent: list[float]
    replicates: int
    criteria: list[Criterion] = Field(default_factory=list)


class SingleLevelExperiment(Strict):
    level_percent: float
    replicates: int
    analyst_id: Ident | None = None
    instrument_id: Ident | None = None
    day_offset: int = 0
    criteria: list[Criterion] = Field(default_factory=list)


class RobustnessCondition(Strict):
    factor: str
    delta: float


class RobustnessExperiment(Strict):
    replicates: int
    conditions: list[RobustnessCondition]
    criteria: list[Criterion] = Field(default_factory=list)


class SolutionStabilityExperiment(Strict):
    timepoints_h: list[float]
    solutions: list[str]
    replicates: int
    criteria: list[Criterion] = Field(default_factory=list)


class FilterExperiment(Strict):
    filters: list[str]
    replicates: int
    criteria: list[Criterion] = Field(default_factory=list)


class ValidationExperiments(Strict):
    specificity: SpecificityExperiment | None = None
    linearity: LevelsExperiment | None = None
    accuracy: LevelsExperiment | None = None
    repeatability: SingleLevelExperiment | None = None
    intermediate_precision: SingleLevelExperiment | None = None
    detection_limits: LevelsExperiment | None = None
    robustness: RobustnessExperiment | None = None
    solution_stability: SolutionStabilityExperiment | None = None
    filter_validation: FilterExperiment | None = None


class Validation(Strict):
    validation_id: Ident
    method_id: Ident
    protocol: str
    title: str
    started: date
    lead_analyst: Ident
    instrument_id: Ident
    column_id: Ident
    experiments: ValidationExperiments


class ValidationsConfig(Strict):
    validations: list[Validation] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# stability.yaml
# --------------------------------------------------------------------------- #


class StorageCondition(Strict):
    condition_id: Ident
    label: str
    temperature_c: float
    humidity_pct: Positive
    timepoints_months: list[float]


class Kinetics(Strict):
    substance: Ident
    activation_energy_kj_mol: Positive
    reference_temperature_c: float
    reference_humidity_pct: Positive
    humidity_exponent: float
    reference_rate_percent_per_month: NonNegative
    degradant_split: dict[Ident, Fraction]
    release_levels_percent: dict[Ident, NonNegative]


class SecondaryAttribute(Strict):
    release_value: float
    change_per_month_at_reference: float
    lower_limit: float | None = None
    upper_limit: float | None = None


class Limit(Strict):
    lower: float | None = None
    upper: float | None = None


class Specification(Strict):
    assay: Limit
    total_impurities: Limit
    individual_impurity: Limit


class ShelfLifeRules(Strict):
    fit_condition: Ident
    confidence: Fraction
    maximum_months: Positive
    round_down_to_months: Positive
    investigate_out_of_specification: bool = True


class SignificantChange(Strict):
    assay_change_percent: NonNegative
    any_specification_exceeded: bool = True


class StabilityProtocol(Strict):
    protocol_id: Ident
    product_id: Ident
    method_id: Ident
    title: str
    batches: int
    package: str
    orientations: list[str]
    replicates_per_pull: int
    analysts: list[Ident]
    instruments: list[Ident]


class StabilityConfig(Strict):
    conditions: list[StorageCondition]
    kinetics: Kinetics
    secondary_attributes: dict[str, SecondaryAttribute]
    specification: Specification
    shelf_life: ShelfLifeRules
    significant_change: SignificantChange
    protocols: list[StabilityProtocol]

    def condition(self, condition_id: str) -> StorageCondition | None:
        return next((c for c in self.conditions if c.condition_id == condition_id), None)


# --------------------------------------------------------------------------- #
# formulations.yaml and doe.yaml
# --------------------------------------------------------------------------- #


class PreformulationStudy(Strict):
    study: str
    technique: str
    outcome_from: str | None = None


class Compatibility(Strict):
    excipient_id: Ident
    outcome: str
    degradation_percent_4wk: NonNegative


class Preformulation(Strict):
    substance: Ident
    studies: list[PreformulationStudy]
    compatibility: list[Compatibility]


class Prototype(Strict):
    formulation_id: Ident
    name: str
    route: str
    role: str
    description: str
    composition_percent: dict[Ident, Positive]
    api_form: str | None = None
    api_d50_um: Positive | None = None
    matches: Ident | None = None


class Range(Strict):
    target: float | None = None
    minimum: float | None = None
    maximum: float | None = None


class TargetProduct(Strict):
    product_id: Ident
    strength_mg: Positive
    tablet_weight_mg: Positive
    specification: dict[str, Range]


class FormulationsConfig(Strict):
    preformulation: Preformulation
    prototypes: list[Prototype]
    target_product: TargetProduct

    def prototype(self, formulation_id: str) -> Prototype | None:
        return next(
            (p for p in self.prototypes if p.formulation_id == formulation_id), None
        )


class DesignSpec(Strict):
    type: str
    factors: int
    fraction: int
    resolution: str
    generator: dict[str, list[str]]
    centre_points: int
    replicates: int = 1


class Factor(Strict):
    factor: str
    name: Ident
    unit: str
    low: float
    high: float
    centre: float


class ResponseTerm(Strict):
    factor: Ident
    coef: float


class TrueResponse(Strict):
    intercept: float
    terms: list[ResponseTerm]
    #: Coefficient on ``(factor - centre) ** 2``, per factor. A two-level design
    #: cannot fit these; they exist so the centre points can detect them.
    quadratic: dict[Ident, float] = Field(default_factory=dict)


class ResponseSpec(Strict):
    response: Ident
    unit: str
    #: TARGET, MINIMISE or MAXIMISE.
    direction: str
    noise_sigma: NonNegative
    true_response: TrueResponse
    form_effect: dict[str, float] = Field(default_factory=dict)
    target: float | None = None
    minimum: float | None = None
    maximum: float | None = None


class Optimisation(Strict):
    method: str
    weights: dict[Ident, Positive]
    grid_points: int
    setpoint_tolerance: dict[Ident, Positive]
    curvature_threshold: Positive = 2.0


class DoeStudy(Strict):
    study_id: Ident
    title: str
    prototypes: list[Ident]
    started: date
    analyst: Ident
    method_id: Ident


class DoeConfig(Strict):
    design: DesignSpec
    factors: list[Factor]
    responses: list[ResponseSpec]
    optimisation: Optimisation
    studies: list[DoeStudy]

    def factor(self, name: str) -> Factor | None:
        return next((f for f in self.factors if f.name == name), None)


class LabConfig(Strict):
    """Every laboratory configuration file, validated and bundled."""

    substances: SubstancesConfig
    methods: MethodsConfig
    instruments: InstrumentsConfig
    cds: CdsConfig
    validations: ValidationsConfig
    stability: StabilityConfig
    formulations: FormulationsConfig
    doe: DoeConfig


#: Maps each file stem under ``config/lab/`` to the field it populates.
LAB_CONFIG_FILES: dict[str, str] = {
    "substances": "substances",
    "methods": "methods",
    "instruments": "instruments",
    "cds": "cds",
    "validation": "validations",
    "stability": "stability",
    "formulations": "formulations",
    "doe": "doe",
}
