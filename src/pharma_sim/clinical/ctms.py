"""Site activation and enrolment — the CTMS layer.

Enrolment is derived here rather than configured. A site cannot randomise anybody
before its green light, and its green light waits on a contract, an ethics
committee and a regulatory authority, each with its own cycle time drawn from
``sites.yaml``. A site whose contract takes five months contributes a handful of
subjects and no amount of recruitment effort recovers the accrual.

That ordering is the point. Configuring an enrolment curve directly would produce
the same *shape* while losing the thing a study manager actually wants to see:
which site cost the study its timeline, and at which milestone.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from random import Random

from pharma_sim.clinical.config import ClinicalConfig, Milestone, Site

__all__ = [
    "SiteActivation",
    "Enrolment",
    "activate_sites",
    "allocate_enrolment",
    "poisson",
]

_WEEKS_PER_MONTH = 52.1775 / 12.0


def poisson(rng: Random, mean: float) -> int:
    """Poisson draw by Knuth's method.

    Enrolment in a week is a count, not a coin flip: a busy site can randomise
    two subjects in a week and a Bernoulli draw can never produce that.
    """
    if mean <= 0.0:
        return 0
    if mean > 30.0:  # pragma: no cover - defensive, no site enrols this fast
        return max(0, int(round(rng.gauss(mean, math.sqrt(mean)))))
    limit = math.exp(-mean)
    product = rng.random()
    count = 0
    while product > limit:
        count += 1
        product *= rng.random()
    return count


@dataclass(frozen=True, slots=True)
class SiteActivation:
    """One site's activation chain and the performance it will show."""

    site_id: str
    country: str
    name: str
    principal_investigator: str
    archetype: str
    #: Milestone name to week from study start.
    milestones: dict[str, float]
    enrolment_per_month: float
    entry_lag_days: float
    query_rate_per_form: float
    deviation_rate_per_subject: float
    query_response_days: float
    #: Weeks during which this site's data entry is degraded, if it ever is.
    turnover_from: float | None = None
    turnover_to: float | None = None

    @property
    def ready_week(self) -> float:
        return self.milestones.get("SITE_READY_TO_ENROL", math.inf)

    def entry_lag_at(self, week: float, multiplier: float) -> float:
        """Entry lag at a point in the study, allowing for staff turnover."""
        if (
            self.turnover_from is not None
            and self.turnover_to is not None
            and self.turnover_from <= week <= self.turnover_to
        ):
            return self.entry_lag_days * multiplier
        return self.entry_lag_days


@dataclass(frozen=True, slots=True)
class Enrolment:
    """One subject's site and randomisation timing."""

    subject_id: str
    site_id: str
    week: float
    randomised: date


def _interval(
    milestone: Milestone, site: Site, config: ClinicalConfig, rng: Random
) -> float:
    """How long this milestone takes, from wherever the config says to look."""
    if milestone.source == "ARCHETYPE_CONTRACT":
        archetype = config.sites.archetype(site.archetype)
        assert archetype is not None
        return max(0.0, rng.gauss(archetype.contract_weeks.mean, archetype.contract_weeks.sd))
    if milestone.source in {"COUNTRY_CTA", "COUNTRY_EC"}:
        country = config.sites.country(site.country)
        assert country is not None
        window = (
            country.regulatory.cta_weeks
            if milestone.source == "COUNTRY_CTA"
            else country.regulatory.ec_weeks
        )
        return max(0.0, rng.gauss(window.mean, window.sd))
    assert milestone.weeks is not None
    return max(0.0, rng.gauss(milestone.weeks.mean, milestone.weeks.sd))


