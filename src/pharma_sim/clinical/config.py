"""Pydantic models for the clinical configuration files."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

Ident = Annotated[str, Field(min_length=1, max_length=64)]
Fraction = Annotated[float, Field(ge=0.0, le=1.0)]
Positive = Annotated[float, Field(gt=0.0)]
NonNegative = Annotated[float, Field(ge=0.0)]

__all__ = [
    "ClinicalConfig",
    "ProtocolConfig",
    "Arm",
    "SitesConfig",
    "Site",
    "Country",
    "Archetype",
    "Milestone",
    "CrfConfig",
    "Form",
    "Item",
    "EditCheck",
    "ValueSource",
    "MonitoringConfig",
    "TmfConfig",
    "Artifact",
    "SafetyConfig",
    "AdverseEventSpec",
    "DoseModificationConfig",
    "DoseRule",
    "TumourConfig",
    "RecistConfig",
    "ReaderConfig",
    "OrganConfig",
    "GrowthArm",
    "HazardConfig",
    "AssessmentSchedule",
    "CLINICAL_CONFIG_FILES",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecistConfig(Strict):
    version: str = "1.1"
    measurable_min_mm: Positive = 10.0
    nodal_measurable_min_mm: Positive = 15.0
    nodal_normal_max_mm: Positive = 10.0
    max_target_lesions: int = 5
    max_target_per_organ: int = 2
    partial_response_decrease: Fraction = 0.30
    progression_increase: Fraction = 0.20
    progression_min_absolute_mm: NonNegative = 5.0
    confirmation_required: bool = False
    confirmation_min_weeks: NonNegative = 4.0
    stable_disease_min_weeks: NonNegative = 6.0


class ReaderConfig(Strict):
    reader_id: Ident
    role: str
    #: Systematic tendency to read larger or smaller, as a fraction.
    bias: float = 0.0
    #: Coefficient of variation of a single lesion measurement.
    cv: NonNegative = 0.0


class MeasurementConfig(Strict):
    quantisation_mm: Positive = 1.0
    too_small_to_measure_mm: Positive = 5.0
    #: How strongly a reader prefers the larger lesion when selecting targets.
    #: At 1.0 both readers always choose identically.
    selection_size_preference: Fraction = 1.0
    #: Probability that a reader concurs a new finding is malignant.
    new_lesion_concurrence: Fraction = 1.0
    readers: list[ReaderConfig]

    def reader(self, reader_id: str) -> ReaderConfig | None:
        for reader in self.readers:
            if reader.reader_id == reader_id:
                return reader
        return None


class OrganConfig(Strict):
    organ: Ident
    weight: Positive
    nodal: bool = False


class CountDistribution(Strict):
    minimum: int
    maximum: int
    mode: int


class SizeDistribution(Strict):
    median: Positive
    log_sd: Positive
    minimum: Positive
    maximum: Positive


class BaselineConfig(Strict):
    measurable_lesion_count: CountDistribution
    target_lesion_count: CountDistribution
    non_target_lesion_count: CountDistribution
    diameter_mm: SizeDistribution
    nodal_short_axis_mm: SizeDistribution


class Normal(Strict):
    mean: float
    sd: NonNegative


class GrowthArm(Strict):
    sensitive_fraction: Normal
    shrinkage_rate_per_week: Normal
    growth_rate_per_week: Normal


class GrowthConfig(Strict):
    arms: dict[str, GrowthArm]


class HazardConfig(Strict):
    base_hazard_per_week: NonNegative
    burden_exponent: float = 1.0


class AssessmentSchedule(Strict):
    first_week: Positive
    interval_weeks: Positive
    switch_week: Positive
    later_interval_weeks: Positive
    window_days: NonNegative
    slip_days_sd: NonNegative = 0.0
    missed_probability: Fraction = 0.0


class TumourConfig(Strict):
    recist: RecistConfig
    measurement: MeasurementConfig
    organs: list[OrganConfig]
    baseline: BaselineConfig
    growth: GrowthConfig
    new_lesion: HazardConfig
    non_target_progression: HazardConfig
    death: HazardConfig
    assessment_schedule: AssessmentSchedule


class Arm(Strict):
    arm_id: Ident
    label: str
    allocation: int
    subjects: int


class Endpoint(Strict):
    endpoint_id: Ident
    label: str
    evaluator: Ident


class Endpoints(Strict):
    primary: list[Endpoint]
    secondary: list[Endpoint] = Field(default_factory=list)


class Enrolment(Strict):
    first_subject_in: date
    planned_accrual_weeks: NonNegative


class Analysis(Strict):
    cutoff_weeks_from_fsi: Positive
    horizon_weeks: Positive


class ProtocolConfig(Strict):
    study_id: Ident
    title: str
    phase: str
    indication: str
    blinding: str
    cycle_length_days: int
    arms: list[Arm]
    endpoints: Endpoints
    enrolment: Enrolment
    analysis: Analysis

    def arm(self, arm_id: str) -> Arm | None:
        for arm in self.arms:
            if arm.arm_id == arm_id:
                return arm
        return None


# --------------------------------------------------------------------------- #
# sites.yaml — the CTMS layer
# --------------------------------------------------------------------------- #


class RegulatoryTimelines(Strict):
    cta_weeks: Normal
    ec_weeks: Normal


class Country(Strict):
    country: Ident
    name: str
    language: str
    regulatory: RegulatoryTimelines


class Archetype(Strict):
    """A site performance profile. Sites reference one rather than restating it."""

    archetype: Ident
    description: str
    enrolment_per_month: Normal
    contract_weeks: Normal
    entry_lag_days: Normal
    query_rate_per_form: Normal
    deviation_rate_per_subject: Normal
    query_response_days: Normal


class Site(Strict):
    site_id: Ident
    country: Ident
    name: str
    principal_investigator: str
    archetype: Ident


class Milestone(Strict):
    milestone: Ident
    #: One predecessor, several (wait for the last), or none for the chain head.
    predecessor: Ident | list[Ident] | None = None
    weeks: Normal | None = None
    #: Where the interval comes from when it is not declared here.
    source: str | None = None


class StaffTurnover(Strict):
    probability_per_site: Fraction
    starts_week: Normal
    duration_weeks: Normal
    entry_lag_multiplier: Positive


class SitesConfig(Strict):
    countries: list[Country]
    archetypes: list[Archetype]
    sites: list[Site]
    milestones: list[Milestone]
    staff_turnover: StaffTurnover

    def country(self, code: str) -> Country | None:
        return next((c for c in self.countries if c.country == code), None)

    def archetype(self, name: str) -> Archetype | None:
        return next((a for a in self.archetypes if a.archetype == name), None)

    def site(self, site_id: str) -> Site | None:
        return next((s for s in self.sites if s.site_id == site_id), None)


# --------------------------------------------------------------------------- #
# crf.yaml — the EDC layer
# --------------------------------------------------------------------------- #


class Item(Strict):
    item_id: Ident
    label: str
    type: str
    unit: str | None = None
    codelist: Ident | None = None
    sdtm: str | None = None


class Form(Strict):
    form_id: Ident
    name: str
    #: ONCE, PER_VISIT, PER_ASSESSMENT, PER_CYCLE.
    scope: str
    sdtm_domain: str | None = None
    items: list[Item]

    def item(self, item_id: str) -> Item | None:
        return next((i for i in self.items if i.item_id == item_id), None)


class EditCheck(Strict):
    check_id: Ident
    form_id: Ident
    item_id: Ident
    kind: str
    severity: str
    text: str
    low: float | None = None
    high: float | None = None
    expected: str | None = None
    before: Ident | None = None


class QueryBehaviour(Strict):
    data_management_rate_per_form: Fraction
    monitor_rate_per_form: Fraction
    requery_probability: Fraction
    max_requeries: int
    notification_days: Normal
    closure_days: Normal


class ValueSource(Strict):
    """How one item's value is produced."""

    kind: str
    mean: float | None = None
    sd: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    decimals: int = 0
    weights: dict[str, float] | None = None
    value: str | None = None
    #: For FROM_STUDY: which computed quantity to read.
    field: str | None = None


