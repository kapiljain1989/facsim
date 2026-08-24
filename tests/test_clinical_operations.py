"""The operational layer: case report forms, queries, monitoring, TMF and lock.

Two properties matter more than the rest.

The first is that the case report form and the SDTM datasets are two views of one
set of facts. A tumour assessment form carries the sum of diameters the lesion
model computed, so a query that corrects it is visible in both. If they were
generated separately they would be individually plausible and mutually
inconsistent, which is the failure mode that makes synthetic clinical data
useless for anything that joins across systems.

The second is that site performance propagates. One site's archetype has to show
up as more queries, slower entry, more deviations and a for-cause monitoring
visit — not as a flag on a row.
"""

from __future__ import annotations

import collections
import statistics as stats
from pathlib import Path

import pytest

from pharma_sim.clinical.loader import load_clinical_config
from pharma_sim.clinical.study import run_study

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "clinical"


@pytest.fixture(scope="module")
def config():
    return load_clinical_config(CONFIG_DIR)


@pytest.fixture(scope="module")
def study(config):
    return run_study(config, seed=42)


class TestFormsAndItems:
    def test_forms_exist_for_every_subject(self, study):
        with_forms = {row["subject_id"] for row in study.forms}
        assert with_forms == {row["USUBJID"] for row in study.subjects}

    def test_every_item_belongs_to_a_form_instance(self, study):
        instances = {row["form_instance_id"] for row in study.forms}
        assert {row["form_instance_id"] for row in study.item_data} <= instances

    def test_a_form_is_entered_after_the_visit_not_on_it(self, study):
        for row in study.forms:
            assert row["entered_at"] >= row["visit_date"]
            assert row["entry_lag_days"] >= 0.0

    def test_entry_lag_reflects_the_site(self, study):
        """The archetype has to be visible in the data, not just in the config."""
        by_site: dict[str, list[float]] = collections.defaultdict(list)
        for row in study.forms:
            by_site[row["site_id"]].append(row["entry_lag_days"])
        means = {site: stats.fmean(values) for site, values in by_site.items()}
        archetypes = {row["SITEID"]: row["ARCHETYPE"] for row in study.sites}
        worst = max(means, key=lambda site: means[site])
        assert archetypes[worst] == "POOR_DATA_QUALITY"

    def test_every_declared_form_is_used(self, study, config):
        used = {row["form_id"] for row in study.forms}
        assert used == {form.form_id for form in config.crf.forms}


class TestTheCrfAgreesWithTheDatasets:
    """The property that makes the dataset usable across systems."""

    def test_recorded_sum_of_diameters_matches_the_response_dataset(self, study):
        visit_of = {row["form_instance_id"]: row["visitnum"] for row in study.forms}
        reported = {
            (row["USUBJID"], row["VISITNUM"]): row["SUMDIAM"]
            for row in study.rs
            if row["RSTESTCD"] == "OVRLRESP" and row["RSEVALID"] == "INV"
        }
        checked = 0
        for row in study.item_data:
            if row["item_id"] != "SUMDIAM" or row["value"] == "":
                continue
            key = (row["subject_id"], visit_of[row["form_instance_id"]])
            if key not in reported:
                continue
            # Items corrected in response to a query legitimately differ; the
            # audit trail records those, and they are excluded here.
            corrected = any(
                entry["form_instance_id"] == row["form_instance_id"]
                and entry["item_id"] == "SUMDIAM"
                for entry in study.item_audit
            )
            if corrected:
                continue
            assert float(row["value"]) == pytest.approx(reported[key], abs=0.15)
            checked += 1
        assert checked > 500

    def test_recorded_response_matches_the_response_dataset(self, study):
        visit_of = {row["form_instance_id"]: row["visitnum"] for row in study.forms}
        reported = {
            (row["USUBJID"], row["VISITNUM"]): row["RSSTRESC"]
            for row in study.rs
            if row["RSTESTCD"] == "OVRLRESP" and row["RSEVALID"] == "INV"
        }
        checked = 0
        for row in study.item_data:
            if row["item_id"] != "OVRLRESP" or row["value"] == "":
                continue
            key = (row["subject_id"], visit_of[row["form_instance_id"]])
            if key in reported:
                assert row["value"] == reported[key]
                checked += 1
        assert checked > 500


