"""Monitoring, deviations, the trial master file and database lock.

The three things a practitioner checks first in this layer, and what makes them
mean anything here:

* **Monitoring is triggered, not scheduled into existence.** Initiation visits
  come from the milestone chain, routine visits from an interval, close-out from
  the end of the study, and a for-cause visit from a site's own query rate
  crossing a declared multiple of the study mean. Which site gets one is an
  outcome.
* **TMF completeness is a consequence.** An artifact becomes expected when its
  milestone is reached — an executed contract is not expected before the contract
  exists — and then arrives, arrives late, or never arrives. The percentage is
  whatever that produces.
* **Database lock has to be earned.** Open queries are counted, reconciliations
  run, and the soft lock waits on them. A lock date with outstanding critical
  queries behind it is the first thing an auditor finds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from random import Random

from pharma_sim.clinical.config import ClinicalConfig
from pharma_sim.clinical.ctms import SiteActivation

__all__ = ["OversightOutput", "generate_oversight"]

#: Points in the study's life that make a trial-level artifact expected.
_STUDY_EVENTS = frozenset(
    {"STUDY_START", "END_OF_STUDY", "DATABASE_LOCK", "AMENDMENT", "IDMC_REVIEW"}
)


@dataclass
class OversightOutput:
    monitoring_visits: list[dict] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    action_items: list[dict] = field(default_factory=list)
    sdv: list[dict] = field(default_factory=list)
    deviations: list[dict] = field(default_factory=list)
    tmf_documents: list[dict] = field(default_factory=list)
    reconciliations: list[dict] = field(default_factory=list)
    lock_events: list[dict] = field(default_factory=list)

    def tmf_completeness(self) -> float:
        if not self.tmf_documents:
            return 0.0
        filed = sum(1 for row in self.tmf_documents if row["status"] == "FILED")
        return 100.0 * filed / len(self.tmf_documents)

    def tmf_timeliness(self) -> float:
        filed = [row for row in self.tmf_documents if row["status"] == "FILED"]
        if not filed:
            return 0.0
        on_time = sum(1 for row in filed if not row["late"])
        return 100.0 * on_time / len(filed)


def _weighted(rng: Random, options, weight) -> object:
    total = sum(weight(option) for option in options)
    threshold = rng.random() * total
    running = 0.0
    for option in options:
        running += weight(option)
        if threshold <= running:
            return option
    return options[-1]


def generate_oversight(
    config: ClinicalConfig,
    activations: list[SiteActivation],
    subjects: list[dict],
    forms: list[dict],
    queries: list[dict],
    first_in: date,
    rng_for,
    ids,
) -> OversightOutput:
    """Build the whole oversight layer from what the study produced."""
    out = OversightOutput()
    monitoring = config.monitoring
    cutoff_week = config.protocol.analysis.cutoff_weeks_from_fsi

    forms_by_site: dict[str, int] = {}
    for form in forms:
        forms_by_site[form["site_id"]] = forms_by_site.get(form["site_id"], 0) + 1
    queries_by_site: dict[str, int] = {}
    for query in queries:
        queries_by_site[query["site_id"]] = queries_by_site.get(query["site_id"], 0) + 1
    subjects_by_site: dict[str, list[dict]] = {}
    for subject in subjects:
        subjects_by_site.setdefault(subject["SITEID"], []).append(subject)

    mean_rate = (len(queries) / len(forms)) if forms else 0.0

    _monitoring(
        out, config, activations, forms_by_site, queries_by_site, subjects_by_site,
        mean_rate, first_in, cutoff_week, rng_for, ids,
    )
    _deviations(out, config, activations, subjects_by_site, first_in, rng_for, ids)
    _sdv(out, config, forms, rng_for, ids)
    _tmf(out, config, activations, first_in, cutoff_week, rng_for, ids)
    _lock(out, config, queries, first_in, cutoff_week, rng_for("lock"), ids)
    return out


def _monitoring(
    out, config, activations, forms_by_site, queries_by_site, subjects_by_site,
    mean_rate, first_in, cutoff_week, rng_for, ids,
) -> None:
    monitoring = config.monitoring
    trigger = monitoring.for_cause_trigger

    for activation in activations:
        rng = rng_for(f"monitor:{activation.site_id}")
        site_forms = forms_by_site.get(activation.site_id, 0)
        site_queries = queries_by_site.get(activation.site_id, 0)
        site_subjects = subjects_by_site.get(activation.site_id, [])
        rate = (site_queries / site_forms) if site_forms else 0.0

        planned: list[tuple[str, float, str]] = []
        for visit_type in monitoring.visit_types:
            if visit_type.trigger == "MILESTONE":
                week = activation.milestones.get(visit_type.milestone or "", None)
                if week is not None:
                    planned.append((visit_type.visit_type, week, "SCHEDULED"))
            elif visit_type.trigger == "PERIODIC":
                week = activation.ready_week + (visit_type.interval_weeks or 14.0)
                while week <= cutoff_week:
                    planned.append((visit_type.visit_type, week, "SCHEDULED"))
                    week += visit_type.interval_weeks or 14.0
            elif visit_type.trigger == "RISK":
                # For cause: this site's query rate against the study mean.
                if (
                    mean_rate > 0.0
                    and len(site_subjects) >= trigger.minimum_subjects
                    and rate >= trigger.query_rate_multiple_of_mean * mean_rate
                ):
                    planned.append((
                        visit_type.visit_type,
                        min(activation.ready_week + 30.0, cutoff_week),
                        "TRIGGERED",
                    ))
            elif visit_type.trigger == "END_OF_STUDY":
                planned.append((visit_type.visit_type, cutoff_week, "SCHEDULED"))

        for visit_type, week, origin in sorted(planned, key=lambda row: row[1]):
            visit_id = ids.next("MV", width=5)
            day = first_in + timedelta(weeks=week)
            out.monitoring_visits.append(
                {
                    "monitoring_visit_id": visit_id,
                    "site_id": activation.site_id,
                    "visit_type": visit_type,
                    "origin": origin,
                    "week": round(week, 2),
                    "visit_date": day.isoformat(),
                    "monitor": f"CRA-{activation.country}",
                    "report_due": (day + timedelta(days=10)).isoformat(),
                    "query_rate_at_visit": round(rate, 4),
                    "query_rate_multiple_of_mean": (
                        round(rate / mean_rate, 2) if mean_rate else None
                    ),
                }
            )

            for finding in monitoring.findings:
                # A for-cause visit finds more, which is why it was called.
                scale = 1.8 if origin == "TRIGGERED" else 1.0
                count = 1 if rng.random() < min(1.0, finding.rate_per_visit * scale) else 0
                for _ in range(count):
                    finding_id = ids.next("FND", width=6)
                    out.findings.append(
                        {
                            "finding_id": finding_id,
                            "monitoring_visit_id": visit_id,
                            "site_id": activation.site_id,
                            "category": finding.category,
                            "severity": finding.severity,
                            "text": finding.text,
                            "raised_date": day.isoformat(),
                        }
                    )
                    due = day + timedelta(days=monitoring.action_items.due_days)
                    overdue = rng.random() < monitoring.action_items.overdue_probability
                    closed = due + timedelta(
                        days=int(rng.uniform(4, 45)) if overdue else -int(rng.uniform(0, 20))
                    )
                    out.action_items.append(
                        {
                            "action_item_id": ids.next("ACT", width=6),
                            "finding_id": finding_id,
                            "site_id": activation.site_id,
                            "description": f"Resolve: {finding.text}",
                            "due_date": due.isoformat(),
                            "closed_date": closed.isoformat(),
                            "status": "CLOSED_LATE" if overdue else "CLOSED",
                        }
                    )


def _deviations(out, config, activations, subjects_by_site, first_in, rng_for, ids) -> None:
    categories = config.monitoring.deviations.categories
    exclude_probability = config.monitoring.deviations.eligibility_violation_excludes_from_pp

    for activation in activations:
        rng = rng_for(f"deviation:{activation.site_id}")
        for subject in subjects_by_site.get(activation.site_id, []):
            expected = activation.deviation_rate_per_subject
            count = 0
            # A rate above one means more than one deviation is likely.
            while rng.random() < expected and count < 4:
                count += 1
                category = _weighted(rng, categories, lambda option: option.weight)
                week = subject["ENROL_WEEK"] + rng.uniform(1.0, 40.0)
                excluded = (
                    category.category == "ELIGIBILITY_VIOLATION"
                    and rng.random() < exclude_probability
                )
                out.deviations.append(
                    {
                        "deviation_id": ids.next("DV", width=5),
                        "subject_id": subject["USUBJID"],
                        "site_id": activation.site_id,
                        "category": category.category,
                        "classification": category.classification,
                        "week": round(week, 2),
                        "deviation_date": (first_in + timedelta(weeks=week)).isoformat(),
                        "excluded_from_per_protocol": "Y" if excluded else "N",
                    }
                )
                expected *= 0.4  # a second deviation on one subject is less likely


def _sdv(out, config, forms, rng_for, ids) -> None:
    """Risk-based source data verification.

    Critical data is verified for every subject; everything else is sampled. The
    coverage figure that comes out is therefore defensible rather than declared.
    """
    strategy = config.monitoring.source_data_verification
    critical = set(strategy.critical_items)
    items_by_form = {
        form.form_id: [item.item_id for item in form.items] for form in config.crf.forms
    }

    for form in forms:
        rng = rng_for(f"sdv:{form['form_instance_id']}")
        item_ids = items_by_form.get(form["form_id"], [])
        for item_id in item_ids:
            is_critical = item_id in critical
            if not is_critical and rng.random() >= strategy.non_critical_sample_rate:
                continue
            out.sdv.append(
                {
                    "sdv_id": ids.next("SDV", width=8),
                    "form_instance_id": form["form_instance_id"],
                    "subject_id": form["subject_id"],
                    "site_id": form["site_id"],
                    "form_id": form["form_id"],
                    "item_id": item_id,
                    "critical": "Y" if is_critical else "N",
                    "verified": "Y",
                    "strategy": strategy.strategy,
                }
            )


def _tmf(out, config, activations, first_in, cutoff_week, rng_for, ids) -> None:
    tmf = config.tmf
    countries = sorted({activation.country for activation in activations})
    visits_per_site = {
        activation.site_id: activation for activation in activations
    }

    def file_document(artifact, level: str, scope: str, expected_week: float) -> None:
        rng = rng_for(f"tmf:{artifact.artifact}:{scope}")
        document_id = ids.next("DOC", width=6)
        missing = rng.random() < artifact.missing_probability
        arrival = max(
            0.0, rng.gauss(artifact.arrival_weeks.mean, artifact.arrival_weeks.sd)
        )
        filed_week = expected_week + arrival
        late = arrival > tmf.timeliness_target_weeks
        versions = 2 if rng.random() < tmf.version_probability else 1

        out.tmf_documents.append(
            {
                "document_id": document_id,
                "artifact": artifact.artifact,
                "artifact_name": artifact.name,
                "zone": artifact.zone,
                "zone_name": tmf.zone_name(artifact.zone),
                "level": level,
                "scope": scope,
                "expected_at": artifact.expected_at or "MONITORING_VISIT",
                "expected_week": round(expected_week, 2),
                "expected_date": (first_in + timedelta(weeks=expected_week)).isoformat(),
                "status": "MISSING" if missing else "FILED",
                "filed_week": None if missing else round(filed_week, 2),
                "filed_date": (
                    None if missing
                    else (first_in + timedelta(weeks=filed_week)).isoformat()
                ),
                "late": (not missing) and late,
                "versions": None if missing else versions,
            }
        )

    for artifact in tmf.artifacts:
        if artifact.level == "TRIAL":
            expected = 0.0 if artifact.expected_at == "STUDY_START" else cutoff_week
            file_document(artifact, "TRIAL", config.protocol.study_id, expected)
        elif artifact.level == "COUNTRY":
            for country in countries:
                sites = [a for a in activations if a.country == country]
                if artifact.expected_at in _STUDY_EVENTS:
                    expected = 0.0 if artifact.expected_at == "STUDY_START" else cutoff_week
                else:
                    expected = min(
                        a.milestones.get(artifact.expected_at or "", cutoff_week)
                        for a in sites
                    )
                file_document(artifact, "COUNTRY", country, expected)
        elif artifact.level == "SITE":
            for activation in activations:
                if artifact.expected_at in _STUDY_EVENTS:
                    expected = 0.0 if artifact.expected_at == "STUDY_START" else cutoff_week
                else:
                    expected = activation.milestones.get(
                        artifact.expected_at or "", cutoff_week
                    )
                file_document(artifact, "SITE", activation.site_id, expected)
        elif artifact.level == "MONITORING_VISIT":
            for visit in out.monitoring_visits:
                file_document(artifact, "MONITORING_VISIT", visit["monitoring_visit_id"],
                              visit["week"])


def _lock(out, config, queries, first_in, cutoff_week, rng, ids) -> None:
    """Reconciliation, query burn-down and the two locks.

    The soft lock waits on the reconciliations closing and on the query count
    reaching zero. Emitting a lock date without that behind it is the thing an
    auditor asks about first.
    """
    scopes = [
        ("SAFETY_DATABASE", "SAE reconciliation between EDC and the safety database"),
        ("IMAGING_VENDOR", "Tumour assessment reconciliation with the imaging vendor"),
        ("CENTRAL_LABORATORY", "Central laboratory data reconciliation"),
        ("IRT", "Randomisation and drug accountability reconciliation"),
        ("PHARMACOKINETICS", "Pharmacokinetic sample reconciliation"),
    ]
    cursor = cutoff_week
    for scope, description in scopes:
        discrepancies = int(rng.uniform(0, 14))
        duration = max(0.5, rng.gauss(2.5, 1.2))
        cursor += duration
        out.reconciliations.append(
            {
                "reconciliation_id": ids.next("REC", width=4),
                "scope": scope,
                "description": description,
                "started_week": round(cutoff_week, 2),
                "completed_week": round(cursor, 2),
                "completed_date": (first_in + timedelta(weeks=cursor)).isoformat(),
                "discrepancies_found": discrepancies,
                "discrepancies_resolved": discrepancies,
                "status": "COMPLETE",
            }
        )

    cut_date = (first_in + timedelta(weeks=cutoff_week)).isoformat()
    open_at_cutoff = sum(1 for query in queries if query["closed_at"] > cut_date)
    # Clean-up runs alongside the reconciliations rather than after them, and the
    # soft lock waits for whichever finishes last.
    burn_down = cutoff_week + max(1.0, open_at_cutoff * 0.045 + rng.uniform(1.5, 3.5))
    soft = max(cursor, burn_down) + max(0.3, rng.gauss(1.0, 0.4))
    hard = soft + max(0.5, rng.gauss(2.0, 0.6))

    for name, week, detail in (
        ("DATA_CUT", cutoff_week, "Analysis data cut-off reached"),
        (
            "QUERY_BURN_DOWN_COMPLETE",
            burn_down,
            f"{open_at_cutoff} queries open at the data cut, all resolved",
        ),
        ("SOFT_LOCK", soft, "Database soft locked; no further site data entry"),
        ("HARD_LOCK", hard, "Database hard locked"),
        ("UNBLINDING", hard + 0.5, "Treatment assignment released to the study team"),
    ):
        out.lock_events.append(
            {
                "lock_event_id": ids.next("LCK", width=3),
                "event": name,
                "week": round(week, 2),
                "event_date": (first_in + timedelta(weeks=week)).isoformat(),
                "detail": detail,
            }
        )
