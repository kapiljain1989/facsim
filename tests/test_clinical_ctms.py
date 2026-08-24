"""Site activation and enrolment.

The claim is that enrolment is a *consequence* of the activation chain rather
than a configured curve. These tests check the causal ordering holds: no subject
before its site is ready, green light after everything it waits on, and a site
whose contract is slow contributing fewer subjects than its rate would suggest.
"""

from __future__ import annotations

import collections
import statistics as stats
from pathlib import Path
from random import Random

import pytest

from pharma_sim.clinical.ctms import activate_sites, allocate_enrolment, poisson
from pharma_sim.clinical.loader import load_clinical_config
from pharma_sim.engine.rng import RngRegistry

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config" / "clinical"


@pytest.fixture(scope="module")
def config():
    return load_clinical_config(CONFIG_DIR)


@pytest.fixture(scope="module")
def activations(config):
    rngs = RngRegistry(42)
    return activate_sites(config, lambda site_id: rngs.child("clin", "site", site_id))


class TestPoisson:
    def test_mean_is_approximately_right(self):
        rng = Random(1)
        draws = [poisson(rng, 0.4) for _ in range(20_000)]
        assert stats.fmean(draws) == pytest.approx(0.4, abs=0.02)

    def test_can_produce_more_than_one(self):
        """A Bernoulli draw cannot, and a busy site randomises two in a week."""
        rng = Random(1)
        assert any(poisson(rng, 0.5) >= 2 for _ in range(2_000))

    def test_zero_mean_gives_nothing(self):
        assert poisson(Random(1), 0.0) == 0


class TestActivationChain:
    def test_every_site_reaches_every_milestone(self, activations, config):
        declared = {milestone.milestone for milestone in config.sites.milestones}
        for activation in activations:
            assert set(activation.milestones) == declared

    def test_green_light_waits_for_everything_it_depends_on(self, activations):
        """Contract, ethics opinion and regulatory authorisation all have to be in
        before a site opens. A signed contract does not open a site whose ethics
        approval has not arrived."""
        for activation in activations:
            milestones = activation.milestones
            blockers = (
                milestones["CONTRACT_EXECUTED"],
                milestones["EC_APPROVED"],
                milestones["REGULATORY_APPROVED"],
            )
            assert milestones["GREEN_LIGHT"] >= max(blockers)

    def test_the_chain_is_monotonic(self, activations, config):
        for activation in activations:
            for milestone in config.sites.milestones:
                predecessor = milestone.predecessor
                if predecessor is None:
                    continue
                names = [predecessor] if isinstance(predecessor, str) else predecessor
                for name in names:
                    assert (
                        activation.milestones[milestone.milestone]
                        >= activation.milestones[name]
                    )

    def test_ready_to_enrol_is_last(self, activations):
        for activation in activations:
            assert activation.ready_week == max(activation.milestones.values())

    def test_the_slow_contract_archetype_actually_has_a_slow_contract(self, activations):
        slow = [a for a in activations if a.archetype == "SLOW_CONTRACT"]
        others = [a for a in activations if a.archetype != "SLOW_CONTRACT"]
        assert slow
        slowest = max(a.milestones["CONTRACT_EXECUTED"] for a in slow)
        assert slowest > stats.fmean(a.milestones["CONTRACT_EXECUTED"] for a in others)

    def test_poor_data_quality_archetype_has_the_worst_entry_lag(self, activations):
        poor = next(a for a in activations if a.archetype == "POOR_DATA_QUALITY")
        assert poor.entry_lag_days == max(a.entry_lag_days for a in activations)

    def test_some_sites_have_staff_turnover(self, activations):
        affected = [a for a in activations if a.turnover_from is not None]
        assert affected
        for activation in affected:
            assert activation.turnover_to > activation.turnover_from

    def test_turnover_multiplies_entry_lag_only_within_its_window(self, activations, config):
        multiplier = config.sites.staff_turnover.entry_lag_multiplier
        affected = next(a for a in activations if a.turnover_from is not None)
        during = (affected.turnover_from + affected.turnover_to) / 2.0
        assert affected.entry_lag_at(during, multiplier) == pytest.approx(
            affected.entry_lag_days * multiplier
        )
        assert affected.entry_lag_at(
            affected.turnover_to + 10.0, multiplier
        ) == pytest.approx(affected.entry_lag_days)


class TestEnrolmentIsAConsequence:
    @pytest.fixture(scope="class")
    @staticmethod
    def enrolments(config, activations):
        return allocate_enrolment(activations, config, Random("enrol"))

    def test_reaches_the_protocol_target(self, enrolments, config):
        assert len(enrolments) == sum(arm.subjects for arm in config.protocol.arms)

    def test_no_subject_is_enrolled_before_its_site_opens(self, enrolments, activations):
        """The property the whole layer exists for."""
        ready = {a.site_id: a.ready_week for a in activations}
        earliest = min(ready.values())
        for enrolment in enrolments:
            # Weeks are measured from the first site opening, so the comparison is
            # against the site's opening relative to that.
            assert enrolment.week >= ready[enrolment.site_id] - earliest - 1.0

    def test_enrolment_is_ordered_in_time(self, enrolments):
        weeks = [enrolment.week for enrolment in enrolments]
        assert weeks == sorted(weeks)

    def test_every_site_that_opened_in_time_contributes(self, enrolments, activations):
        contributing = {enrolment.site_id for enrolment in enrolments}
        assert len(contributing) == len(activations)

    def test_a_late_site_contributes_fewer_than_its_rate_implies(
        self, enrolments, activations
    ):
        """A site that opens late finds the study already filling up, so its count
        is about the months it was open rather than how well it recruits."""
        counts = collections.Counter(enrolment.site_id for enrolment in enrolments)
        by_id = {a.site_id: a for a in activations}
        latest = max(activations, key=lambda a: a.ready_week)
        earliest = min(activations, key=lambda a: a.ready_week)
        # Normalise each site's count by its rate to get "months of accrual won".
        late_months = counts[latest.site_id] / latest.enrolment_per_month
        early_months = counts[earliest.site_id] / earliest.enrolment_per_month
        assert late_months < early_months