class TestQueries:
    def test_every_query_points_at_a_real_item_on_a_real_form(self, study):
        instances = {row["form_instance_id"] for row in study.forms}
        items = {(row["form_instance_id"], row["item_id"]) for row in study.item_data}
        for query in study.queries:
            assert query["form_instance_id"] in instances
            assert (query["form_instance_id"], query["item_id"]) in items

    def test_the_lifecycle_is_ordered(self, study):
        by_query: dict[str, list[dict]] = collections.defaultdict(list)
        for event in study.query_events:
            by_query[event["query_id"]].append(event)
        for query_id, events in by_query.items():
            times = [event["occurred_at"] for event in events]
            assert times == sorted(times), f"{query_id} events out of order"
            assert events[0]["state"] == "OPEN"
            assert events[-1]["state"] == "CLOSED"

    def test_requery_count_matches_the_events(self, study):
        counts = collections.Counter(
            event["query_id"]
            for event in study.query_events
            if event["state"] == "RE_QUERIED"
        )
        for query in study.queries:
            assert query["requeries"] == counts.get(query["query_id"], 0)

    def test_some_queries_are_re_asked(self, study):
        """Without re-query, ageing has one mode and looks nothing like reality."""
        assert any(query["requeries"] > 0 for query in study.queries)

    def test_a_re_queried_query_takes_longer(self, study):
        plain = [q["age_days"] for q in study.queries if q["requeries"] == 0]
        again = [q["age_days"] for q in study.queries if q["requeries"] > 0]
        assert plain and again
        assert stats.fmean(again) > stats.fmean(plain)

    def test_system_queries_name_the_check_that_fired(self, study):
        system = [q for q in study.queries if q["origin"] == "SYSTEM"]
        assert system
        assert all(query["check_id"] for query in system)

    def test_the_worst_site_generates_the_most_queries_per_form(self, study):
        forms = collections.Counter(row["site_id"] for row in study.forms)
        queries = collections.Counter(row["site_id"] for row in study.queries)
        rates = {site: queries[site] / count for site, count in forms.items()}
        archetypes = {row["SITEID"]: row["ARCHETYPE"] for row in study.sites}
        worst = max(rates, key=lambda site: rates[site])
        assert archetypes[worst] == "POOR_DATA_QUALITY"
        mean_rate = len(study.queries) / len(study.forms)
        assert rates[worst] > 2.0 * mean_rate


class TestAuditTrail:
    def test_no_entry_records_a_change_that_did_not_happen(self, study):
        assert all(
            row["old_value"] != row["new_value"] for row in study.item_audit
        )

    def test_the_latest_entry_agrees_with_the_current_value(self, study):
        current = {
            (row["form_instance_id"], row["item_id"]): row["value"]
            for row in study.item_data
        }
        latest: dict[tuple[str, str], dict] = {}
        for row in study.item_audit:
            latest[(row["form_instance_id"], row["item_id"])] = row
        for key, row in latest.items():
            assert current[key] == row["new_value"]

    def test_every_entry_cites_the_query_that_caused_it(self, study):
        query_ids = {row["query_id"] for row in study.queries}
        for row in study.item_audit:
            assert row["query_id"] in query_ids
            assert row["reason"]

    def test_a_correction_stays_within_the_edit_check_range(self, study, config):
        """A correction that immediately re-fires the check is not a correction."""
        ranges = {
            check.item_id: (check.low, check.high)
            for check in config.crf.edit_checks
            if check.kind == "RANGE" and check.low is not None
        }
        for row in study.item_audit:
            bounds = ranges.get(row["item_id"])
            if bounds is None:
                continue
            try:
                value = float(row["new_value"])
            except (TypeError, ValueError):
                continue
            assert bounds[0] <= value <= bounds[1]


class TestMonitoring:
    def test_every_site_gets_an_initiation_and_a_close_out_visit(self, study):
        sites = {row["SITEID"] for row in study.sites}
        for visit_type in ("SIV", "COV"):
            covered = {
                row["site_id"]
                for row in study.monitoring_visits
                if row["visit_type"] == visit_type
            }
            assert covered == sites

    def test_a_for_cause_visit_goes_only_to_a_site_above_the_threshold(self, study, config):
        threshold = config.monitoring.for_cause_trigger.query_rate_multiple_of_mean
        triggered = [
            row for row in study.monitoring_visits if row["origin"] == "TRIGGERED"
        ]
        assert triggered, "no for-cause visit was triggered"
        for row in triggered:
            assert row["query_rate_multiple_of_mean"] >= threshold

    def test_findings_belong_to_visits_and_action_items_to_findings(self, study):
        visits = {row["monitoring_visit_id"] for row in study.monitoring_visits}
        findings = {row["finding_id"] for row in study.findings}
        assert {row["monitoring_visit_id"] for row in study.findings} <= visits
        assert {row["finding_id"] for row in study.action_items} <= findings

    def test_some_action_items_close_late(self, study):
        statuses = {row["status"] for row in study.action_items}
        assert "CLOSED_LATE" in statuses