class CrfConfig(Strict):
    forms: list[Form]
    codelists: dict[str, list[str]]
    edit_checks: list[EditCheck]
    queries: QueryBehaviour
    value_sources: dict[str, ValueSource]

    def form(self, form_id: str) -> Form | None:
        return next((f for f in self.forms if f.form_id == form_id), None)

    def checks_for(self, form_id: str) -> list[EditCheck]:
        return [c for c in self.edit_checks if c.form_id == form_id]


# --------------------------------------------------------------------------- #
# monitoring.yaml
# --------------------------------------------------------------------------- #


class VisitType(Strict):
    visit_type: Ident
    name: str
    trigger: str
    milestone: Ident | None = None
    interval_weeks: Positive | None = None


class ForCauseTrigger(Strict):
    query_rate_multiple_of_mean: Positive
    minimum_subjects: int


class SdvStrategy(Strict):
    strategy: str
    critical_items: list[Ident]
    non_critical_sample_rate: Fraction


class Finding(Strict):
    finding_id: Ident
    category: str
    severity: str
    rate_per_visit: NonNegative
    text: str


class ActionItems(Strict):
    due_days: int
    overdue_probability: Fraction


class DeviationCategory(Strict):
    category: str
    classification: str
    weight: Positive


class Deviations(Strict):
    categories: list[DeviationCategory]
    eligibility_violation_excludes_from_pp: Fraction