def activate_sites(config: ClinicalConfig, rng_for: callable) -> list[SiteActivation]:
    """Walk the milestone chain for every site.

    Args:
        rng_for: called with a site id, returns that site's stream. Injected so
            each site draws independently and adding a site does not shift the
            others.
    """
    activations: list[SiteActivation] = []
    turnover = config.sites.staff_turnover

    for site in config.sites.sites:
        rng = rng_for(site.site_id)
        archetype = config.sites.archetype(site.archetype)
        assert archetype is not None

        reached: dict[str, float] = {}
        for milestone in config.sites.milestones:
            predecessor = milestone.predecessor
            if predecessor is None:
                start = 0.0
            elif isinstance(predecessor, str):
                start = reached.get(predecessor, 0.0)
            else:
                # Several predecessors: this milestone waits for the last of them.
                # Green light is the case that matters — a signed contract does
                # not open a site whose ethics approval has not arrived.
                start = max((reached.get(name, 0.0) for name in predecessor), default=0.0)
            reached[milestone.milestone] = start + _interval(milestone, site, config, rng)

        turnover_from = turnover_to = None
        if rng.random() < turnover.probability_per_site:
            turnover_from = max(0.0, rng.gauss(turnover.starts_week.mean, turnover.starts_week.sd))
            turnover_to = turnover_from + max(
                1.0, rng.gauss(turnover.duration_weeks.mean, turnover.duration_weeks.sd)
            )

        activations.append(
            SiteActivation(
                site_id=site.site_id,
                country=site.country,
                name=site.name,
                principal_investigator=site.principal_investigator,
                archetype=site.archetype,
                milestones=reached,
                enrolment_per_month=max(
                    0.05, rng.gauss(archetype.enrolment_per_month.mean, archetype.enrolment_per_month.sd)
                ),
                entry_lag_days=max(
                    0.5, rng.gauss(archetype.entry_lag_days.mean, archetype.entry_lag_days.sd)
                ),
                query_rate_per_form=max(
                    0.0, rng.gauss(archetype.query_rate_per_form.mean, archetype.query_rate_per_form.sd)
                ),
                deviation_rate_per_subject=max(
                    0.0,
                    rng.gauss(
                        archetype.deviation_rate_per_subject.mean,
                        archetype.deviation_rate_per_subject.sd,
                    ),
                ),
                query_response_days=max(
                    0.5, rng.gauss(archetype.query_response_days.mean, archetype.query_response_days.sd)
                ),
                turnover_from=turnover_from,
                turnover_to=turnover_to,
            )
        )
    return activations


def allocate_enrolment(
    activations: list[SiteActivation],
    config: ClinicalConfig,
    rng: Random,
) -> list[Enrolment]:
    """Enrol subjects week by week until the target is met.

    Recruitment competes: every open site draws in the same week, and the target
    closes recruitment for everybody the moment it is reached. A site that opens
    late finds the study already full, which is why its subject count is small
    even though its rate is fine.
    """
    protocol = config.protocol
    target = sum(arm.subjects for arm in protocol.arms)
    first_in = protocol.enrolment.first_subject_in

    # The study clock starts when the first site could enrol, so week zero is
    # meaningful rather than an arbitrary offset.
    earliest = min((activation.ready_week for activation in activations), default=0.0)

    enrolled: list[Enrolment] = []
    week = 0.0
    horizon = protocol.analysis.horizon_weeks
    # Weeks are reported on the STUDY clock, whose origin is the study start, not
    # the first site opening. Reporting them from the first opening made the first
    # subject appear to be randomised in study week zero -- fifteen weeks before
    # any site was open and before any drug had been shipped, which the spine
    # checks caught as kits dispensed months before they arrived.
    while len(enrolled) < target and week <= horizon:
        for activation in activations:
            if len(enrolled) >= target:
                break
            if activation.ready_week - earliest > week:
                continue
            count = poisson(rng, activation.enrolment_per_month / _WEEKS_PER_MONTH)
            for _ in range(count):
                if len(enrolled) >= target:
                    break
                # Randomisation lands somewhere inside the week.
                offset = earliest + week + rng.random()
                enrolled.append(
                    Enrolment(
                        subject_id="",  # assigned by the caller, in enrolment order
                        site_id=activation.site_id,
                        week=offset,
                        randomised=first_in + timedelta(weeks=offset),
                    )
                )
        week += 1.0

    enrolled.sort(key=lambda item: item.week)
    return enrolled
