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
    accrual_weeks: NonNegative


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


class ClinicalConfig(Strict):
    protocol: ProtocolConfig
    tumour: TumourConfig


CLINICAL_CONFIG_FILES: dict[str, str] = {"protocol": "protocol", "tumour": "tumour"}
