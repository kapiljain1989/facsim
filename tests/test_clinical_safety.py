"""Adverse events, CTCAE grading, and the exposure they produce.

The claim under test is that the grade drives the dose. A Grade 3
non-haematological event interrupts dosing and the subject resumes a level down,
so the arm with more Grade 3 events receives less drug — and relative dose
intensity is therefore a consequence of the safety profile rather than a separate
setting. That is the first class here.

The rest defends three things a safety reviewer checks immediately: that
seriousness is not severity, that attribution is not arm, and that an
adverse-event table cannot kill anybody.
"""

from __future__ import annotations

import collections
import statistics as stats
from pathlib import Path
from random import Random

import pytest

from pharma_sim.clinical.loader import load_clinical_config
from pharma_sim.clinical.safety import apply_dose_modifications, generate_events
from pharma_sim.clinical.study import run_study
from pharma_sim.config.errors import ConfigError
from pharma_sim.engine.ids import IdFactory
from pharma_sim.lifecycle.config import load_lifecycle_config

CLINICAL = Path(__file__).resolve().parents[1] / "config" / "clinical"
LIFECYCLE = Path(__file__).resolve().parents[1] / "config" / "lifecycle"


@pytest.fixture(scope="module")
def config():
    return load_clinical_config(CLINICAL)


@pytest.fixture(scope="module")
def cohort(config):
    """A large cohort per arm, so incidence and grade mixes are measurable."""
    ids = IdFactory()
    cycle_weeks = config.protocol.cycle_length_days / 7.0
    result: dict[str, list] = {}
    for arm in ("ARM-A", "ARM-B"):
        rows = []
        for index in range(300):
            subject = f"{arm}-{index:04d}"
            events = generate_events(
                subject, arm, 40.0, config, Random(subject), ids
            )
            exposure = apply_dose_modifications(
                subject, events, 13, cycle_weeks, config.dose_modification
            )
            rows.append((events, exposure))
        result[arm] = rows
    return result


@pytest.fixture(scope="module")
def study(config):
    return run_study(config, load_lifecycle_config(LIFECYCLE), seed=42)


class TestTheGradeDrivesTheDose:
    """The property the whole module exists for."""

    def test_the_arm_with_more_severe_toxicity_receives_less_drug(self, cohort, config):
        starting = config.dose_modification.starting_dose_mg
        cycle_days = float(config.protocol.cycle_length_days)
        summary = {}
        for arm, rows in cohort.items():
            severe = sum(1 for events, _ in rows for event in events if event.grade >= 3)
            intensity = stats.fmean(
                exposure.relative_dose_intensity(starting, cycle_days)
                for _, exposure in rows
            )
            summary[arm] = (severe / len(rows), intensity)

        active_severe, active_rdi = summary["ARM-A"]
        control_severe, control_rdi = summary["ARM-B"]
        assert active_severe > control_severe, "the active arm should be more toxic"
        assert active_rdi < control_rdi, (
            "relative dose intensity has to follow the toxicity, or the grades are "
            "not driving the dose at all"
        )

    def test_a_grade_three_non_haematological_event_reduces_the_dose(self, config):
        """Searches for a qualifying subject rather than skipping if the first one
        happens not to have such an event -- a conditional skip here would let the
        rule stop firing entirely without the suite noticing."""
        ids = IdFactory()
        for index in range(200):
            subject = f"S-{index}"
            events = generate_events(
                subject, "ARM-A", 40.0, config, Random(subject), ids
            )
            severe = [
                event for event in events
                if event.grade >= 3 and event.category == "NON_HAEMATOLOGICAL"
                and event.special_interest is None
            ]
            if not severe:
                continue
            exposure = apply_dose_modifications(
                subject, events, 13, 3.0, config.dose_modification
            )
            assert exposure.modifications, subject
            assert any(
                modification.action in {
                    "INTERRUPT_AND_REDUCE", "PERMANENT_DISCONTINUATION"
                }
                for modification in exposure.modifications
            ), subject
            return
        pytest.fail("no subject in 200 had a severe non-haematological event")

    def test_a_grade_three_haematological_event_does_not_reduce_the_dose(self, config):
        """Marrow suppression comes from the chemotherapy backbone. Reducing the
        investigational product for it would be reducing the wrong drug."""
        rules = {rule.rule_id: rule for rule in config.dose_modification.rules}
        haematological = [
            rule for rule in rules.values()
            if rule.trigger.category == "HAEMATOLOGICAL"
            and rule.trigger.grade_at_least == 3
        ]
        assert haematological
        assert all(rule.action == "INTERRUPT" for rule in haematological)

    def test_the_dose_never_goes_back_up(self, cohort):
        for rows in cohort.values():
            for _, exposure in rows:
                doses = [exposure.dose_by_cycle[c] for c in sorted(exposure.dose_by_cycle)]
                assert doses == sorted(doses, reverse=True), doses

    def test_the_dose_stays_on_a_declared_level(self, cohort, config):
        allowed = set(config.dose_modification.dose_levels_mg)
        for rows in cohort.values():
            for _, exposure in rows:
                assert set(exposure.dose_by_cycle.values()) <= allowed

    def test_an_interruption_lowers_intensity_without_lowering_the_dose(self, config):
        """Both mechanisms have to count, or a subject interrupted for weeks looks
        identical to one who took every tablet."""
        starting = config.dose_modification.starting_dose_mg
        from pharma_sim.clinical.safety import Exposure

        full = Exposure(subject_id="A", dose_by_cycle={1: starting}, days_by_cycle={1: 21.0})
        interrupted = Exposure(
            subject_id="B", dose_by_cycle={1: starting}, days_by_cycle={1: 10.0}
        )
        assert full.relative_dose_intensity(starting, 21.0) == pytest.approx(1.0)
        assert interrupted.relative_dose_intensity(starting, 21.0) < 1.0