class MonitoringConfig(Strict):
    visit_types: list[VisitType]
    for_cause_trigger: ForCauseTrigger
    source_data_verification: SdvStrategy
    findings: list[Finding]
    action_items: ActionItems
    deviations: Deviations


# --------------------------------------------------------------------------- #
# tmf_model.yaml
# --------------------------------------------------------------------------- #


class Zone(Strict):
    zone: str
    name: str


class Artifact(Strict):
    artifact: str
    name: str
    zone: str
    #: TRIAL, COUNTRY, SITE or MONITORING_VISIT.
    level: str
    arrival_weeks: Normal
    missing_probability: Fraction
    #: The milestone that makes this artifact expected. Absent for artifacts
    #: expected per monitoring visit.
    expected_at: str | None = None


class TmfConfig(Strict):
    zones: list[Zone]
    artifacts: list[Artifact]
    timeliness_target_weeks: NonNegative
    version_probability: Fraction

    def zone_name(self, zone: str) -> str:
        return next((z.name for z in self.zones if z.zone == zone), zone)


# --------------------------------------------------------------------------- #
# safety.yaml
# --------------------------------------------------------------------------- #


class Grade(Strict):
    grade: int
    label: str


class Category(Strict):
    category: Ident
    description: str


class AdverseEventSpec(Strict):
    pt_code: str
    pt: str
    soc: str
    soc_code: str
    category: Ident
    #: INVESTIGATIONAL_PRODUCT, CHEMOTHERAPY or BOTH. Drives both the arm
    #: difference in incidence and how often the investigator calls it related.
    attribution: str
    incidence: dict[Ident, Fraction]
    grade_weights: dict[int, Fraction]
    onset_weeks: Normal
    duration_days: Normal
    special_interest: str | None = None


class SeriousnessCriterion(Strict):
    criterion: str
    weight: Positive


class Seriousness(Strict):
    criteria: list[SeriousnessCriterion]
    probability_by_grade: dict[int, Fraction]


class Causality(Strict):
    related_probability: dict[str, Fraction]


class ExpeditedReporting(Strict):
    unexpected_probability: Fraction
    fatal_or_life_threatening_days: int
    other_susar_days: int
    site_awareness_days: Normal
    sponsor_notification_days: Normal


class SafetyConfig(Strict):
    meddra_version: str
    ctcae_version: str
    grades: list[Grade]
    categories: list[Category]
    adverse_events: list[AdverseEventSpec]
    seriousness: Seriousness
    causality: Causality
    expedited_reporting: ExpeditedReporting


# --------------------------------------------------------------------------- #
# dose_modification.yaml
# --------------------------------------------------------------------------- #


class DoseTrigger(Strict):
    grade_at_least: int
    category: Ident | None = None
    special_interest: str | None = None
    occurrence_at_least: int = 1


class DoseRule(Strict):
    rule_id: Ident
    action: str
    trigger: DoseTrigger
    reason: str
    resume_when: str | None = None


class ChemotherapyDelay(Strict):
    delay_for_grade_at_least: int
    category: Ident
    delay_days: int
    maximum_delays_per_cycle: int


class RelativeDoseIntensity(Strict):
    report: bool = True


class DoseModificationConfig(Strict):
    dose_levels_mg: list[float]
    starting_dose_mg: float
    discontinue_below_lowest_level: bool
    rules: list[DoseRule]
    chemotherapy: ChemotherapyDelay
    relative_dose_intensity: RelativeDoseIntensity

    def next_lower(self, dose: float) -> float | None:
        """The next dose level down, or None when there is nowhere to go."""
        lower = [level for level in self.dose_levels_mg if level < dose]
        return max(lower) if lower else None


class ClinicalConfig(Strict):
    protocol: ProtocolConfig
    tumour: TumourConfig
    sites: SitesConfig
    crf: CrfConfig
    monitoring: MonitoringConfig
    tmf: TmfConfig
    safety: SafetyConfig
    dose_modification: DoseModificationConfig


CLINICAL_CONFIG_FILES: dict[str, str] = {
    "protocol": "protocol",
    "tumour": "tumour",
    "sites": "sites",
    "crf": "crf",
    "monitoring": "monitoring",
    "tmf_model": "tmf",
    "safety": "safety",
    "dose_modification": "dose_modification",
}
