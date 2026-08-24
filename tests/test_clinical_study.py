"""Study NVR-101-201 as emitted in SDTM and ADaM shape.

The end-to-end version of spine invariant 7: response is recomputed from the
exported ``TR`` rows alone, with no access to the objects that produced them. If
that holds on the emitted records then the dataset survives a statistician
recomputing it, which is the only test that really matters here.
"""

from __future__ import annotations

import collections
from pathlib import Path

import pytest

from pharma_sim.clinical.loader import load_clinical_config
from pharma_sim.clinical.lesion import rules_from_config
from pharma_sim.clinical.recist import (
    LesionMeasurement,
    Timepoint,
    evaluate_course,
    sum_of_diameters,
)
from pharma_sim.clinical.study import run_study

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "clinical"


@pytest.fixture(scope="module")
def config():
    return load_clinical_config(CONFIG_DIR)


@pytest.fixture(scope="module")
def study(config):
    return run_study(config, seed=42)


class TestSdtmShape:
    def test_every_domain_is_populated(self, study):
        assert study.subjects and study.tu and study.tr and study.rs and study.adtte

    def test_subject_count_matches_the_protocol(self, study, config):
        assert len(study.subjects) == sum(arm.subjects for arm in config.protocol.arms)

    def test_domain_variable_is_set(self, study):
        assert {row["DOMAIN"] for row in study.tu} == {"TU"}
        assert {row["DOMAIN"] for row in study.tr} == {"TR"}
        assert {row["DOMAIN"] for row in study.rs} == {"RS"}

    def test_nodes_are_reported_by_short_axis_and_masses_by_longest_diameter(self, study):
        """A node's short axis and a mass's longest diameter are different
        measurements, and SDTM distinguishes them by test code."""
        assert {row["TRTESTCD"] for row in study.tr} <= {"LDIAM", "SAXIS"}
        by_lesion = {row["TRLNKID"]: row["TRTESTCD"] for row in study.tr}
        assert "SAXIS" in by_lesion.values()
        assert "LDIAM" in by_lesion.values()

    def test_a_lesion_keeps_one_test_code_for_the_whole_study(self, study):
        """A lesion cannot switch from being measured one way to the other."""
        seen: dict[str, set[str]] = collections.defaultdict(set)
        for row in study.tr:
            seen[row["TRLNKID"]].add(row["TRTESTCD"])
        assert all(len(codes) == 1 for codes in seen.values())

    def test_unmeasurable_results_carry_their_reason(self, study):
        """``TRORRES`` holds what was written down, ``TRSTRESN`` the number it
        standardises to. A default of 5 mm has to be traceable to the reason."""
        defaults = [row for row in study.tr if row["TRORRES"] == "TOO SMALL TO MEASURE"]
        assert defaults, "expected some lesions to shrink below measurability"
        assert all(row["TRSTRESN"] == 5.0 for row in defaults)


class TestReferentialIntegrity:
    def test_every_measurement_belongs_to_an_identified_lesion(self, study):
        identified = {
            (row["USUBJID"], row["TUEVALID"], row["TULNKID"]) for row in study.tu
        }
        orphans = [
            row
            for row in study.tr
            if (row["USUBJID"], row["TREVALID"], row["TRLNKID"]) not in identified
        ]
        assert orphans == []

    def test_every_record_belongs_to_a_randomised_subject(self, study):
        subjects = {row["USUBJID"] for row in study.subjects}
        for domain in (study.tu, study.tr, study.rs, study.adtte):
            assert {row["USUBJID"] for row in domain} <= subjects

    def test_one_survival_record_per_subject_per_evaluator(self, study, config):
        evaluators = len(config.tumour.measurement.readers)
        counts = collections.Counter(row["USUBJID"] for row in study.adtte)
        assert set(counts.values()) == {evaluators}

    def test_censoring_follows_the_adam_convention(self, study):
        """CNSR is 0 for an event and 1 for a censored observation, which is the
        opposite way round to how most people write it."""
        for row in study.adtte:
            if row["CNSR"] == 0:
                assert row["EVNTDESC"] in {"PROGRESSION", "DEATH"}
            else:
                assert row["EVNTDESC"] not in {"PROGRESSION", "DEATH"}

    def test_ground_truth_is_not_in_the_operational_records(self, study):
        """The growth parameters a subject was generated from must not leak into
        anything a model would train on."""
        leaked = {"sensitive_fraction", "shrinkage_rate_per_week", "death_week"}
        for domain in (study.subjects, study.tu, study.tr, study.rs, study.adtte):
            for row in domain:
                assert leaked.isdisjoint(row.keys())
        assert study.truth and leaked <= set(study.truth[0].keys())