class TestSeriousnessIsNotSeverity:
    def test_some_low_grade_events_are_serious(self, cohort):
        """A Grade 2 event requiring hospitalisation is serious. Tying seriousness
        to grade would be using the wrong definition."""
        low_and_serious = [
            event
            for rows in cohort.values()
            for events, _ in rows
            for event in events
            if event.serious and event.grade <= 2
        ]
        assert low_and_serious

    def test_some_severe_events_are_not_serious(self, cohort):
        severe_not_serious = [
            event
            for rows in cohort.values()
            for events, _ in rows
            for event in events
            if not event.serious and event.grade >= 3
        ]
        assert severe_not_serious

    def test_seriousness_still_rises_with_grade(self, cohort):
        by_grade: dict[int, list[bool]] = collections.defaultdict(list)
        for rows in cohort.values():
            for events, _ in rows:
                for event in events:
                    by_grade[event.grade].append(event.serious)
        rates = {
            grade: sum(values) / len(values)
            for grade, values in by_grade.items()
            if len(values) > 30
        }
        ordered = [rates[grade] for grade in sorted(rates)]
        assert ordered == sorted(ordered)

    def test_every_serious_event_names_a_criterion(self, cohort):
        for rows in cohort.values():
            for events, _ in rows:
                for event in events:
                    assert bool(event.seriousness_criterion) == event.serious


class TestAttributionIsNotArm:
    def _incidence(self, cohort, term):
        return {
            arm: sum(
                1 for events, _ in rows if any(event.pt == term for event in events)
            ) / len(rows)
            for arm, rows in cohort.items()
        }

    @pytest.mark.parametrize("term", ["Anaemia", "Neutrophil count decreased", "Alopecia"])
    def test_backbone_events_are_similar_in_both_arms(self, cohort, term):
        """Both arms receive carboplatin and pemetrexed. A profile where these
        differ has not understood the design."""
        rates = self._incidence(cohort, term)
        assert rates["ARM-A"] == pytest.approx(rates["ARM-B"], abs=0.10)

    @pytest.mark.parametrize(
        "term", ["Diarrhoea", "Alanine aminotransferase increased"]
    )
    def test_product_events_are_markedly_higher_on_the_active_arm(self, cohort, term):
        rates = self._incidence(cohort, term)
        assert rates["ARM-A"] > 2.0 * rates["ARM-B"]

    def test_relatedness_follows_attribution(self, cohort):
        """An event the backbone is known to cause is more often judged unrelated
        to the study drug."""
        by_attribution: dict[str, list[bool]] = collections.defaultdict(list)
        for rows in cohort.values():
            for events, _ in rows:
                for event in events:
                    by_attribution[event.attribution].append(event.related)
        rates = {
            key: sum(values) / len(values) for key, values in by_attribution.items()
        }
        assert rates["INVESTIGATIONAL_PRODUCT"] > rates["BOTH"] > rates["CHEMOTHERAPY"]