class TestSourceDataVerification:
    def test_critical_data_is_verified_wherever_it_appears(self, study, config):
        critical = set(config.monitoring.source_data_verification.critical_items)
        items_on_form = {
            form.form_id: {item.item_id for item in form.items}
            for form in config.crf.forms
        }
        verified = {
            (row["form_instance_id"], row["item_id"])
            for row in study.sdv
            if row["critical"] == "Y"
        }
        for form in study.forms:
            for item_id in items_on_form[form["form_id"]] & critical:
                assert (form["form_instance_id"], item_id) in verified

    def test_non_critical_data_is_sampled_not_exhausted(self, study):
        non_critical = [row for row in study.sdv if row["critical"] == "N"]
        assert non_critical
        assert len(non_critical) < len(study.item_data)


class TestTrialMasterFile:
    def test_completeness_is_plausible_and_not_perfect(self, study):
        """A TMF at 100% is not a TMF anybody has seen."""
        assert 80.0 < study.tmf_completeness < 99.0

    def test_nothing_is_filed_before_it_became_expected(self, study):
        for row in study.tmf_documents:
            if row["status"] != "FILED":
                continue
            assert row["filed_week"] >= row["expected_week"]

    def test_missing_documents_have_no_filing_date(self, study):
        for row in study.tmf_documents:
            if row["status"] == "MISSING":
                assert row["filed_week"] is None
                assert row["filed_date"] is None

    def test_site_level_artifacts_are_expected_once_per_site(self, study, config):
        sites = {row["SITEID"] for row in study.sites}
        site_artifacts = [
            artifact.artifact
            for artifact in config.tmf.artifacts
            if artifact.level == "SITE"
        ]
        per_artifact = collections.Counter(
            row["artifact"] for row in study.tmf_documents if row["level"] == "SITE"
        )
        for artifact in site_artifacts:
            assert per_artifact[artifact] == len(sites)

    def test_timeliness_is_reported_and_imperfect(self, study):
        assert 60.0 < study.tmf_timeliness < 99.0


class TestDatabaseLock:
    def test_the_sequence_is_ordered(self, study):
        order = [row["event"] for row in study.lock_events]
        assert order == [
            "DATA_CUT",
            "QUERY_BURN_DOWN_COMPLETE",
            "SOFT_LOCK",
            "HARD_LOCK",
            "UNBLINDING",
        ]
        weeks = [row["week"] for row in study.lock_events]
        assert weeks == sorted(weeks)

    def test_the_lock_waits_for_the_reconciliations(self, study):
        soft = next(row["week"] for row in study.lock_events if row["event"] == "SOFT_LOCK")
        assert soft >= max(row["completed_week"] for row in study.reconciliations)

    def test_unblinding_comes_after_the_hard_lock(self, study):
        weeks = {row["event"]: row["week"] for row in study.lock_events}
        assert weeks["UNBLINDING"] > weeks["HARD_LOCK"]

    def test_every_reconciliation_resolved_what_it_found(self, study):
        assert study.reconciliations
        for row in study.reconciliations:
            assert row["discrepancies_resolved"] == row["discrepancies_found"]
            assert row["status"] == "COMPLETE"


class TestDeviations:
    def test_classification_matches_the_configured_category(self, study, config):
        declared = {
            row.category: row.classification
            for row in config.monitoring.deviations.categories
        }
        for row in study.deviations:
            assert row["classification"] == declared[row["category"]]

    def test_the_worst_site_deviates_most_per_subject(self, study):
        subjects = collections.Counter(row["SITEID"] for row in study.subjects)
        deviations = collections.Counter(row["site_id"] for row in study.deviations)
        rates = {site: deviations[site] / count for site, count in subjects.items()}
        archetypes = {row["SITEID"]: row["ARCHETYPE"] for row in study.sites}
        assert archetypes[max(rates, key=lambda site: rates[site])] == "POOR_DATA_QUALITY"

    def test_only_eligibility_violations_exclude_from_per_protocol(self, study):
        for row in study.deviations:
            if row["excluded_from_per_protocol"] == "Y":
                assert row["category"] == "ELIGIBILITY_VIOLATION"
