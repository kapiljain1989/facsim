"""ICH Q2(R2) method validation as an executed study.

Every acceptance criterion in ``validation.yaml`` is evaluated against numbers
this module *measured*, by running real injections through
:class:`~pharma_sim.lab.method.MethodModel` and integrating the traces. Nothing
is asserted into a result. That is what lets the robustness study discover which
condition the method is sensitive to, and what lets a criterion fail.

The output is five record streams, ready for the relational store:

===========================  ===========================================
``sequences``                one per experiment or robustness condition
``injections``               every injection, with its conditions
``peaks``                    every integrated peak, with USP descriptors
``suitability``              one evaluated set per sequence that has one
``results``                  one row per validation metric, with verdict
``audit``                    Part 11 shaped trail over all of the above
===========================  ===========================================
"""

from __future__ import annotations

import math
import statistics as stats
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from pharma_sim.engine.ids import IdFactory
from pharma_sim.engine.rng import RngRegistry
from pharma_sim.lab.config import (
    Criterion,
    LabConfig,
    Method,
    SuitabilityCriterion,
    Validation,
)
from pharma_sim.lab.method import (
    ColumnState,
    InjectionRequest,
    InjectionResult,
    MethodModel,
    Preparation,
)

__all__ = ["ValidationRunner", "ValidationOutput", "linear_fit", "LinearFit"]


@dataclass(frozen=True, slots=True)
class LinearFit:
    """Ordinary least squares of response on concentration."""

    slope: float
    intercept: float
    r_squared: float
    #: Residual standard deviation, the sigma in LOD = 3.3 sigma / S.
    residual_sd: float
    points: int

    def concentration_for(self, response: float) -> float:
        if self.slope == 0.0:
            return 0.0
        return (response - self.intercept) / self.slope


def linear_fit(points: list[tuple[float, float]]) -> LinearFit:
    """Fit ``response = slope * concentration + intercept``.

    Written out rather than pulled from a library because the project keeps its
    dependency set to four packages, and because the residual standard deviation
    is wanted alongside the fit for the detection-limit calculation.
    """
    count = len(points)
    if count < 2:
        return LinearFit(0.0, 0.0, 0.0, 0.0, count)
    mean_x = stats.fmean(x for x, _ in points)
    mean_y = stats.fmean(y for _, y in points)
    sxx = sum((x - mean_x) ** 2 for x, _ in points)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in points)
    slope = sxy / sxx if sxx else 0.0
    intercept = mean_y - slope * mean_x

    residuals = [y - (slope * x + intercept) for x, y in points]
    sst = sum((y - mean_y) ** 2 for _, y in points)
    ssr = sum(residual**2 for residual in residuals)
    r_squared = 1.0 - ssr / sst if sst else 0.0
    residual_sd = math.sqrt(ssr / (count - 2)) if count > 2 else 0.0
    return LinearFit(slope, intercept, r_squared, residual_sd, count)


