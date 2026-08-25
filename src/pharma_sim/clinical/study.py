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
from pharma_sim.clinical.ctms import SiteActivation, activate_sites, allocate_enrolment
from pharma_sim.clinical.oversight import OversightOutput, generate_oversight
from pharma_sim.clinical.safety import (
    AdverseEvent,
    Exposure,
    apply_dose_modifications,
    generate_events,
)
from pharma_sim.clinical.edc import (
    EdcOutput,
    SubjectContext,
    VisitRecord,
    generate_edc,
)
from pharma_sim.engine.ids import IdFactory
from pharma_sim.lifecycle.config import LifecycleConfig
from pharma_sim.lifecycle.spine import Spine, build_spine, resupply
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
    # CTMS
    sites: list[dict] = field(default_factory=list)
    site_milestones: list[dict] = field(default_factory=list)
    # EDC
    forms: list[dict] = field(default_factory=list)
    item_data: list[dict] = field(default_factory=list)
    queries: list[dict] = field(default_factory=list)
    query_events: list[dict] = field(default_factory=list)
    item_audit: list[dict] = field(default_factory=list)
    # Oversight
    monitoring_visits: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    action_items: list[dict] = field(default_factory=list)
    sdv: list[dict] = field(default_factory=list)
    deviations: list[dict] = field(default_factory=list)
    tmf_documents: list[dict] = field(default_factory=list)
    reconciliations: list[dict] = field(default_factory=list)
    lock_events: list[dict] = field(default_factory=list)
    # Lifecycle spine — investigational product from batch to dose
    imp_lots: list[dict] = field(default_factory=list)
    imp_shipments: list[dict] = field(default_factory=list)
    imp_kits: list[dict] = field(default_factory=list)
    imp_accountability: list[dict] = field(default_factory=list)
    dosing: list[dict] = field(default_factory=list)
    ex: list[dict] = field(default_factory=list)
    # Safety
    ae: list[dict] = field(default_factory=list)
    dose_modifications: list[dict] = field(default_factory=list)
    exposure: list[dict] = field(default_factory=list)
    #: True when every kit traces to a batch a real manufacturing run produced.
    spine_linked: bool = False
    #: Ground truth, for the evaluation store only. Never exported operationally.
    truth: list[dict] = field(default_factory=list)

    @property
    def tmf_completeness(self) -> float:
        if not self.tmf_documents:
            return 0.0
        filed = sum(1 for row in self.tmf_documents if row["status"] == "FILED")
        return 100.0 * filed / len(self.tmf_documents)

    @property
    def tmf_timeliness(self) -> float:
        filed = [row for row in self.tmf_documents if row["status"] == "FILED"]
        if not filed:
            return 0.0
        return 100.0 * sum(1 for row in filed if not row["late"]) / len(filed)

    def summary(self) -> str:
        lines = [
            f"{self.study_id}",
            f"  sites {len(self.sites)}  subjects {len(self.subjects)}"
            f"  TU {len(self.tu)}  TR {len(self.tr)}  RS {len(self.rs)}"
            f"  ADTTE {len(self.adtte)}",
            f"  forms {len(self.forms)}  items {len(self.item_data)}"
            f"  queries {len(self.queries)}  query events {len(self.query_events)}"
            f"  item audit {len(self.item_audit)}",
            f"  monitoring visits {len(self.monitoring_visits)}"
            f"  findings {len(self.findings)}  SDV {len(self.sdv)}"
            f"  deviations {len(self.deviations)}",
            f"  TMF documents {len(self.tmf_documents)}"
            f" — {self.tmf_completeness:.1f}% complete,"
            f" {self.tmf_timeliness:.1f}% filed on time",
            f"  AE {len(self.ae)}  dose modifications {len(self.dose_modifications)}"
            f"  serious {sum(1 for r in self.ae if r['AESER'] == 'Y')}"
            f"  SUSAR {sum(1 for r in self.ae if r['SUSAR'] == 'Y')}",
            f"  IMP lots {len(self.imp_lots)}  shipments {len(self.imp_shipments)}"
            f"  kits {len(self.imp_kits)}  dosing {len(self.dosing)}  EX {len(self.ex)}"
            f"  — batches {'from manufacturing' if self.spine_linked else 'STUBBED'}",
        ]
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