class TestResponseIsRecomputableFromTheExport:
    """Spine invariant 7, on the emitted records."""

    @staticmethod
    def _rebuild(tr_rows, rs_rows):
        """Reconstruct timepoints from TR rows plus the categorical RS fields."""
        by_visit: dict[int, list[dict]] = collections.defaultdict(list)
        for row in tr_rows:
            by_visit[row["VISITNUM"]].append(row)
        response_rows = {
            row["VISITNUM"]: row for row in rs_rows if row["RSTESTCD"] == "OVRLRESP"
        }

        timepoints: list[tuple[int, Timepoint]] = []
        for visitnum in sorted(by_visit):
            lesions = tuple(
                LesionMeasurement(
                    lesion_id=row["TRLNKID"],
                    organ=row["TRLOC"],
                    nodal=row["TRTESTCD"] == "SAXIS",
                    diameter_mm=0.0 if row["TRSTRESN"] is None else float(row["TRSTRESN"]),
                    too_small_to_measure=row["TRORRES"] == "TOO SMALL TO MEASURE",
                    absent=row["TRORRES"] == "0",
                    not_evaluable=row["TRORRES"] == "NOT EVALUABLE",
                )
                for row in sorted(by_visit[visitnum], key=lambda row: row["TRLNKID"])
            )
            source = response_rows.get(visitnum)
            timepoints.append(
                (
                    visitnum,
                    Timepoint(
                        week=0.0 if visitnum == 1 else float(visitnum),
                        target=lesions,
                        non_target=(source or {}).get("NONTRESP") or "ABSENT",
                        new_lesion=bool(source and source.get("NEWLESION") == "Y"),
                    ),
                )
            )
        return timepoints

    def test_every_response_recomputes_from_the_tr_rows(self, study, config):
        rules = rules_from_config(config.tumour)

        tr_index: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
        for row in study.tr:
            tr_index[(row["USUBJID"], row["TREVALID"])].append(row)
        rs_index: dict[tuple[str, str], list[dict]] = collections.defaultdict(list)
        for row in study.rs:
            rs_index[(row["USUBJID"], row["RSEVALID"])].append(row)

        checked = 0
        for key, tr_rows in tr_index.items():
            rs_rows = rs_index[key]
            reported = [
                row["RSSTRESC"]
                for row in sorted(
                    (r for r in rs_rows if r["RSTESTCD"] == "OVRLRESP"),
                    key=lambda row: row["VISITNUM"],
                )
            ]
            rebuilt = self._rebuild(tr_rows, rs_rows)
            if len(rebuilt) < 2:
                continue
            del reported

            # The sum every response was derived from has to be recoverable by
            # adding up that visit's TR rows. Matched on VISITNUM, because a
            # missed assessment writes an RS row and no TR rows at all, and
            # zipping the two lists would silently pair the wrong visits.
            visit_sums = {
                row["VISITNUM"]: row["SUMDIAM"]
                for row in rs_rows
                if row["RSTESTCD"] == "OVRLRESP" and row["SUMDIAM"] is not None
            }
            for visitnum, timepoint in rebuilt[1:]:
                if visitnum not in visit_sums:
                    continue
                assert sum_of_diameters(timepoint.target) == pytest.approx(
                    visit_sums[visitnum], abs=0.01
                )

            # And the response itself has to come back out of those rows.
            baseline = rebuilt[0][1]
            course = evaluate_course([tp for _, tp in rebuilt[1:]], baseline, rules, key[1])
            reported_by_visit = {
                row["VISITNUM"]: row["RSSTRESC"]
                for row in rs_rows
                if row["RSTESTCD"] == "OVRLRESP"
            }
            for (visitnum, _), assessment in zip(rebuilt[1:], course):
                expected = reported_by_visit.get(visitnum)
                if expected is None:
                    continue
                assert assessment.response == expected, (
                    f"{key}: visit {visitnum} recomputed as {assessment.response}, "
                    f"exported as {expected}"
                )
            checked += 1
        assert checked > 100

    def test_progression_below_baseline_appears_in_the_data(self, study):
        """The case that separates a correct implementation from a plausible one.

        A subject can progress while their tumour sum is far below where it
        started, because progression is judged against the nadir. If this never
        happened in the emitted data, the nadir was not being carried.
        """
        below_baseline_pd = [
            row
            for row in study.rs
            if row["RSTESTCD"] == "OVRLRESP"
            and row["RSSTRESC"] == "PD"
            and row["PCHGBASE"] is not None
            and row["PCHGBASE"] < -30.0
        ]
        assert below_baseline_pd, "no progression recorded below baseline"

        # Progression driven by the target lesions has to satisfy both RECIST
        # thresholds against the nadir. Progression driven by a new lesion or by
        # non-target disease does not -- it is progression whatever the target
        # sum is doing, and those rows can legitimately show the sum still
        # falling. Separating the two is the point.
        target_driven = [
            row
            for row in below_baseline_pd
            if row["NEWLESION"] == "N" and row["NONTRESP"] != "PD"
        ]
        assert target_driven, "no target-driven progression below baseline"
        for row in target_driven:
            assert row["PCHGNADIR"] is not None and row["PCHGNADIR"] >= 20.0
            assert row["SUMDIAM"] - row["NADIR"] >= 5.0

        other = [row for row in below_baseline_pd if row not in target_driven]
        assert other, "expected some progression from new or non-target disease"