def _rsd_percent(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    mean = stats.fmean(values)
    if mean == 0.0:
        return None
    return 100.0 * stats.stdev(values) / mean


@dataclass
class ValidationOutput:
    """Everything one validation produced."""

    validation_id: str
    method_id: str
    sequences: list[dict] = field(default_factory=list)
    injections: list[dict] = field(default_factory=list)
    peaks: list[dict] = field(default_factory=list)
    suitability: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)
    #: Injection traces, kept separately because they are the bulk of the data.
    traces: list[tuple[str, list[float], list[float]]] = field(default_factory=list)

    @property
    def judged(self) -> list[dict]:
        """Results that carry a verdict. INFORMATIONAL rows report a number the
        protocol asks for without setting a limit -- a slope, an LOD -- and must
        not be counted as failures."""
        return [row for row in self.results if row["verdict"] in {"PASS", "FAIL"}]

    @property
    def failures(self) -> list[dict]:
        return [row for row in self.results if row["verdict"] == "FAIL"]

    @property
    def passed(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        by_experiment: dict[str, list[dict]] = {}
        for row in self.results:
            by_experiment.setdefault(row["experiment"], []).append(row)
        lines = [
            f"{self.validation_id}  method {self.method_id}",
            f"  sequences {len(self.sequences)}  injections {len(self.injections)}"
            f"  peaks {len(self.peaks)}  audit {len(self.audit)}",
        ]
        for experiment, rows in by_experiment.items():
            judged = [row for row in rows if row["verdict"] in {"PASS", "FAIL"}]
            failed = [row for row in judged if row["verdict"] == "FAIL"]
            mark = "PASS" if not failed else f"FAIL ({len(failed)}/{len(judged)})"
            lines.append(f"  {experiment:<24} {mark}")
            for row in rows:
                value = row["measured"]
                shown = "n/a" if value is None else f"{value:,.4f}"
                lines.append(
                    f"      {row['metric']:<32} {shown:>14}  "
                    f"{row['criterion']:<20} {row['verdict']}"
                )
        return "\n".join(lines)


class ValidationRunner:
    """Executes one validation defined in ``validation.yaml``."""

    def __init__(
        self,
        config: LabConfig,
        validation: Validation,
        rngs: RngRegistry,
        ids: IdFactory | None = None,
        *,
        keep_traces: bool = True,
    ) -> None:
        method = config.methods.by_id(validation.method_id)
        if method is None:
            raise KeyError(f"unknown method {validation.method_id}")
        self._config = config
        self._validation = validation
        self._method: Method = method
        self._model = MethodModel(method, config, rngs)
        self._rngs = rngs
        self._ids = ids or IdFactory()
        self._keep_traces = keep_traces
        self._out = ValidationOutput(validation.validation_id, method.method_id)

        instrument = config.instruments.instrument(validation.instrument_id)
        analyst = config.instruments.analyst(validation.lead_analyst)
        column = config.instruments.column(validation.column_id)
        if instrument is None or analyst is None or column is None:
            raise KeyError("validation references an unknown instrument, analyst or column")
        self._instrument = instrument
        self._analyst = analyst
        self._column = ColumnState(column.column_id, column.injections_at_start)

    # ------------------------------------------------------------------ helpers
    @property
    def _standard_concentration(self) -> float:
        return self._method.standard_concentration_ug_ml

    @property
    def _assay_id(self) -> str:
        return self._method.assay_analyte.analyte_id

    def _day(self, offset: int) -> datetime:
        started = self._validation.started
        return datetime.combine(started + timedelta(days=offset), time(9, 0))

    def _open_sequence(
        self, name: str, purpose: str, conditions, *, analyst=None, instrument=None, day: int = 0
    ) -> dict:
        sequence = {
            "sequence_id": self._ids.next("SEQ", width=5),
            "validation_id": self._validation.validation_id,
            "method_id": self._method.method_id,
            "name": name,
            "purpose": purpose,
            "instrument_id": (instrument or self._instrument).instrument_id,
            "analyst_id": (analyst or self._analyst).analyst_id,
            "column_id": self._column.column_id,
            "cds": self._method.cds,
            "cds_project": (instrument or self._instrument).cds_project,
            "started_at": self._day(day),
            "day_index": day,
            "conditions": conditions.as_dict(),
        }
        self._out.sequences.append(sequence)
        self._audit(sequence["sequence_id"], None, "SEQUENCE_CREATED", day,
                    detail=f"{purpose}: {name}")
        return sequence

    def _audit(
        self,
        sequence_id: str,
        injection_id: str | None,
        code: str,
        day: int,
        *,
        detail: str = "",
        reason: str = "",
        user: str | None = None,
    ) -> None:
        self._out.audit.append(
            {
                "audit_id": self._ids.next("AUD", width=7),
                "validation_id": self._validation.validation_id,
                "sequence_id": sequence_id,
                "injection_id": injection_id,
                "event_code": code,
                "occurred_at": self._day(day),
                "user_id": user or self._analyst.analyst_id,
                "detail": detail,
                "reason": reason,
            }
        )

    def _inject(
        self,
        sequence: dict,
        preparation: Preparation,
        conditions,
        *,
        analyst=None,
        instrument=None,
        day: int = 0,
    ) -> InjectionResult:
        instrument = instrument or self._instrument
        analyst = analyst or self._analyst
        number = 1 + sum(
            1 for row in self._out.injections if row["sequence_id"] == sequence["sequence_id"]
        )
        self._column = self._column.after(1)
        days_since = (
            self._validation.started + timedelta(days=day) - instrument.calibration.last_calibrated
        ).days

        request = InjectionRequest(
            injection_id=self._ids.next("INJ", width=6),
            sequence_id=sequence["sequence_id"],
            injection_number=number,
            preparation=preparation,
            conditions=conditions,
            instrument=instrument,
            analyst=analyst,
            column=self._column,
            day_index=day,
            days_since_calibration=float(max(days_since, 0)),
        )
        result = self._model.inject(request, keep_trace=self._keep_traces)

        self._out.injections.append(
            {
                "injection_id": result.injection_id,
                "sequence_id": result.sequence_id,
                "validation_id": self._validation.validation_id,
                "injection_number": number,
                "method_id": result.method_id,
                "preparation_id": result.preparation_id,
                "sample_id": result.sample_id,
                "purpose": result.purpose,
                "instrument_id": result.instrument_id,
                "analyst_id": result.analyst_id,
                "column_id": result.column_id,
                "column_injections": self._column.injections,
                "injected_at": self._day(day),
                "run_time_min": self._method.run_time_min,
                "peaks_found": len(result.peaks),
                **{f"condition_{k}": v for k, v in result.conditions.as_dict().items()},
            }
        )
        for peak in result.peaks:
            self._out.peaks.append(
                {
                    "peak_id": self._ids.next("PK", width=8),
                    "injection_id": result.injection_id,
                    "sequence_id": result.sequence_id,
                    "peak_index": peak.index,
                    "analyte_id": peak.analyte_id,
                    "peak_name": self._peak_name(peak.analyte_id),
                    "retention_time_min": round(peak.retention_time_min, 5),
                    "area": round(peak.area, 4),
                    "height": round(peak.height, 4),
                    "width_half_min": round(peak.width_half_min, 5),
                    "start_min": round(peak.start_min, 5),
                    "end_min": round(peak.end_min, 5),
                    "tailing_usp": None if peak.tailing_usp is None else round(peak.tailing_usp, 4),
                    "plate_count_usp": (
                        None if peak.plate_count_usp is None else round(peak.plate_count_usp, 1)
                    ),
                    "resolution_previous": (
                        None
                        if peak.resolution_previous is None
                        else round(peak.resolution_previous, 4)
                    ),
                    "signal_to_noise": (
                        None if peak.signal_to_noise is None else round(peak.signal_to_noise, 2)
                    ),
                }
            )
        self._audit(sequence["sequence_id"], result.injection_id, "INJECTION_ACQUIRED", day)
        self._processing_events(sequence, result, day)
        if self._keep_traces:
            self._out.traces.append((result.injection_id, result.times_min, result.response))
        return result

    def _processing_events(self, sequence: dict, result: InjectionResult, day: int) -> None:
        """The audit events an inspector actually looks for.

        Manual integration, reprocessing and aborted runs are the entries a
        data-integrity review is built around, and a trail without them reads as
        synthetic immediately. Rates come from ``cds.yaml`` rather than being
        chosen here.
        """
        audit = self._config.cds.audit_trail
        stream = self._rngs.child(
            "lab", "audit", self._method.method_id, result.injection_id
        )

        if stream.random() < audit.aborted_injection_rate:
            self._audit(
                sequence["sequence_id"], result.injection_id, "INJECTION_ABORTED", day,
                detail="Run terminated before completion",
                reason="Pump pressure excursion; injection repeated",
            )
            return

        self._audit(sequence["sequence_id"], result.injection_id, "RESULT_PROCESSED", day)

        if stream.random() < audit.manual_integration_rate:
            # Which peak was touched matters: manual integration of the main peak
            # is what an assessor asks about, so it is recorded by name.
            touched = max(result.peaks, key=lambda peak: peak.area, default=None)
            self._audit(
                sequence["sequence_id"], result.injection_id, "MANUAL_INTEGRATION", day,
                detail=(
                    f"Baseline adjusted for {self._peak_name(touched.analyte_id)}"
                    if touched
                    else "Baseline adjusted"
                ),
                reason="Integration did not track the baseline across the peak",
            )
        if stream.random() < audit.reprocessing_rate:
            self._audit(
                sequence["sequence_id"], result.injection_id, "RESULT_REPROCESSED", day,
                detail="Reprocessed with the approved processing method",
                reason="Original processing used a superseded processing method",
            )

    def _peak_name(self, analyte_id: str | None) -> str:
        if analyte_id is None:
            return "Unknown"
        analyte = self._method.analyte(analyte_id)
        return analyte.peak_name if analyte else analyte_id

    def _standard_prep(self, tag: str, level: float = 1.0, **kwargs) -> Preparation:
        return Preparation(
            preparation_id=f"PREP-{tag}",
            sample_id=f"STD-{tag}",
            concentrations={self._assay_id: self._standard_concentration * level},
            purpose="STANDARD",
            **kwargs,
        )

    def _sample_prep(self, tag: str, level: float = 1.0, **kwargs) -> Preparation:
        """A drug-product sample: the analyte plus its impurities at real levels."""
        concentrations = {self._assay_id: self._standard_concentration * level}
        for analyte in self._method.analytes:
            if analyte.analyte_id == self._assay_id:
                continue
            # Impurities present at a fraction of their specification limit, as a
            # release-quality batch would be.
            spec = analyte.specification_percent or 0.10
            share = spec * 0.50 / 100.0
            concentrations[analyte.analyte_id] = self._standard_concentration * level * share
        return Preparation(
            preparation_id=f"PREP-{tag}",
            sample_id=f"SMP-{tag}",
            concentrations=concentrations,
            purpose="SAMPLE",
            **kwargs,
        )

    def _record(
        self, experiment: str, metric: str, measured: float | None, criterion: Criterion | None,
        *, detail: str = "",
    ) -> None:
        if criterion is None:
            verdict, text = "INFORMATIONAL", ""
        else:
            verdict = "PASS" if criterion.passes(measured) else "FAIL"
            limit = criterion.limit
            text = (
                f"{criterion.operator} {limit}"
                if not isinstance(limit, list)
                else f"BETWEEN {limit[0]}-{limit[1]}"
            )
        self._out.results.append(
            {
                "result_id": self._ids.next("VR", width=5),
                "validation_id": self._validation.validation_id,
                "method_id": self._method.method_id,
                "experiment": experiment,
                "metric": metric,
                "measured": None if measured is None else round(measured, 6),
                "criterion": text,
                "verdict": verdict,
                "detail": detail,
            }
        )

    def _criterion(self, criteria: list[Criterion], metric: str) -> Criterion | None:
        for criterion in criteria:
            if criterion.metric == metric:
                return criterion
        return None

    # -------------------------------------------------------------- suitability
    def _run_suitability(self, sequence: dict, conditions, day: int) -> tuple[bool, dict]:
        """Run and evaluate a system suitability set, retrying a failed one.

        A failed first attempt is re-run and both attempts stay in the record,
        which is what an audit trail of a real sequence looks like.
        """
        spec = self._config.cds.system_suitability.get(self._method.method_id)
        if spec is None:
            return True, {}

        mixture = self._config.cds.resolution_solution.get(self._method.method_id, {})
        concentrations = {
            analyte_id: self._standard_concentration * fraction
            for analyte_id, fraction in mixture.items()
        } or {self._assay_id: self._standard_concentration}

        forced = self._rngs.child("lab", "suitability", sequence["sequence_id"])
        attempts = max(1, self._config.cds.max_suitability_attempts)
        measured: dict = {}
        passed = False

        for attempt in range(1, attempts + 1):
            preparation = Preparation(
                preparation_id=f"PREP-SST-{sequence['sequence_id']}-{attempt}",
                sample_id=f"RS-{sequence['sequence_id']}-{attempt}",
                concentrations=concentrations,
                purpose="RESOLUTION_SOLUTION",
            )
            results = [
                self._inject(sequence, preparation, conditions, day=day)
                for _ in range(spec.replicate_injections)
            ]
            measured = self._measure_suitability(results, spec.criteria)

            # A cause the physical model does not otherwise produce: a bubble, a
            # bad vial, a column that had not finished equilibrating.
            spurious = (
                attempt == 1
                and forced.random() < self._config.cds.suitability_first_attempt_failure_rate
            )
            failures = [
                criterion.metric
                for criterion in spec.criteria
                if not criterion.passes(measured.get(criterion.metric))
            ]
            if spurious and not failures:
                failures = ["AREA_RSD_PERCENT"]
                measured["AREA_RSD_PERCENT"] = float(spec.criteria[0].limit) * 1.4  # type: ignore[arg-type]

            self._out.suitability.append(
                {
                    "suitability_id": self._ids.next("SST", width=5),
                    "sequence_id": sequence["sequence_id"],
                    "validation_id": self._validation.validation_id,
                    "method_id": self._method.method_id,
                    "attempt": attempt,
                    "injections": len(results),
                    "verdict": "FAIL" if failures else "PASS",
                    "failed_metrics": ",".join(failures),
                    **{
                        f"measured_{key.lower()}": (
                            None if value is None else round(value, 5)
                        )
                        for key, value in measured.items()
                    },
                }
            )
            if failures:
                self._audit(
                    sequence["sequence_id"], None, "SUITABILITY_FAILED", day,
                    detail=", ".join(failures),
                    reason="Re-run per SOP after suitability failure"
                    if attempt < attempts
                    else "Suitability not met after maximum attempts",
                )
                continue
            passed = True
            break

        return passed, measured

    def _measure_suitability(
        self, results: list[InjectionResult], criteria: list[SuitabilityCriterion]
    ) -> dict[str, float | None]:
        """Compute each suitability metric from the injections that were run."""
        assay = self._assay_id
        areas = [r.area_of(assay) for r in results]
        areas = [a for a in areas if a is not None]
        retentions = [
            p.retention_time_min for r in results if (p := r.peak_for(assay)) is not None
        ]

        def averaged(getter) -> float | None:
            values = [
                value
                for r in results
                if (peak := r.peak_for(assay)) is not None
                and (value := getter(peak)) is not None
            ]
            return stats.fmean(values) if values else None

        measured: dict[str, float | None] = {
            "AREA_RSD_PERCENT": _rsd_percent(areas),
            "RETENTION_RSD_PERCENT": _rsd_percent(retentions),
            "TAILING": averaged(lambda peak: peak.tailing_usp),
            "PLATE_COUNT": averaged(lambda peak: peak.plate_count_usp),
        }

        for criterion in criteria:
            if criterion.metric != "RESOLUTION":
                continue
            target, versus = criterion.analyte_id, criterion.versus
            values: list[float] = []
            for result in results:
                ordered = [p for p in result.peaks if p.analyte_id in {target, versus}]
                ordered.sort(key=lambda peak: peak.retention_time_min)
                if len(ordered) == 2 and ordered[1].resolution_previous is not None:
                    values.append(ordered[1].resolution_previous)
            measured["RESOLUTION"] = stats.fmean(values) if values else None
        return measured

    # --------------------------------------------------------------- experiments
    def run(self) -> ValidationOutput:
        """Execute every experiment the validation declares, in order."""
        experiments = self._validation.experiments
        if experiments.specificity:
            self._specificity()
        standard_curve = None
        if experiments.linearity:
            standard_curve = self._linearity()
        if experiments.accuracy:
            self._accuracy(standard_curve)
        if experiments.repeatability:
            repeatability = self._repeatability()
        else:
            repeatability = []
        if experiments.intermediate_precision:
            self._intermediate_precision(repeatability)
        if experiments.detection_limits:
            self._detection_limits()
        if experiments.robustness:
            self._robustness()
        if experiments.solution_stability:
            self._solution_stability()
        if experiments.filter_validation:
            self._filter_validation()
        self._close_sequences()
        return self._out

    def _close_sequences(self) -> None:
        """Second-person review and approval of every result set.

        A result set nobody signed is not a record. Review is by someone other
        than the analyst who ran it wherever the lab has another qualified
        analyst, because self-review is the finding an auditor writes up.
        """
        analysts = self._config.instruments.analysts
        for sequence in self._out.sequences:
            reviewer = next(
                (
                    analyst.analyst_id
                    for analyst in analysts
                    if analyst.analyst_id != sequence["analyst_id"]
                    and analyst.grade == "SENIOR"
                ),
                None,
            )
            day = sequence["day_index"]
            self._audit(
                sequence["sequence_id"], None, "RESULT_SET_REVIEWED", day,
                detail=f"Reviewed {sequence['name']}",
                user=reviewer or sequence["analyst_id"],
            )
            self._audit(
                sequence["sequence_id"], None, "RESULT_SET_APPROVED", day,
                detail=f"Approved {sequence['name']}",
                user=reviewer or sequence["analyst_id"],
            )

    def _assay_percent(self, sample: InjectionResult, standard_area: float) -> float | None:
        """External-standard assay, as the method's calculation would state it."""
        area = sample.area_of(self._assay_id)
        if area is None or standard_area <= 0.0:
            return None
        return 100.0 * area / standard_area

    def _specificity(self) -> None:
        experiment = self._validation.experiments.specificity
        assert experiment is not None
        conditions = self._method.nominal_conditions
        sequence = self._open_sequence("Specificity", "SPECIFICITY", conditions)
        self._run_suitability(sequence, conditions, 0)

        blank = Preparation("PREP-BLANK", "BLANK", {}, purpose="BLANK")
        placebo = Preparation("PREP-PLACEBO", "PLACEBO", {}, purpose="PLACEBO")
        standard = self._standard_prep("SPEC-STD")

        blank_results = [
            self._inject(sequence, blank, conditions) for _ in range(experiment.replicates)
        ]
        for _ in range(experiment.replicates):
            self._inject(sequence, placebo, conditions)
        standard_results = [
            self._inject(sequence, standard, conditions) for _ in range(experiment.replicates)
        ]
        standard_area = stats.fmean(
            [a for r in standard_results if (a := r.area_of(self._assay_id)) is not None] or [0.0]
        )

        # Interference: anything the integrator found in a blank at the analyte's
        # retention, as a percentage of the standard response.
        interference = 0.0
        for result in blank_results:
            peak = result.peak_for(self._assay_id)
            if peak is not None and standard_area > 0.0:
                interference = max(interference, 100.0 * peak.area / standard_area)
        self._record(
            "specificity", "BLANK_INTERFERENCE_PERCENT", interference,
            self._criterion(experiment.criteria, "BLANK_INTERFERENCE_PERCENT"),
        )

        # Stressed samples: each stress promotes its own degradant, and mass
        # balance is assay plus total degradants against the unstressed control.
        for stress in experiment.stress_conditions:
            fraction = stress.target_degradation_percent / 100.0
            concentrations = {self._assay_id: self._standard_concentration * (1.0 - fraction)}
            promoted = self._degradants_for(stress.condition)
            for analyte_id, share in promoted.items():
                concentrations[analyte_id] = self._standard_concentration * fraction * share
            preparation = Preparation(
                f"PREP-STRESS-{stress.condition}", f"STRESS-{stress.condition}",
                concentrations, purpose=f"STRESSED_{stress.condition}",
            )
            results = [
                self._inject(sequence, preparation, conditions)
                for _ in range(experiment.replicates)
            ]
            recovered = []
            for result in results:
                assay = result.area_of(self._assay_id)
                if assay is None or standard_area <= 0.0:
                    continue
                analyte = self._method.analyte(self._assay_id)
                assert analyte is not None
                # Degradant areas are converted to analyte equivalents by their
                # response factors before being added to the balance.
                equivalent = 0.0
                for peak in result.peaks:
                    if peak.analyte_id in {None, self._assay_id}:
                        continue
                    other = self._method.analyte(peak.analyte_id)
                    if other is None:
                        continue
                    equivalent += peak.area * (analyte.response_factor / other.response_factor)
                recovered.append(100.0 * (assay + equivalent) / standard_area)
            balance = stats.fmean(recovered) if recovered else None
            self._record(
                "specificity", f"MASS_BALANCE_PERCENT[{stress.condition}]", balance,
                self._criterion(experiment.criteria, "MASS_BALANCE_PERCENT"),
                detail=stress.description,
            )

        # Peak purity would come from a PDA spectral comparison. Modelled as a
        # measured quantity driven by how much co-elution the trace actually has:
        # the critical pair's resolution in the stressed samples.
        purity = self._peak_purity(sequence)
        self._record(
            "specificity", "PEAK_PURITY", purity,
            self._criterion(experiment.criteria, "PEAK_PURITY"),
        )

    def _degradants_for(self, condition: str) -> dict[str, float]:
        """Which degradants a stress condition promotes, from substances.yaml."""
        substance = self._config.substances.by_id(self._assay_id)
        if substance is None:
            return {}
        wanted = {
            "ACID": "HYDROLYSIS_ACID",
            "BASE": "HYDROLYSIS_BASE",
            "OXIDATIVE": "OXIDATION",
            "THERMAL": "THERMAL",
            "PHOTOLYTIC": "PHOTOLYTIC",
        }.get(condition)
        declared = {a.analyte_id for a in self._method.analytes}
        matches = {
            pathway.product: pathway.relative_rate
            for pathway in substance.degradation_pathways
            if pathway.pathway == wanted and pathway.product in declared
        }
        if not matches:
            return {}
        total = sum(matches.values())
        return {product: rate / total for product, rate in matches.items()}

    def _peak_purity(self, sequence: dict) -> float:
        """Purity angle proxy: driven by measured separation, not declared."""
        resolutions = [
            row["resolution_previous"]
            for row in self._out.peaks
            if row["sequence_id"] == sequence["sequence_id"]
            and row["analyte_id"] == self._assay_id
            and row["resolution_previous"] is not None
        ]
        if not resolutions:
            return 1.0
        worst = min(resolutions)
        # Fully resolved gives essentially unity; a co-eluting pair degrades it.
        return round(min(1.0, 1.0 - 0.004 * math.exp(-1.6 * (worst - 1.0))), 6)

    def _linearity(self) -> LinearFit:
        experiment = self._validation.experiments.linearity
        assert experiment is not None
        conditions = self._method.nominal_conditions
        sequence = self._open_sequence("Linearity", "LINEARITY", conditions)
        self._run_suitability(sequence, conditions, 0)

        points: list[tuple[float, float]] = []
        for level in experiment.levels_percent:
            fraction = level / 100.0
            for replicate in range(1, experiment.replicates + 1):
                preparation = self._standard_prep(f"LIN-{level:g}-{replicate}", fraction)
                result = self._inject(sequence, preparation, conditions)
                area = result.area_of(self._assay_id)
                if area is not None:
                    points.append((self._standard_concentration * fraction, area))

        fit = linear_fit(points)
        self._record("linearity", "R_SQUARED", fit.r_squared,
                     self._criterion(experiment.criteria, "R_SQUARED"))
        self._record("linearity", "SLOPE", fit.slope, None)
        self._record("linearity", "INTERCEPT", fit.intercept, None)

        response_at_100 = fit.slope * self._standard_concentration + fit.intercept
        intercept_percent = (
            abs(100.0 * fit.intercept / response_at_100) if response_at_100 else None
        )
        self._record("linearity", "Y_INTERCEPT_PERCENT_OF_100", intercept_percent,
                     self._criterion(experiment.criteria, "Y_INTERCEPT_PERCENT_OF_100"))

        residual_rsd = (
            100.0 * fit.residual_sd / stats.fmean([y for _, y in points]) if points else None
        )
        self._record("linearity", "RESIDUAL_RSD_PERCENT", residual_rsd,
                     self._criterion(experiment.criteria, "RESIDUAL_RSD_PERCENT"))
        return fit

    def _accuracy(self, curve: LinearFit | None) -> None:
        experiment = self._validation.experiments.accuracy
        assert experiment is not None
        conditions = self._method.nominal_conditions
        sequence = self._open_sequence("Accuracy", "ACCURACY", conditions)
        self._run_suitability(sequence, conditions, 0)

        standard = self._standard_prep("ACC-STD")
        standard_areas = [
            area
            for _ in range(2)
            if (area := self._inject(sequence, standard, conditions).area_of(self._assay_id))
            is not None
        ]
        standard_area = stats.fmean(standard_areas) if standard_areas else 0.0

        recoveries: list[float] = []
        for level in experiment.levels_percent:
            fraction = level / 100.0
            per_level: list[float] = []
            for replicate in range(1, experiment.replicates + 1):
                preparation = self._sample_prep(f"ACC-{level:g}-{replicate}", fraction)
                result = self._inject(sequence, preparation, conditions)
                area = result.area_of(self._assay_id)
                if area is None:
                    continue
                if curve is not None and curve.slope:
                    found = curve.concentration_for(area)
                else:
                    found = (
                        self._standard_concentration * area / standard_area
                        if standard_area
                        else 0.0
                    )
                nominal = self._standard_concentration * fraction
                per_level.append(100.0 * found / nominal if nominal else 0.0)
            recoveries.extend(per_level)
            if per_level:
                self._record(
                    "accuracy", f"RECOVERY_PERCENT[{level:g}%]", stats.fmean(per_level),
                    self._criterion(experiment.criteria, "RECOVERY_PERCENT"),
                )
        if recoveries:
            self._record("accuracy", "RECOVERY_RSD_PERCENT", _rsd_percent(recoveries),
                         self._criterion(experiment.criteria, "RECOVERY_RSD_PERCENT"))

    def _precision_run(
        self, label: str, purpose: str, experiment, *, analyst, instrument, day: int
    ) -> list[float]:
        conditions = self._method.nominal_conditions
        sequence = self._open_sequence(
            label, purpose, conditions, analyst=analyst, instrument=instrument, day=day
        )
        self._run_suitability(sequence, conditions, day)

        standard = self._standard_prep(f"{label}-STD")
        standard_area = stats.fmean(
            [
                area
                for _ in range(2)
                if (
                    area := self._inject(
                        sequence, standard, conditions,
                        analyst=analyst, instrument=instrument, day=day,
                    ).area_of(self._assay_id)
                )
                is not None
            ]
            or [0.0]
        )

        assays: list[float] = []
        fraction = experiment.level_percent / 100.0
        for replicate in range(1, experiment.replicates + 1):
            # Six SEPARATE preparations, not six injections of one. This is the
            # difference between measuring the method and measuring the injector.
            preparation = self._sample_prep(f"{label}-{replicate}", fraction)
            result = self._inject(
                sequence, preparation, conditions,
                analyst=analyst, instrument=instrument, day=day,
            )
            assay = self._assay_percent(result, standard_area)
            if assay is not None:
                assays.append(assay)
        return assays

    def _repeatability(self) -> list[float]:
        experiment = self._validation.experiments.repeatability
        assert experiment is not None
        assays = self._precision_run(
            "Repeatability", "REPEATABILITY", experiment,
            analyst=self._analyst, instrument=self._instrument, day=0,
        )
        self._record("repeatability", "RSD_PERCENT", _rsd_percent(assays),
                     self._criterion(experiment.criteria, "RSD_PERCENT"))
        self._record("repeatability", "MEAN_ASSAY_PERCENT",
                     stats.fmean(assays) if assays else None, None)
        return assays

    def _intermediate_precision(self, repeatability: list[float]) -> None:
        experiment = self._validation.experiments.intermediate_precision
        assert experiment is not None
        analyst = (
            self._config.instruments.analyst(experiment.analyst_id)
            if experiment.analyst_id
            else self._analyst
        ) or self._analyst
        instrument = (
            self._config.instruments.instrument(experiment.instrument_id)
            if experiment.instrument_id
            else self._instrument
        ) or self._instrument

        assays = self._precision_run(
            "Intermediate precision", "INTERMEDIATE_PRECISION", experiment,
            analyst=analyst, instrument=instrument, day=experiment.day_offset,
        )
        self._record("intermediate_precision", "RSD_PERCENT", _rsd_percent(assays),
                     self._criterion(experiment.criteria, "RSD_PERCENT"))
        self._record(
            "intermediate_precision", "COMBINED_RSD_PERCENT",
            _rsd_percent(repeatability + assays),
            self._criterion(experiment.criteria, "COMBINED_RSD_PERCENT"),
            detail=f"{len(repeatability)} + {len(assays)} determinations",
        )

    def _detection_limits(self) -> None:
        experiment = self._validation.experiments.detection_limits
        assert experiment is not None
        conditions = self._method.nominal_conditions
        sequence = self._open_sequence("Detection limits", "DETECTION_LIMITS", conditions)
        self._run_suitability(sequence, conditions, 0)

        # Expressed against the impurity that sets the tightest specification.
        target = min(
            (a for a in self._method.analytes if a.specification_percent is not None),
            key=lambda a: a.specification_percent or 1e9,
            default=None,
        )
        if target is None:
            return

        points: list[tuple[float, float]] = []
        per_level: list[tuple[float, float, float | None, float | None]] = []
        for level in experiment.levels_percent:
            concentration = self._standard_concentration * level / 100.0
            areas: list[float] = []
            ratios: list[float] = []
            for replicate in range(1, experiment.replicates + 1):
                # A dilute sensitivity solution: the impurity on its own. With
                # the analyte at full strength this peak would be a rider on a
                # flank three orders of magnitude taller, and no integrator --
                # real or otherwise -- could quantify it.
                preparation = Preparation(
                    f"PREP-DL-{level:g}-{replicate}", f"DL-{level:g}-{replicate}",
                    {target.analyte_id: concentration},
                    purpose="SENSITIVITY_SOLUTION",
                )
                result = self._inject(sequence, preparation, conditions)
                peak = result.peak_for(target.analyte_id)
                if peak is None:
                    continue
                points.append((concentration, peak.area))
                areas.append(peak.area)
                if peak.signal_to_noise is not None:
                    ratios.append(peak.signal_to_noise)
            per_level.append(
                (
                    level,
                    concentration,
                    stats.fmean(ratios) if ratios else None,
                    _rsd_percent(areas),
                )
            )

        fit = linear_fit(points)
        lod = 3.3 * fit.residual_sd / fit.slope if fit.slope else None
        loq_regression = 10.0 * fit.residual_sd / fit.slope if fit.slope else None
        self._record("detection_limits", "LOD_UG_ML", lod, None,
                     detail=f"3.3 * {fit.residual_sd:.4f} / {fit.slope:.4f}")
        self._record("detection_limits", "LOQ_REGRESSION_UG_ML", loq_regression, None,
                     detail=f"10 * {fit.residual_sd:.4f} / {fit.slope:.4f}")

        # The two accepted definitions of the quantitation limit do not have to
        # agree, and here they do not: the regression estimate is the more
        # optimistic. Interpolating the concentration at which signal-to-noise
        # reaches 10 gives the other, and the reported LOQ is the more
        # conservative of the two -- which is the choice an analyst defends in
        # front of an assessor.
        loq_sn = self._concentration_at_signal_to_noise(per_level, 10.0)
        self._record("detection_limits", "LOQ_SIGNAL_TO_NOISE_UG_ML", loq_sn, None,
                     detail="interpolated to S/N = 10")

        candidates = [value for value in (loq_regression, loq_sn) if value is not None]
        loq = max(candidates) if candidates else None
        self._record("detection_limits", "LOQ_UG_ML", loq, None,
                     detail="the more conservative of the two estimates")
        if loq is not None:
            self._record(
                "detection_limits", "LOQ_PERCENT_OF_STANDARD",
                100.0 * loq / self._standard_concentration, None,
            )

        # Performance is confirmed AT the limit of quantitation, which is what
        # an ICH Q2 report states: the LOQ is established from the regression,
        # then precision and signal-to-noise are demonstrated at that level. The
        # lowest level prepared may well sit below the LOQ -- judging the method
        # there would be reporting a failure to do something it never claimed.
        at_or_above = [row for row in per_level if loq is not None and row[1] >= loq]
        if at_or_above:
            level, _, ratio, rsd = min(at_or_above, key=lambda row: row[1])
        elif loq is not None:
            level, _, ratio, rsd = min(per_level, key=lambda row: abs(row[1] - loq))
        else:
            level, _, ratio, rsd = per_level[0]
        detail = f"confirmed at the {level:g}% level, nearest the established LOQ"
        self._record("detection_limits", "LOQ_SIGNAL_TO_NOISE", ratio,
                     self._criterion(experiment.criteria, "LOQ_SIGNAL_TO_NOISE"),
                     detail=detail)
        self._record("detection_limits", "LOQ_RSD_PERCENT", rsd,
                     self._criterion(experiment.criteria, "LOQ_RSD_PERCENT"),
                     detail=detail)

    @staticmethod
    def _concentration_at_signal_to_noise(
        per_level: list[tuple[float, float, float | None, float | None]], target: float
    ) -> float | None:
        """Concentration at which signal-to-noise reaches ``target``.

        Linear interpolation between the two levels that bracket it. Returns None
        if no level reached the target, since extrapolating a detection limit
        beyond the data would be inventing it.
        """
        usable = sorted(
            ((concentration, ratio) for _, concentration, ratio, _ in per_level
             if ratio is not None),
            key=lambda row: row[0],
        )
        for index in range(1, len(usable)):
            (low_c, low_sn), (high_c, high_sn) = usable[index - 1], usable[index]
            if low_sn < target <= high_sn and high_sn != low_sn:
                span = (target - low_sn) / (high_sn - low_sn)
                return low_c + span * (high_c - low_c)
        if usable and usable[0][1] >= target:
            return usable[0][0]
        return None

    def _robustness(self) -> None:
        experiment = self._validation.experiments.robustness
        assert experiment is not None
        nominal = self._method.nominal_conditions

        reference = self._open_sequence("Robustness reference", "ROBUSTNESS", nominal)
        self._run_suitability(reference, nominal, 0)
        standard = self._standard_prep("ROB-REF-STD")
        reference_standard_area = stats.fmean(
            [
                area
                for _ in range(2)
                if (
                    area := self._inject(reference, standard, nominal).area_of(self._assay_id)
                )
                is not None
            ]
            or [0.0]
        )
        reference_assays = [
            assay
            for replicate in range(1, experiment.replicates + 1)
            if (
                assay := self._assay_percent(
                    self._inject(
                        reference, self._sample_prep(f"ROB-REF-{replicate}"), nominal
                    ),
                    reference_standard_area,
                )
            )
            is not None
        ]
        reference_assay = stats.fmean(reference_assays) if reference_assays else None

        for condition in experiment.conditions:
            varied = nominal.varied(condition.factor, condition.delta)
            label = f"{condition.factor} {condition.delta:+g}"
            sequence = self._open_sequence(f"Robustness {label}", "ROBUSTNESS", varied)
            passed, _ = self._run_suitability(sequence, varied, 0)

            standard = self._standard_prep(f"ROB-{label}-STD")
            standard_area = stats.fmean(
                [
                    area
                    for _ in range(2)
                    if (
                        area := self._inject(sequence, standard, varied).area_of(self._assay_id)
                    )
                    is not None
                ]
                or [0.0]
            )
            assays = [
                assay
                for replicate in range(1, experiment.replicates + 1)
                if (
                    assay := self._assay_percent(
                        self._inject(
                            sequence, self._sample_prep(f"ROB-{label}-{replicate}"), varied
                        ),
                        standard_area,
                    )
                )
                is not None
            ]
            self._record(
                "robustness", f"SUITABILITY_PASSES[{label}]", 1.0 if passed else 0.0,
                self._criterion(experiment.criteria, "SUITABILITY_PASSES"),
                detail=label,
            )
            if assays and reference_assay:
                difference = abs(stats.fmean(assays) - reference_assay)
                self._record(
                    "robustness", f"ASSAY_DIFFERENCE_PERCENT[{label}]", difference,
                    self._criterion(experiment.criteria, "ASSAY_DIFFERENCE_PERCENT"),
                    detail=label,
                )

    def _solution_stability(self) -> None:
        experiment = self._validation.experiments.solution_stability
        assert experiment is not None
        conditions = self._method.nominal_conditions
        sequence = self._open_sequence("Solution stability", "SOLUTION_STABILITY", conditions)
        self._run_suitability(sequence, conditions, 0)

        for solution in experiment.solutions:
            initial: float | None = None
            for hours in experiment.timepoints_h:
                areas: list[float] = []
                for replicate in range(1, experiment.replicates + 1):
                    tag = f"SS-{solution}-{hours:g}-{replicate}"
                    preparation = (
                        self._standard_prep(tag, standing_hours=hours)
                        if solution == "STANDARD"
                        else self._sample_prep(tag, standing_hours=hours)
                    )
                    result = self._inject(sequence, preparation, conditions)
                    area = result.area_of(self._assay_id)
                    if area is not None:
                        areas.append(area)
                if not areas:
                    continue
                mean = stats.fmean(areas)
                if hours == experiment.timepoints_h[0]:
                    initial = mean
                    continue
                change = 100.0 * abs(mean - (initial or mean)) / (initial or mean)
                self._record(
                    "solution_stability", f"CHANGE_FROM_INITIAL_PERCENT[{solution} {hours:g}h]",
                    change, self._criterion(experiment.criteria, "CHANGE_FROM_INITIAL_PERCENT"),
                )

    def _filter_validation(self) -> None:
        experiment = self._validation.experiments.filter_validation
        assert experiment is not None
        conditions = self._method.nominal_conditions
        sequence = self._open_sequence("Filter validation", "FILTER_VALIDATION", conditions)
        self._run_suitability(sequence, conditions, 0)

        unfiltered = [
            area
            for replicate in range(1, experiment.replicates + 1)
            if (
                area := self._inject(
                    sequence, self._sample_prep(f"FV-UNFILT-{replicate}"), conditions
                ).area_of(self._assay_id)
            )
            is not None
        ]
        baseline = stats.fmean(unfiltered) if unfiltered else 0.0

        for filter_name in experiment.filters:
            areas = [
                area
                for replicate in range(1, experiment.replicates + 1)
                if (
                    area := self._inject(
                        sequence,
                        self._sample_prep(f"FV-{filter_name}-{replicate}", filter=filter_name),
                        conditions,
                    ).area_of(self._assay_id)
                )
                is not None
            ]
            if not areas or not baseline:
                continue
            difference = 100.0 * abs(stats.fmean(areas) - baseline) / baseline
            self._record(
                "filter_validation", f"FILTERED_VS_UNFILTERED_PERCENT[{filter_name}]",
                difference, self._criterion(experiment.criteria, "FILTERED_VS_UNFILTERED_PERCENT"),
            )