class TestGradeFiveIsNeverDrawn:
    def test_no_event_is_fatal(self, cohort):
        """Death belongs to the survival model. An adverse-event table that could
        kill subjects would double count mortality and break the relationship
        between death and disease burden."""
        for rows in cohort.values():
            for events, _ in rows:
                assert all(event.grade <= 4 for event in events)

    def test_no_event_claims_death_as_its_seriousness_criterion(self, cohort):
        for rows in cohort.values():
            for events, _ in rows:
                assert all(
                    event.seriousness_criterion != "DEATH" for event in events
                )

    def test_the_config_rejects_a_grade_five_weight(self, tmp_path):
        import shutil

        target = tmp_path / "clinical"
        shutil.copytree(CLINICAL, target)
        safety = target / "safety.yaml"
        safety.write_text(
            safety.read_text().replace(
                "grade_weights: {1: 0.44, 2: 0.33, 3: 0.21, 4: 0.02}",
                "grade_weights: {1: 0.44, 2: 0.33, 3: 0.21, 5: 0.02}",
                1,
            )
        )
        with pytest.raises(ConfigError) as excinfo:
            load_clinical_config(target)
        assert "Grade 5" in str(excinfo.value)


class TestExpeditedReporting:
    def test_a_susar_is_serious_related_and_unexpected(self, cohort):
        for rows in cohort.values():
            for events, _ in rows:
                for event in events:
                    if event.susar:
                        assert event.serious and event.related and event.unexpected

    def test_susars_occur(self, cohort):
        assert any(
            event.susar
            for rows in cohort.values()
            for events, _ in rows
            for event in events
        )

    def test_only_a_susar_has_a_reporting_clock(self, cohort):
        for rows in cohort.values():
            for events, _ in rows:
                for event in events:
                    assert (event.reporting_due_week is not None) == event.susar

    def test_awareness_precedes_notification_precedes_the_deadline(self, cohort):
        for rows in cohort.values():
            for events, _ in rows:
                for event in events:
                    assert event.onset_week <= event.site_awareness_week
                    assert event.site_awareness_week <= event.sponsor_notified_week
                    if event.reporting_due_week is not None:
                        assert event.sponsor_notified_week < event.reporting_due_week


class TestInTheEmittedStudy:
    def test_the_ae_domain_is_populated_and_shaped(self, study):
        assert study.ae
        assert {row["DOMAIN"] for row in study.ae} == {"AE"}
        assert all(1 <= row["AETOXGR"] <= 4 for row in study.ae)

    def test_action_taken_matches_what_happened_to_the_dose(self, study):
        by_ae = {row["ae_id"]: row for row in study.dose_modifications}
        expected = {
            "PERMANENT_DISCONTINUATION": "DRUG WITHDRAWN",
            "INTERRUPT_AND_REDUCE": "DOSE REDUCED",
            "INTERRUPT": "DRUG INTERRUPTED",
        }
        seen = set()
        for row in study.dose_modifications:
            seen.add(row["action"])
        assert seen, "no dose modifications occurred"
        # Every reported action has a modification behind it, and vice versa.
        changed = [row for row in study.ae if row["AEACN"] != "DOSE NOT CHANGED"]
        assert changed
        for row in changed:
            assert row["AEACN"] in expected.values()

    def test_every_dose_modification_cites_a_real_event(self, study):
        ids = {row["AEPTCD"] for row in study.ae}
        assert ids
        subjects = {row["USUBJID"] for row in study.ae}
        assert {row["subject_id"] for row in study.dose_modifications} <= subjects

    def test_toxicity_discontinuation_reaches_the_disposition_form(self, study):
        stopped = {
            row["subject_id"] for row in study.exposure
            if row["discontinued_for_toxicity"] == "Y"
        }
        assert stopped
        reasons = {
            row["value"] for row in study.item_data if row["item_id"] == "DSREAS"
        }
        assert "ADVERSE EVENT" in reasons

    def test_exposure_is_reported_for_every_subject(self, study):
        assert {row["subject_id"] for row in study.exposure} == {
            row["USUBJID"] for row in study.subjects
        }
        for row in study.exposure:
            assert 0.0 < row["relative_dose_intensity"] <= 1.0

    def test_relative_dose_intensity_is_lower_on_the_active_arm(self, study):
        by_arm: dict[str, list[float]] = collections.defaultdict(list)
        for row in study.exposure:
            by_arm[row["arm"]].append(row["relative_dose_intensity"])
        assert stats.fmean(by_arm["ARM-A"]) < stats.fmean(by_arm["ARM-B"])