class TestEvaluatorsDisagree:
    def test_readers_follow_different_lesions_for_some_subjects(self, study):
        targets: dict[str, dict[str, set[str]]] = collections.defaultdict(
            lambda: collections.defaultdict(set)
        )
        for row in study.tu:
            if row["TUORRES"] == "TARGET":
                targets[row["USUBJID"]][row["TUEVALID"]].add(row["TULNKID"])
        diverged = [
            subject
            for subject, per_reader in targets.items()
            if len(per_reader) == 2 and len(set(map(frozenset, per_reader.values()))) > 1
        ]
        assert diverged, "the two readers never selected different target lesions"

    def test_response_disagrees_at_some_visits(self, study):
        paired: dict[tuple[str, int], dict[str, str]] = collections.defaultdict(dict)
        for row in study.rs:
            if row["RSTESTCD"] == "OVRLRESP":
                paired[(row["USUBJID"], row["VISITNUM"])][row["RSEVALID"]] = row["RSSTRESC"]
        both = [values for values in paired.values() if len(values) == 2]
        disagreeing = [v for v in both if len(set(v.values())) > 1]
        assert both
        # Some disagreement, but not so much that the readers are unrelated.
        fraction = len(disagreeing) / len(both)
        assert 0.01 < fraction < 0.25, f"discordance {fraction:.1%} is implausible"

    def test_survival_can_differ_between_evaluators(self, study):
        by_subject: dict[str, dict[str, float]] = collections.defaultdict(dict)
        for row in study.adtte:
            by_subject[row["USUBJID"]][row["EVAL"]] = row["AVAL"]
        differing = [
            values
            for values in by_subject.values()
            if len(values) == 2 and len(set(values.values())) > 1
        ]
        assert differing, "investigator and central review always agreed on PFS"


class TestReproducibility:
    def test_two_runs_at_one_seed_agree(self, config):
        first = run_study(config, seed=7)
        second = run_study(config, seed=7)
        assert first.rs == second.rs
        assert first.adtte == second.adtte

    def test_a_different_seed_changes_the_study(self, config):
        assert run_study(config, seed=11).adtte != run_study(config, seed=12).adtte