def _permuted_blocks(arms, rng) -> list[str]:
    """Permuted block randomisation honouring the allocation ratio.

    A block is one repetition of the ratio, shuffled. It keeps the arms balanced
    throughout accrual rather than only at the end, which matters because accrual
    runs for over a year and an interim look must not find the arms lopsided.
    """
    block: list[str] = []
    for arm in arms:
        block.extend([arm.arm_id] * arm.allocation)
    target = {arm.arm_id: arm.subjects for arm in arms}
    assigned: dict[str, int] = {arm.arm_id: 0 for arm in arms}
    sequence: list[str] = []
    total = sum(target.values())
    while len(sequence) < total:
        draw = block[:]
        rng.shuffle(draw)
        for arm_id in draw:
            if len(sequence) >= total:
                break
            if assigned[arm_id] < target[arm_id]:
                assigned[arm_id] += 1
                sequence.append(arm_id)
    return sequence


def run_study(
    config: ClinicalConfig,
    lifecycle: LifecycleConfig,
    seed: int = 42,
    *,
    manufacturing_export: str | None = None,
    shelf_life_months: float | None = None,
) -> StudyOutput:
    """Run the whole study and return it in SDTM, ADaM and operational shape.

    Args:
        lifecycle: the spine configuration. Required, not optional. The case
            report form has a required kit-number item on every dosing cycle, so
            a study run without drug supply fails its own edit checks on every
            cycle -- which inflated the query rate fivefold and buried the one
            site that should have triggered a for-cause monitoring visit. There
            is one path, and it has drug in it.
        manufacturing_export: a plant export directory. Given one, kits trace to
            batches the plant actually released; without one the spine
            materialises clearly-labelled stubs.
    """
    protocol = config.protocol
    tumour_config = config.tumour
    rules = rules_from_config(tumour_config)
    rngs = RngRegistry(seed)
    ids = IdFactory()
    out = StudyOutput(study_id=protocol.study_id)

    cutoff_week = protocol.analysis.cutoff_weeks_from_fsi
    horizon = protocol.analysis.horizon_weeks
    cycle_weeks = protocol.cycle_length_days / 7.0

    # ------------------------------------------------------------------ CTMS
    activations = activate_sites(config, lambda site_id: rngs.child("clin", "site", site_id))
    by_site = {activation.site_id: activation for activation in activations}
    _emit_sites(out, protocol.study_id, activations, protocol.enrolment.first_subject_in)

    # --------------------------------------------------------------- spine
    spine = build_spine(
        lifecycle,
        [
            (
                activation.site_id,
                protocol.enrolment.first_subject_in
                + timedelta(weeks=activation.ready_week),
            )
            for activation in activations
        ],
        protocol.enrolment.first_subject_in,
        rngs.child("lifecycle", "spine"),
        ids,
        manufacturing_export=manufacturing_export,
        shelf_life_months=shelf_life_months,
    )
    out.spine_linked = spine.linked

    enrolments = allocate_enrolment(activations, config, rngs.child("clin", "enrolment"))
    arm_sequence = _permuted_blocks(protocol.arms, rngs.child("clin", "randomisation"))

    turnover_multiplier = config.sites.staff_turnover.entry_lag_multiplier
    contexts: list[SubjectContext] = []

    for index, enrolment in enumerate(enrolments):
        subject_id = f"{protocol.study_id}-{index + 1:04d}"
        arm_id = arm_sequence[index]
        arm = protocol.arm(arm_id)
        assert arm is not None
        activation = by_site[enrolment.site_id]
        randomised = enrolment.randomised
        available = max(cutoff_week - enrolment.week, 0.0)

        subject_rng = rngs.child("clin", "subject", subject_id)
        consent = randomised - timedelta(days=subject_rng.randint(7, 28))

        tumour = build_tumour(
            subject_id, arm_id, tumour_config,
            rngs.child("clin", "tumour", subject_id), horizon_weeks=horizon,
        )
        weeks = assessment_weeks(
            tumour_config, rngs.child("clin", "schedule", subject_id), horizon
        )

        out.subjects.append(
            {
                "STUDYID": protocol.study_id,
                "USUBJID": subject_id,
                "SITEID": enrolment.site_id,
                "COUNTRY": activation.country,
                "ARM": arm_id,
                "ARMCD": arm_id,
                "TRT01P": arm.label,
                "RFICDTC": _iso(consent),
                "RANDDT": _iso(randomised),
                "RFSTDTC": _iso(randomised),
                "ENROL_WEEK": round(enrolment.week, 2),
                "FOLLOWUP_WEEKS": round(available, 2),
            }
        )
        out.truth.append(
            {
                "USUBJID": subject_id,
                "ARM": arm_id,
                "SITEID": enrolment.site_id,
                "measurable_lesions": len(tumour.lesions),
                "sensitive_fraction": round(tumour.growth.sensitive_fraction, 5),
                "shrinkage_rate_per_week": round(tumour.growth.shrinkage_rate_per_week, 6),
                "growth_rate_per_week": round(tumour.growth.growth_rate_per_week, 6),
                "new_lesion_week": tumour.new_lesion_week,
                "non_target_progression_week": tumour.non_target_progression_week,
                "death_week": tumour.death_week,
            }
        )

        # ---------------------------------------------------------- safety
        # Generated BEFORE the assessments, because the dose a subject actually
        # received changes their tumour trajectory and therefore when they
        # progress. The window is the whole available follow-up rather than the
        # time to progression -- which is not known yet -- and the exposure is
        # truncated to the cycles actually reached once it is.
        events = generate_events(
            subject_id, arm_id, available, config,
            rngs.child("clin", "safety", subject_id), ids,
        )
        planned_cycles = max(1, int(available // cycle_weeks) + 1)
        exposure = apply_dose_modifications(
            subject_id, events, planned_cycles, cycle_weeks, config.dose_modification
        )
        starting_dose = config.dose_modification.starting_dose_mg
        tumour.dose_history = exposure.dose_history(starting_dose, cycle_weeks)

        primary_course: list[Assessment] = []
        primary_outcome = None
        assessment_facts: dict[int, dict[str, object]] = {}

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
                        rngs.child("clin", "measure", subject_id, reader.reader_id, f"{week}"),
                        missed=missed,
                    )
                )

            course = evaluate_course(timepoints, baseline, rules, reader.reader_id)

            # Tumour assessment stops at documented progression. A subject who
            # progresses comes off study treatment and moves to survival
            # follow-up, so there are no further RECIST timepoints. Continuing to
            # assess them produced sums of over 1,500 mm by the data cut, because
            # a resistant tumour grows exponentially and nothing was stopping it.
            progressed = next(
                (index for index, a in enumerate(course) if a.response == "PD"), None
            )
            if progressed is not None:
                course = course[: progressed + 1]
                timepoints = timepoints[: progressed + 1]

            _emit_tr(
                out, protocol.study_id, subject_id, reader.reader_id, reader.role,
                [baseline, *timepoints], randomised,
            )
            _emit_rs(
                out, protocol.study_id, subject_id, reader.reader_id, reader.role,
                course, rules, randomised,
            )
            outcome = derive_pfs(
                course, evaluator=reader.reader_id,
                death_week=tumour.death_week, analysis_week=available,
            )
            _emit_adtte(
                out, protocol.study_id, subject_id, arm, outcome, randomised, available
            )

            # The investigator's read is what drives treatment decisions and
            # therefore what the case report form records.
            if reader.role == "INVESTIGATOR":
                primary_course, primary_outcome = course, outcome
                for order, assessment in enumerate(course, start=1):
                    visitnum, _ = _visit_label(order)
                    assessment_facts[visitnum] = {
                        "SUM_OF_DIAMETERS": round(assessment.target.sum_of_diameters_mm, 1),
                        "OVERALL_RESPONSE": assessment.response,
                        "NEW_LESION": "Y" if assessment.new_lesion else "N",
                        "NON_TARGET_RESPONSE": assessment.non_target,
                    }

        assert primary_outcome is not None

        # Now that progression is known, keep only what the subject reached.
        on_treatment = min(primary_outcome.week, available)
        if exposure.discontinued_week is not None:
            on_treatment = min(on_treatment, exposure.discontinued_week)
        reached = max(1, int(on_treatment // cycle_weeks) + 1)
        exposure.truncate(reached)
        events = [event for event in events if event.onset_week <= on_treatment]

        _emit_safety(
            out, protocol.study_id, subject_id, arm, events, exposure,
            randomised, cycle_weeks, config,
        )

        contexts.append(
            _build_context(
                subject_id, enrolment.site_id, arm_id, randomised, consent,
                primary_course, primary_outcome, assessment_facts,
                activation, enrolment.week, available, cycle_weeks,
                turnover_multiplier, ids, rngs.child("clin", "cycles", subject_id),
                spine, lifecycle, out, arm, exposure,
            )
        )

    # ------------------------------------------------------------------- EDC
    edc = generate_edc(contexts, config, lambda sid: rngs.child("clin", "edc", sid), ids)
    out.forms = edc.forms
    out.item_data = edc.items
    out.queries = edc.queries
    out.query_events = edc.query_events
    out.item_audit = edc.audit

    # ------------------------------------------------------------- oversight
    oversight = generate_oversight(
        config,
        activations,
        out.subjects,
        out.forms,
        out.queries,
        protocol.enrolment.first_subject_in,
        lambda key: rngs.child("clin", "oversight", key),
        ids,
    )
    out.monitoring_visits = oversight.monitoring_visits
    out.findings = oversight.findings
    out.action_items = oversight.action_items
    out.sdv = oversight.sdv
    out.deviations = oversight.deviations
    out.tmf_documents = oversight.tmf_documents
    out.reconciliations = oversight.reconciliations
    out.lock_events = oversight.lock_events

    _emit_spine(out, spine)

    return out


def _emit_spine(out: StudyOutput, spine: Spine) -> None:
    """Record the investigational product chain."""
    for batch in spine.batches:
        pass  # batches belong to the manufacturing dataset, not this one
    for lot in spine.lots:
        out.imp_lots.append(
            {
                "lot_id": lot.lot_id,
                "batch_id": lot.batch_id,
                "product_id": lot.product_id,
                "role": lot.role,
                "kits": lot.kits,
                "packed_on": lot.packed_on.isoformat(),
                "expiry": lot.expiry.isoformat(),
                "batch_source": "STUB" if lot.stub_batch else "MANUFACTURING",
                "expiry_source": lot.expiry_source,
                "shelf_life_months": lot.shelf_life_months,
            }
        )
    for shipment in spine.shipments:
        out.imp_shipments.append(
            {
                "shipment_id": shipment.shipment_id,
                "site_id": shipment.site_id,
                "lot_ids": ",".join(shipment.lot_ids),
                "kits": shipment.kits,
                "shipped_on": shipment.shipped_on.isoformat(),
                "received_on": shipment.received_on.isoformat(),
                "status": shipment.status,
                "temperature_excursion": "Y" if shipment.temperature_excursion else "N",
            }
        )
    for kit in spine.kits:
        out.imp_kits.append(
            {
                "kit_number": kit.kit_number,
                "lot_id": kit.lot_id,
                "batch_id": kit.batch_id,
                "role": kit.role,
                "shipment_id": kit.shipment_id,
                "site_id": kit.site_id,
            }
        )
    out.imp_accountability = list(spine.accountability)
    for stockout in spine.stockouts:
        out.imp_accountability.append(
            {
                "accountability_id": "",
                "site_id": stockout["site_id"],
                "shipment_id": "",
                "event": "STOCKOUT",
                "kits": 0,
                "occurred_on": stockout["occurred_on"],
                "detail": f"No {stockout['role']} kit available for "
                          f"{stockout['subject_id']} cycle {stockout['cycle']}",
            }
        )


def _build_context(
    subject_id: str,
    site_id: str,
    arm_id: str,
    randomised: date,
    consent: date,
    course: list[Assessment],
    outcome: PfsOutcome,
    assessment_facts: dict[int, dict[str, object]],
    activation: SiteActivation,
    enrol_week: float,
    available_weeks: float,
    cycle_weeks: float,
    turnover_multiplier: float,
    ids: IdFactory,
    rng,
    spine: Spine,
    lifecycle: LifecycleConfig,
    out: StudyOutput,
    arm,
    exposure: Exposure,
) -> SubjectContext:
    """Assemble the EDC view of one subject from what the study already computed."""
    # Treatment runs until progression, death or the data cut -- or until
    # toxicity stops it, which is what the exposure already worked out.
    on_treatment = min(outcome.week, available_weeks)
    cycles = len(exposure.dose_by_cycle) or max(1, int(on_treatment // cycle_weeks) + 1)
    if exposure.discontinued_week is not None:
        on_treatment = min(on_treatment, exposure.discontinued_week)

    visits: list[VisitRecord] = [
        VisitRecord(visitnum=0, visit="SCREENING", day=consent, kind="SCREENING")
    ]
    cycle_facts: dict[int, dict[str, object]] = {}
    role = lifecycle.randomisation.role_for(arm_id)
    reduced_at = {
        modification.cycle
        for modification in exposure.modifications
        if modification.action == "INTERRUPT_AND_REDUCE"
    }
    previous_dose: float | None = None

    for cycle in range(1, cycles + 1):
        visitnum = 100 + cycle
        day = randomised + timedelta(weeks=(cycle - 1) * cycle_weeks)
        # The dose is whatever the toxicity rules left it at, not a coin flip.
        dose = exposure.dose_by_cycle.get(cycle, previous_dose or 240.0)
        adjusted = cycle in reduced_at or (
            previous_dose is not None and dose != previous_dose
        )
        previous_dose = dose

        kit = None
        if role is not None:
            trigger = lifecycle.imp.shipment.resupply_trigger_kits
            if spine.kits_remaining(site_id, role, day) <= trigger:
                resupply(spine, site_id, role, day, rng)
            kit = spine.kit_for(site_id, role, day)
            if kit is None:
                spine.stockouts.append(
                    {
                        "site_id": site_id,
                        "subject_id": subject_id,
                        "cycle": cycle,
                        "role": role,
                        "occurred_on": day.isoformat(),
                    }
                )

        kit_number = kit.kit_number if kit is not None else ""
        cycle_facts[visitnum] = {
            "DOSE": dose,
            "DOSE_ADJUSTED": "Y" if adjusted else "N",
            "KIT_NUMBER": kit_number,
        }

        if kit is not None:
            out.dosing.append(
                {
                    "dosing_id": ids.next("DOS", width=7),
                    "subject_id": subject_id,
                    "site_id": site_id,
                    "cycle": cycle,
                    "visitnum": visitnum,
                    "dosed_on": day.isoformat(),
                    "kit_number": kit.kit_number,
                    "lot_id": kit.lot_id,
                    "batch_id": kit.batch_id,
                    "dose_mg": dose,
                    "dose_adjusted": "Y" if adjusted else "N",
                }
            )
            # SDTM EX. EXTRT stays at the blinded label because that is what the
            # form recorded; the kit reference is how it resolves to a batch.
            out.ex.append(
                {
                    "STUDYID": out.study_id,
                    "DOMAIN": "EX",
                    "USUBJID": subject_id,
                    "EXSEQ": cycle,
                    "EXTRT": "NELVORASIB OR PLACEBO",
                    "EXDOSE": dose,
                    "EXDOSU": "mg",
                    "EXDOSFRM": "TABLET, FILM COATED",
                    "EXDOSFRQ": "QD",
                    "EXROUTE": "ORAL",
                    "EXREFID": kit.kit_number,
                    "EXSTDTC": day.isoformat(),
                    "EXENDTC": (
                        day + timedelta(weeks=cycle_weeks) - timedelta(days=1)
                    ).isoformat(),
                    "VISITNUM": visitnum,
                    "VISIT": f"CYCLE {cycle} DAY 1",
                    # Not SDTM variables. Carried so the accountability chain is
                    # walkable without joining three more tables.
                    "LOTID": kit.lot_id,
                    "BATCHID": kit.batch_id,
                }
            )

        visits.append(
            VisitRecord(visitnum=visitnum, visit=f"CYCLE {cycle} DAY 1", day=day, kind="CYCLE")
        )

    for order, assessment in enumerate(course, start=1):
        visitnum, label = _visit_label(order)
        visits.append(
            VisitRecord(
                visitnum=visitnum,
                visit=label,
                day=randomised + timedelta(weeks=assessment.week),
                kind="ASSESSMENT",
            )
        )

    end_of_treatment = randomised + timedelta(weeks=on_treatment)
    # Toxicity that stopped treatment takes precedence over progression: it
    # happened first, which is why the subject never reached progression on
    # treatment.
    reason = (
        "ADVERSE EVENT"
        if exposure.discontinued_week is not None
        else {"PROGRESSION": "DISEASE PROGRESSION", "DEATH": "DEATH"}.get(outcome.reason)
    )
    if reason is not None:
        visits.append(
            VisitRecord(
                visitnum=900, visit="END OF TREATMENT", day=end_of_treatment,
                kind="END_OF_TREATMENT",
            )
        )

    return SubjectContext(
        subject_id=subject_id,
        site_id=site_id,
        arm=arm_id,
        randomised=randomised,
        consent_date=consent,
        visits=tuple(sorted(visits, key=lambda visit: (visit.day, visit.visitnum))),
        assessment_facts=assessment_facts,
        cycle_facts=cycle_facts,
        end_of_treatment=end_of_treatment if reason else None,
        discontinuation_reason=reason,
        entry_lag_days=activation.entry_lag_at(enrol_week, turnover_multiplier),
        query_rate_per_form=activation.query_rate_per_form,
        query_response_days=activation.query_response_days,
    )


def _emit_safety(
    out: StudyOutput,
    study_id: str,
    subject_id: str,
    arm,
    events: list[AdverseEvent],
    exposure: Exposure,
    randomised: date,
    cycle_weeks: float,
    config,
) -> None:
    """SDTM AE, the dose actions, and the exposure summary."""
    for sequence, event in enumerate(events, start=1):
        onset = randomised + timedelta(weeks=event.onset_week)
        out.ae.append(
            {
                "STUDYID": study_id,
                "DOMAIN": "AE",
                "USUBJID": subject_id,
                "AESEQ": sequence,
                "AETERM": event.pt,
                "AEDECOD": event.pt,
                "AEPTCD": event.pt_code,
                "AEBODSYS": event.soc,
                "AESOCCD": event.soc_code,
                # Oncology grades to CTCAE, so AETOXGR is the variable that
                # matters and AESEV is derived from it rather than the reverse.
                "AETOXGR": event.grade,
                "AESEV": {1: "MILD", 2: "MODERATE", 3: "SEVERE", 4: "SEVERE"}.get(
                    event.grade, "SEVERE"
                ),
                "AESER": "Y" if event.serious else "N",
                "AESERCRIT": event.seriousness_criterion or "",
                "AEREL": "RELATED" if event.related else "NOT RELATED",
                "AEACN": _action_taken(event, exposure),
                "AEOUT": "RECOVERED/RESOLVED",
                "AESTDTC": _iso(onset),
                "AEENDTC": _iso(randomised + timedelta(weeks=event.end_week)),
                "AECAT": event.category,
                "AESCAN": event.special_interest or "",
                # Not SDTM. Carried so the expedited reporting story is walkable
                # without joining a safety database that does not exist here.
                "ATTRIBUTION": event.attribution,
                "UNEXPECTED": "Y" if event.unexpected else "N",
                "SUSAR": "Y" if event.susar else "N",
                "SITE_AWARE_DTC": _iso(
                    randomised + timedelta(weeks=event.site_awareness_week)
                ),
                "SPONSOR_NOTIFIED_DTC": _iso(
                    randomised + timedelta(weeks=event.sponsor_notified_week)
                ),
                "REPORT_DUE_DTC": (
                    "" if event.reporting_due_week is None
                    else _iso(randomised + timedelta(weeks=event.reporting_due_week))
                ),
                "REPORTED_ON_TIME": (
                    "" if event.reported_within_timeline is None
                    else ("Y" if event.reported_within_timeline else "N")
                ),
            }
        )

    for modification in exposure.modifications:
        out.dose_modifications.append(
            {
                "subject_id": subject_id,
                "ae_id": modification.ae_id,
                "rule_id": modification.rule_id,
                "action": modification.action,
                "reason": modification.reason,
                "cycle": modification.cycle,
                "occurred_on": _iso(randomised + timedelta(weeks=modification.week)),
                "dose_before_mg": modification.dose_before_mg,
                "dose_after_mg": modification.dose_after_mg,
                "interruption_days": modification.interruption_days,
            }
        )

    starting = config.dose_modification.starting_dose_mg
    out.exposure.append(
        {
            "subject_id": subject_id,
            "arm": arm.arm_id,
            "cycles_received": len(exposure.dose_by_cycle),
            "final_dose_mg": (
                max(exposure.dose_by_cycle) and exposure.dose_by_cycle[
                    max(exposure.dose_by_cycle)
                ]
                if exposure.dose_by_cycle else 0.0
            ),
            "dose_reductions": sum(
                1 for m in exposure.modifications
                if m.action == "INTERRUPT_AND_REDUCE"
            ),
            "interruptions": sum(
                1 for m in exposure.modifications if m.action.startswith("INTERRUPT")
            ),
            "interruption_days_total": round(
                sum(m.interruption_days for m in exposure.modifications), 1
            ),
            "discontinued_for_toxicity": "Y" if exposure.discontinued_week else "N",
            "discontinuation_reason": exposure.discontinuation_reason or "",
            "relative_dose_intensity": round(
                exposure.relative_dose_intensity(starting, cycle_weeks * 7.0), 4
            ),
        }
    )


def _action_taken(event: AdverseEvent, exposure: Exposure) -> str:
    """SDTM AEACN, resolved from what actually happened to the dose."""
    for modification in exposure.modifications:
        if modification.ae_id != event.ae_id:
            continue
        if modification.action == "PERMANENT_DISCONTINUATION":
            return "DRUG WITHDRAWN"
        if modification.action == "INTERRUPT_AND_REDUCE":
            return "DOSE REDUCED"
        return "DRUG INTERRUPTED"
    return "DOSE NOT CHANGED"


def _emit_sites(
    out: StudyOutput,
    study_id: str,
    activations: list[SiteActivation],
    first_in: date,
) -> None:
    """CTMS site and milestone records."""
    for activation in activations:
        out.sites.append(
            {
                "STUDYID": study_id,
                "SITEID": activation.site_id,
                "COUNTRY": activation.country,
                "SITE_NAME": activation.name,
                "PRINCIPAL_INVESTIGATOR": activation.principal_investigator,
                "ARCHETYPE": activation.archetype,
                "READY_WEEK": round(activation.ready_week, 2),
                "READY_DATE": _iso(first_in + timedelta(weeks=activation.ready_week)),
                "PLANNED_ENROLMENT_PER_MONTH": round(activation.enrolment_per_month, 2),
                "ENTRY_LAG_DAYS": round(activation.entry_lag_days, 2),
                "STAFF_TURNOVER_FROM_WEEK": (
                    None if activation.turnover_from is None
                    else round(activation.turnover_from, 1)
                ),
                "STAFF_TURNOVER_TO_WEEK": (
                    None if activation.turnover_to is None
                    else round(activation.turnover_to, 1)
                ),
            }
        )
        for milestone, week in activation.milestones.items():
            out.site_milestones.append(
                {
                    "STUDYID": study_id,
                    "SITEID": activation.site_id,
                    "MILESTONE": milestone,
                    "WEEK": round(week, 2),
                    "MILESTONE_DATE": _iso(first_in + timedelta(weeks=week)),
                }
            )


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