class TestExposurePredictsOutcome:
    """The relationship a pharmacometrician looks for first.

    Before the exposure-response coupling, the lesion trajectory was drawn
    independently of the dose received, so a subject who spent half the study
    interrupted responded exactly as well as one who took every tablet and
    relative dose intensity predicted nothing.
    """

    @pytest.fixture(scope="class")
    @staticmethod
    def outcomes(config):
        """PFS events paired with the exposure that produced them, over several
        seeds so the quartiles have something in them."""
        lifecycle = load_lifecycle_config(LIFECYCLE)
        paired: list[tuple[str, float, float]] = []
        for seed in (1, 2, 3):
            study = run_study(config, lifecycle, seed=seed)
            intensity = {
                row["subject_id"]: row["relative_dose_intensity"]
                for row in study.exposure
            }
            arms = {row["USUBJID"]: row["ARM"] for row in study.subjects}
            for row in study.adtte:
                if row["PARAMCD"] != "PFS" or row["EVAL"] != "BICR":
                    continue
                if row["CNSR"] != 0:  # events only; censored times are not outcomes
                    continue
                paired.append((arms[row["USUBJID"]], intensity[row["USUBJID"]], row["AVAL"]))
        return paired

    def test_there_is_a_spread_of_exposure_to_correlate_against(self, outcomes):
        values = [intensity for _, intensity, _ in outcomes]
        assert min(values) < 0.8 < max(values)

    @pytest.mark.parametrize("arm", ["ARM-A", "ARM-B"])
    def test_lower_exposure_gives_shorter_progression_free_survival(self, outcomes, arm):
        rows = sorted((i, a) for armed, i, a in outcomes if armed == arm)
        assert len(rows) >= 40, f"only {len(rows)} events in {arm}"
        quartile = len(rows) // 4
        low = [aval for _, aval in rows[:quartile]]
        high = [aval for _, aval in rows[-quartile:]]
        assert stats.median(low) < stats.median(high), (
            "the lowest exposure quartile should progress sooner than the highest"
        )

    @pytest.mark.parametrize("arm", ["ARM-A", "ARM-B"])
    def test_the_rank_correlation_is_positive(self, outcomes, arm):
        rows = [(i, a) for armed, i, a in outcomes if armed == arm]
        assert len(rows) >= 40
        count = len(rows)
        by_intensity = sorted(range(count), key=lambda index: rows[index][0])
        by_survival = sorted(range(count), key=lambda index: rows[index][1])
        rank_i = {value: position for position, value in enumerate(by_intensity)}
        rank_s = {value: position for position, value in enumerate(by_survival)}
        squared = sum((rank_i[index] - rank_s[index]) ** 2 for index in range(count))
        rho = 1.0 - 6.0 * squared / (count * (count * count - 1))
        assert rho > 0.1, f"rank correlation {rho:+.3f} is too weak to be a relationship"

    def test_stopping_for_toxicity_shortens_survival(self, config):
        """A subject taken off treatment for toxicity loses the treatment effect,
        so their disease progresses on the untreated trajectory."""
        lifecycle = load_lifecycle_config(LIFECYCLE)
        study = run_study(config, lifecycle, seed=5)
        stopped = {
            row["subject_id"] for row in study.exposure
            if row["discontinued_for_toxicity"] == "Y"
        }
        assert stopped, "no subject stopped for toxicity at this seed"
        intensity = {
            row["subject_id"]: row["relative_dose_intensity"] for row in study.exposure
        }
        # Their exposure must be materially below the rest.
        others = [
            value for subject, value in intensity.items() if subject not in stopped
        ]
        theirs = [intensity[subject] for subject in stopped]
        assert stats.fmean(theirs) < stats.fmean(others)
