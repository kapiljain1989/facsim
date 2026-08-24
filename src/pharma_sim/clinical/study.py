"""Running study NVR-101-201 and emitting it in SDTM shape.

Produces the three oncology SDTM domains and the ADaM time-to-event dataset:

===========  ==================================================================
``TU``       Tumour Identification — which lesions each evaluator is following
``TR``       Tumour Results — every measurement of every lesion at every visit
``RS``       Disease Response — the derived response, per evaluator per visit
``ADTTE``    Time-to-event analysis dataset — PFS with censoring reasons
===========  ==================================================================

The variable names are the real ones. ``TREVAL`` distinguishes the investigator's
read from the independent assessor's, which is how a single dataset carries two
opinions about the same scans; ``RSEVAL`` does the same for the response. That
pairing is the whole reason a lesion-level model was worth building.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from random import Random

from pharma_sim.clinical.config import ClinicalConfig
from pharma_sim.clinical.lesion import (
    ReaderSelection,
    SubjectTumour,
    assessment_weeks,
    build_tumour,
    measure,
    rules_from_config,
    select_targets,
)
from pharma_sim.clinical.recist import Assessment, Timepoint, best_overall_response, evaluate_course
from pharma_sim.clinical.survival import PfsOutcome, derive_pfs, median_survival
from pharma_sim.engine.rng import RngRegistry

__all__ = ["StudyOutput", "run_study"]

#: SDTM TRTESTCD depends on what is being measured: a node is reported by its
#: short axis, everything else by its longest diameter.
_LONGEST_DIAMETER = ("LDIAM", "Longest Diameter")
_SHORT_AXIS = ("SAXIS", "Short Axis")

_WEEKS_PER_MONTH = 52.1775 / 12.0


@dataclass
class StudyOutput:
    """Every record the study produced."""

    study_id: str
    subjects: list[dict] = field(default_factory=list)
    tu: list[dict] = field(default_factory=list)
    tr: list[dict] = field(default_factory=list)
    rs: list[dict] = field(default_factory=list)
    adtte: list[dict] = field(default_factory=list)
    #: Ground truth, for the evaluation store only. Never exported operationally.
    truth: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"{self.study_id}", f"  subjects {len(self.subjects)}"
                 f"  TU {len(self.tu)}  TR {len(self.tr)}  RS {len(self.rs)}"
                 f"  ADTTE {len(self.adtte)}"]
        arms = sorted({row["ARM"] for row in self.subjects})
        evaluators = sorted({row["EVAL"] for row in self.adtte})
        header = f"  {'arm':<8}{'n':>4}" + "".join(
            f"{'ORR ' + e:>12}{'mPFS ' + e:>13}" for e in evaluators
        )
        lines.append(header)
        for arm in arms:
            in_arm = [row["USUBJID"] for row in self.subjects if row["ARM"] == arm]
            cells = ""
            for evaluator in evaluators:
                best = [
                    row for row in self.rs
                    if row["RSTESTCD"] == "BESTRESP"
                    # RSEVAL carries the role ("INVESTIGATOR"); RSEVALID carries
                    # the reader id, which is what ADTTE.EVAL matches on.
                    and row["RSEVALID"] == evaluator
                    and row["USUBJID"] in in_arm
                ]
                responders = sum(1 for row in best if row["RSSTRESC"] in {"CR", "PR"})
                orr = 100.0 * responders / len(best) if best else 0.0
                outcomes = [
                    PfsOutcome(
                        evaluator=evaluator,
                        event=row["CNSR"] == 0,
                        week=row["AVAL"] / 7.0,
                        reason=row["EVNTDESC"],  # type: ignore[arg-type]
                        last_adequate_week=None,
                    )
                    for row in self.adtte
                    if row["EVAL"] == evaluator
                    and row["PARAMCD"] == "PFS"
                    and row["USUBJID"] in in_arm
                ]
                median = median_survival(outcomes)
                shown = "not reached" if median is None else f"{median / _WEEKS_PER_MONTH:.1f} mo"
                cells += f"{orr:>11.1f}%{shown:>13}"
            lines.append(f"  {arm:<8}{len(in_arm):>4}{cells}")
        return "\n".join(lines)


def _visit_label(index: int) -> tuple[int, str]:
    """SDTM VISITNUM / VISIT for a tumour assessment.

    Baseline is visit 1; assessments are numbered from 2. Tumour assessments have
    their own numbering because they run on the calendar rather than on the
    cycle schedule.
    """
    if index == 0:
        return 1, "BASELINE"
    return index + 1, f"TUMOUR ASSESSMENT {index}"


def _iso(day: date) -> str:
    return day.isoformat()


def run_study(config: ClinicalConfig, seed: int = 42) -> StudyOutput:
    """Run the whole study and return it in SDTM/ADaM shape."""
    protocol = config.protocol
    tumour_config = config.tumour
    rules = rules_from_config(tumour_config)
    rngs = RngRegistry(seed)
    out = StudyOutput(study_id=protocol.study_id)

    first_in = protocol.enrolment.first_subject_in
    cutoff_week = protocol.analysis.cutoff_weeks_from_fsi
    horizon = protocol.analysis.horizon_weeks

    sequence = 0
    for arm in protocol.arms:
        for index in range(1, arm.subjects + 1):
            sequence += 1
            subject_id = f"{protocol.study_id}-{sequence:04d}"
            subject_rng = rngs.child("clin", "subject", subject_id)

            # Accrual is spread across the enrolment period, so the last subject
            # randomised has far less follow-up than the first. That is where
            # administrative censoring comes from.
            offset_weeks = subject_rng.random() * protocol.enrolment.accrual_weeks
            randomised = first_in + timedelta(weeks=offset_weeks)
            # Follow-up available to this subject before the data cut.
            available = max(cutoff_week - offset_weeks, 0.0)

            tumour = build_tumour(
                subject_id, arm.arm_id, tumour_config,
                rngs.child("clin", "tumour", subject_id), horizon_weeks=horizon,
            )
            weeks = assessment_weeks(
                tumour_config, rngs.child("clin", "schedule", subject_id), horizon
            )

            out.subjects.append(
                {
                    "STUDYID": protocol.study_id,
                    "USUBJID": subject_id,
                    "ARM": arm.arm_id,
                    "ARMCD": arm.arm_id,
                    "TRT01P": arm.label,
                    "RANDDT": _iso(randomised),
                    "RFSTDTC": _iso(randomised),
                    "FOLLOWUP_WEEKS": round(available, 2),
                }
            )
            out.truth.append(
                {
                    "USUBJID": subject_id,
                    "ARM": arm.arm_id,
                    "measurable_lesions": len(tumour.lesions),
                    "sensitive_fraction": round(tumour.growth.sensitive_fraction, 5),
                    "shrinkage_rate_per_week": round(tumour.growth.shrinkage_rate_per_week, 6),
                    "growth_rate_per_week": round(tumour.growth.growth_rate_per_week, 6),
                    "new_lesion_week": tumour.new_lesion_week,
                    "non_target_progression_week": tumour.non_target_progression_week,
                    "death_week": tumour.death_week,
                }
            )

            death_week = tumour.death_week

            for reader in tumour_config.measurement.readers:
                selection = select_targets(
                    tumour, reader, tumour_config,
                    rngs.child("clin", "select", subject_id, reader.reader_id),
                )
                _emit_tu(out, protocol.study_id, subject_id, selection, reader.role, randomised)

                baseline = measure(
                    tumour, selection, 0.0, reader, tumour_config,
                    rngs.child("clin", "measure", subject_id, reader.reader_id, "baseline"),
                )
                timepoints: list[Timepoint] = []
                for week, missed in weeks:
                    if week > available:
                        break
                    timepoints.append(
                        measure(
                            tumour, selection, week, reader, tumour_config,
                            rngs.child(
                                "clin", "measure", subject_id, reader.reader_id, f"{week}"
                            ),
                            missed=missed,
                        )
                    )

                course = evaluate_course(timepoints, baseline, rules, reader.reader_id)
                _emit_tr(
                    out, protocol.study_id, subject_id, reader.reader_id, reader.role,
                    [baseline, *timepoints], randomised,
                )
                _emit_rs(
                    out, protocol.study_id, subject_id, reader.reader_id, reader.role,
                    course, rules, randomised,
                )

                outcome = derive_pfs(
                    course,
                    evaluator=reader.reader_id,
                    death_week=death_week,
                    analysis_week=available,
                )
                _emit_adtte(
                    out, protocol.study_id, subject_id, arm, outcome, randomised, available
                )

    return out


def _emit_tu(
    out: StudyOutput,
    study_id: str,
    subject_id: str,
    selection: ReaderSelection,
    role: str,
    randomised: date,
) -> None:
    """SDTM TU: which lesions this evaluator identified, and as what."""
    sequence = 0
    for lesion in selection.target:
        sequence += 1
        out.tu.append(
            {
                "STUDYID": study_id,
                "DOMAIN": "TU",
                "USUBJID": subject_id,
                "TUSEQ": sequence,
                "TULNKID": lesion.lesion_id,
                "TUTESTCD": "TUMIDENT",
                "TUTEST": "Tumor Identification",
                "TUORRES": "TARGET",
                "TUSTRESC": "TARGET",
                "TULOC": lesion.organ,
                "TUEVAL": role,
                "TUEVALID": selection.reader_id,
                "VISITNUM": 1,
                "VISIT": "BASELINE",
                "TUDTC": _iso(randomised),
            }
        )
    for lesion in selection.non_target:
        sequence += 1
        out.tu.append(
            {
                "STUDYID": study_id,
                "DOMAIN": "TU",
                "USUBJID": subject_id,
                "TUSEQ": sequence,
                "TULNKID": lesion.lesion_id,
                "TUTESTCD": "TUMIDENT",
                "TUTEST": "Tumor Identification",
                "TUORRES": "NON-TARGET",
                "TUSTRESC": "NON-TARGET",
                "TULOC": lesion.organ,
                "TUEVAL": role,
                "TUEVALID": selection.reader_id,
                "VISITNUM": 1,
                "VISIT": "BASELINE",
                "TUDTC": _iso(randomised),
            }
        )


def _emit_tr(
    out: StudyOutput,
    study_id: str,
    subject_id: str,
    evaluator: str,
    role: str,
    timepoints: list[Timepoint],
    randomised: date,
) -> None:
    """SDTM TR: every measurement, with the reason where there is no number."""
    sequence = 0
    for order, timepoint in enumerate(timepoints):
        visitnum, visit = _visit_label(order)
        assessed = randomised + timedelta(weeks=timepoint.week)
        if timepoint.missed:
            continue
        for lesion in timepoint.target:
            sequence += 1
            testcd, test = _SHORT_AXIS if lesion.nodal else _LONGEST_DIAMETER
            if lesion.absent:
                original, standard, numeric = "0", "0", 0.0
            elif lesion.not_evaluable:
                original, standard, numeric = "NOT EVALUABLE", "NE", None
            elif lesion.too_small_to_measure:
                # RECIST permits a default value where a lesion is present but
                # below the size anyone will commit a number to. Recording the
                # reason alongside it is what keeps the sum auditable.
                original, standard, numeric = "TOO SMALL TO MEASURE", "5", 5.0
            else:
                original = f"{lesion.diameter_mm:g}"
                standard = original
                numeric = lesion.diameter_mm
            out.tr.append(
                {
                    "STUDYID": study_id,
                    "DOMAIN": "TR",
                    "USUBJID": subject_id,
                    "TRSEQ": sequence,
                    "TRLNKID": lesion.lesion_id,
                    "TRTESTCD": testcd,
                    "TRTEST": test,
                    "TRORRES": original,
                    "TRORRESU": "mm",
                    "TRSTRESC": standard,
                    "TRSTRESN": numeric,
                    "TRSTRESU": "mm",
                    "TRLOC": lesion.organ,
                    "TREVAL": role,
                    "TREVALID": evaluator,
                    "VISITNUM": visitnum,
                    "VISIT": visit,
                    "TRDTC": _iso(assessed),
                }
            )


def _emit_rs(
    out: StudyOutput,
    study_id: str,
    subject_id: str,
    evaluator: str,
    role: str,
    course: list[Assessment],
    rules,
    randomised: date,
) -> None:
    """SDTM RS: the derived response at each visit, and the best overall."""
    sequence = 0
    for order, assessment in enumerate(course, start=1):
        sequence += 1
        visitnum, visit = _visit_label(order)
        out.rs.append(
            {
                "STUDYID": study_id,
                "DOMAIN": "RS",
                "USUBJID": subject_id,
                "RSSEQ": sequence,
                "RSTESTCD": "OVRLRESP",
                "RSTEST": "Overall Response",
                "RSCAT": "RECIST 1.1",
                "RSORRES": assessment.response,
                "RSSTRESC": assessment.response,
                "RSEVAL": role,
                "RSEVALID": evaluator,
                "VISITNUM": visitnum,
                "VISIT": visit,
                "RSDTC": _iso(randomised + timedelta(weeks=assessment.week)),
                # Not SDTM variables, carried so the derivation can be checked
                # without recomputing it: the sum the response came from and the
                # nadir it was compared against.
                "SUMDIAM": round(assessment.target.sum_of_diameters_mm, 2),
                "NADIR": round(assessment.target.nadir_mm, 2),
                "PCHGBASE": (
                    None if assessment.target.change_from_baseline is None
                    else round(100.0 * assessment.target.change_from_baseline, 2)
                ),
                "PCHGNADIR": (
                    None if assessment.target.change_from_nadir is None
                    else round(100.0 * assessment.target.change_from_nadir, 2)
                ),
                "NEWLESION": "Y" if assessment.new_lesion else "N",
                "NONTRESP": assessment.non_target,
            }
        )

    best, week = best_overall_response(course, rules)
    sequence += 1
    out.rs.append(
        {
            "STUDYID": study_id,
            "DOMAIN": "RS",
            "USUBJID": subject_id,
            "RSSEQ": sequence,
            "RSTESTCD": "BESTRESP",
            "RSTEST": "Best Overall Response",
            "RSCAT": "RECIST 1.1",
            "RSORRES": best,
            "RSSTRESC": best,
            "RSEVAL": role,
            "RSEVALID": evaluator,
            "VISITNUM": 999,
            "VISIT": "END OF STUDY",
            "RSDTC": (
                _iso(randomised + timedelta(weeks=week)) if week is not None else ""
            ),
            "SUMDIAM": None,
            "NADIR": None,
            "PCHGBASE": None,
            "PCHGNADIR": None,
            "NEWLESION": "",
            "NONTRESP": "",
        }
    )


def _emit_adtte(
    out: StudyOutput,
    study_id: str,
    subject_id: str,
    arm,
    outcome: PfsOutcome,
    randomised: date,
    available_weeks: float,
) -> None:
    """ADaM ADTTE: one PFS record per evaluator.

    ``CNSR`` follows the ADaM convention — 0 for an event, 1 for a censored
    observation — which is the opposite way round to how the outcome object
    stores it, so it is flipped here rather than anywhere it could be missed.
    """
    days = outcome.week * 7.0
    out.adtte.append(
        {
            "STUDYID": study_id,
            "USUBJID": subject_id,
            "PARAMCD": "PFS",
            "PARAM": "Progression-Free Survival (weeks)",
            "PARCAT1": "RECIST 1.1",
            "AVAL": round(days, 1),
            "AVALU": "DAYS",
            "CNSR": 0 if outcome.event else 1,
            "EVNTDESC": outcome.reason,
            "SRCDOM": "RS" if outcome.reason == "PROGRESSION" else "DS",
            "STARTDT": _iso(randomised),
            "ADT": _iso(randomised + timedelta(days=days)),
            "TRTP": arm.label,
            "ARM": arm.arm_id,
            "EVAL": outcome.evaluator,
            "FOLLOWUP_WEEKS": round(available_weeks, 2),
        }
    )
