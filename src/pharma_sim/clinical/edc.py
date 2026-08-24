"""Case report forms, data entry and queries — the EDC layer.

The forms wrap data the study has already produced rather than inventing a
parallel version of it. A tumour assessment form carries the sum of diameters and
the response the lesion model computed, and a dosing form carries the cycle that
was actually administered. That is what keeps the case report form and the SDTM
datasets telling the same story: they are two views of one set of facts, so a
query that corrects an item is visible in both.

Two behaviours are worth naming because they are what synthetic query data
usually lacks:

* **Entry lag varies by site and over time.** A site's lag comes from its
  archetype, and one site's coordinator leaves partway through, so its lag triples
  for six weeks and then recovers. Query ageing inherits that shape.
* **Queries get re-asked.** A site that answers the wrong question gets the query
  back. Without re-query, query ageing has a single mode and looks nothing like
  the real distribution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from random import Random

from pharma_sim.clinical.config import ClinicalConfig, EditCheck, Form, ValueSource

__all__ = ["SubjectContext", "EdcOutput", "generate_edc"]


@dataclass(frozen=True, slots=True)
class VisitRecord:
    """A scheduled contact at which forms are completed."""

    visitnum: int
    visit: str
    day: date
    #: What the visit is for, which decides which forms apply.
    kind: str  # SCREENING | CYCLE | ASSESSMENT | END_OF_TREATMENT


@dataclass(frozen=True, slots=True)
class SubjectContext:
    """Everything the EDC layer needs to know about one subject.

    Assembled by the study runner from data that already exists, so the forms
    cannot drift away from the datasets.
    """

    subject_id: str
    site_id: str
    arm: str
    randomised: date
    consent_date: date
    visits: tuple[VisitRecord, ...]
    #: Per assessment visitnum: the computed tumour findings.
    assessment_facts: dict[int, dict[str, object]] = field(default_factory=dict)
    #: Per cycle visitnum: dose, whether it was adjusted, kit number.
    cycle_facts: dict[int, dict[str, object]] = field(default_factory=dict)
    end_of_treatment: date | None = None
    discontinuation_reason: str | None = None
    #: Site performance, resolved for this subject at entry time.
    entry_lag_days: float = 5.0
    query_rate_per_form: float = 0.12
    query_response_days: float = 8.0


@dataclass
class EdcOutput:
    forms: list[dict] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)
    queries: list[dict] = field(default_factory=list)
    query_events: list[dict] = field(default_factory=list)
    audit: list[dict] = field(default_factory=list)


def _round(value: float, decimals: int) -> float | int:
    return int(round(value)) if decimals == 0 else round(value, decimals)


def _weighted(rng: Random, weights: dict[str, float]) -> str:
    total = sum(weights.values())
    threshold = rng.random() * total
    running = 0.0
    for key, weight in weights.items():
        running += weight
        if threshold <= running:
            return key
    return next(reversed(weights))


def _value_for(
    item_id: str,
    source: ValueSource,
    context: SubjectContext,
    visit: VisitRecord,
    rng: Random,
) -> object | None:
    """Produce one item's value from its declared source."""
    if source.kind == "CONSTANT":
        return source.value
    if source.kind == "WEIGHTED":
        return _weighted(rng, source.weights or {})
    if source.kind == "NORMAL":
        assert source.mean is not None and source.sd is not None
        value = rng.gauss(source.mean, source.sd)
        if source.minimum is not None:
            value = max(value, source.minimum)
        if source.maximum is not None:
            value = min(value, source.maximum)
        return _round(value, source.decimals)
    if source.kind == "FROM_STUDY":
        return _from_study(source.field or "", context, visit)
    return None


def _from_study(field_name: str, context: SubjectContext, visit: VisitRecord) -> object | None:
    """Read a value the study already computed.

    Anything not available for this visit returns None, which leaves the item
    blank — and a blank item is what a REQUIRED edit check exists to find.
    """
    if field_name == "CONSENT_DATE":
        return context.consent_date.isoformat()
    if field_name == "VISIT_DATE":
        return visit.day.isoformat()
    if field_name == "ASSESSMENT_DATE":
        return visit.day.isoformat()
    if field_name == "CYCLE_DATE":
        return visit.day.isoformat()
    if field_name == "END_OF_TREATMENT_DATE":
        return None if context.end_of_treatment is None else context.end_of_treatment.isoformat()
    if field_name == "DISCONTINUATION_REASON":
        return context.discontinuation_reason

    facts = context.assessment_facts.get(visit.visitnum) or context.cycle_facts.get(
        visit.visitnum
    )
    if facts is None:
        return None
    return facts.get(field_name)


def _correct(
    value: object | None,
    rng: Random,
    bounds: tuple[float, float] | None = None,
) -> object:
    """A plausible corrected value for an item queried and changed.

    A transcription error is usually small — a digit, a day, a decimal point —
    rather than a wholesale replacement, so the correction stays near the
    original. A blank being completed is the other common case.
    """
    if value is None or value == "":
        return "Y"
    if isinstance(value, bool):
        return not value
    def clamp(candidate: float) -> float:
        if bounds is None:
            return candidate
        low, high = bounds
        return min(max(candidate, low), high)

    if isinstance(value, int):
        step = rng.choice([-10, -1, 1, 10]) if abs(value) >= 20 else rng.choice([-1, 1])
        return int(clamp(value + step))
    if isinstance(value, float):
        return round(clamp(value * rng.choice([0.94, 0.97, 1.03, 1.06])), 2)
    text = str(value)
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        # A date, corrected by a day or two — the classic source discrepancy.
        from datetime import date as _date, timedelta as _delta

        try:
            parsed = _date.fromisoformat(text)
        except ValueError:  # pragma: no cover - defensive
            return text
        return (parsed + _delta(days=rng.choice([-2, -1, 1, 2]))).isoformat()
    return text


def _applies(form: Form, visit: VisitRecord) -> bool:
    """Whether a form is completed at this visit."""
    if form.scope == "ONCE":
        return visit.kind == ("END_OF_TREATMENT" if form.form_id == "DS" else "SCREENING")
    if form.scope == "PER_VISIT":
        return visit.kind in {"SCREENING", "CYCLE"}
    if form.scope == "PER_ASSESSMENT":
        return visit.kind == "ASSESSMENT"
    if form.scope == "PER_CYCLE":
        return visit.kind == "CYCLE"
    return False


def _fires(check: EditCheck, value: object | None, values: dict[str, object]) -> bool:
    """Whether an edit check fires against a recorded value."""
    if check.kind == "REQUIRED":
        return value is None or value == ""
    if value is None or value == "":
        # Other checks have nothing to test against a blank; REQUIRED covers it.
        return False
    if check.kind == "RANGE":
        try:
            numeric = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return not (float(check.low or 0.0) <= numeric <= float(check.high or 0.0))
    if check.kind == "EXPECTED_VALUE":
        return str(value) != str(check.expected)
    if check.kind == "DATE_ORDER":
        other = values.get(check.before or "")
        if other is None:
            return False
        return str(value) < str(other)
    if check.kind == "DERIVATION_MISMATCH":
        # Checked by the caller, which is the only place that knows the nadir.
        return False
    return False


def generate_edc(
    contexts: list[SubjectContext],
    config: ClinicalConfig,
    rng_for,
    ids,
) -> EdcOutput:
    """Build every form, item value, query and audit entry for the study."""
    out = EdcOutput()
    crf = config.crf
    behaviour = crf.queries

    for context in contexts:
        rng = rng_for(context.subject_id)

        for visit in context.visits:
            for form in crf.forms:
                if not _applies(form, visit):
                    continue

                # Data entry lag: the site's own, and the form is entered that
                # many days after the visit rather than on the day.
                lag = max(0.0, rng.gauss(context.entry_lag_days, context.entry_lag_days * 0.35))
                entered = visit.day + timedelta(days=lag)

                form_instance_id = ids.next("FRM", width=7)
                values: dict[str, object] = {}
                item_rows: list[dict] = []

                for item in form.items:
                    source = crf.value_sources.get(item.item_id)
                    if source is None:
                        continue
                    value = _value_for(item.item_id, source, context, visit, rng)
                    values[item.item_id] = value
                    item_rows.append(
                        {
                            "item_data_id": ids.next("ITM", width=8),
                            "form_instance_id": form_instance_id,
                            "subject_id": context.subject_id,
                            "site_id": context.site_id,
                            "form_id": form.form_id,
                            "item_id": item.item_id,
                            "item_label": item.label,
                            "value": "" if value is None else value,
                            "unit": item.unit or "",
                            "entered_at": entered.isoformat(),
                        }
                    )

                complete = all(row["value"] != "" for row in item_rows)
                out.forms.append(
                    {
                        "form_instance_id": form_instance_id,
                        "subject_id": context.subject_id,
                        "site_id": context.site_id,
                        "form_id": form.form_id,
                        "form_name": form.name,
                        "visitnum": visit.visitnum,
                        "visit": visit.visit,
                        "visit_date": visit.day.isoformat(),
                        "entered_at": entered.isoformat(),
                        "entry_lag_days": round(lag, 2),
                        "status": "COMPLETE" if complete else "INCOMPLETE",
                        "items": len(item_rows),
                    }
                )
                out.items.extend(item_rows)

                _raise_queries(
                    out, context, form, visit, form_instance_id, values, item_rows,
                    entered, config, rng, ids,
                )

    return out


def _raise_queries(
    out: EdcOutput,
    context: SubjectContext,
    form: Form,
    visit,
    form_instance_id: str,
    values: dict[str, object],
    item_rows: list[dict],
    entered: date,
    config: ClinicalConfig,
    rng: Random,
    ids,
) -> None:
    """System queries from the edit checks, plus manual ones from review."""
    crf = config.crf
    behaviour = crf.queries

    raised: list[tuple[str, str, str, str]] = []  # (item_id, origin, severity, text)

    for check in crf.checks_for(form.form_id):
        if _fires(check, values.get(check.item_id), values):
            raised.append((check.item_id, "SYSTEM", check.severity, check.text))

    # Manual review. The site's own query rate scales the reviewer rates, which is
    # how one site ends up generating three times the queries of anybody else.
    scale = context.query_rate_per_form / 0.12
    if rng.random() < behaviour.data_management_rate_per_form * scale:
        item = crf.forms[0].items[0] if not form.items else rng.choice(form.items)
        raised.append(
            (item.item_id, "DATA_MANAGEMENT", "MINOR",
             f"Please confirm the value recorded for {item.label.lower()} against source.")
        )
    if rng.random() < behaviour.monitor_rate_per_form * scale:
        item = rng.choice(form.items) if form.items else None
        if item is not None:
            raised.append(
                (item.item_id, "MONITOR", "MINOR",
                 f"Source document for {item.label.lower()} could not be located at "
                 f"monitoring. Please provide.")
            )

    for item_id, origin, severity, text in raised:
        query_id = ids.next("QRY", width=6)
        check_id = next(
            (
                check.check_id
                for check in crf.checks_for(form.form_id)
                if check.item_id == item_id and check.text == text
            ),
            "",
        )
        notified = entered + timedelta(
            days=max(0.0, rng.gauss(behaviour.notification_days.mean, behaviour.notification_days.sd))
        )

        out.query_events.append(
            {
                "query_event_id": ids.next("QEV", width=7),
                "query_id": query_id,
                "state": "OPEN",
                "occurred_at": notified.isoformat(),
                "actor": "SYSTEM" if origin == "SYSTEM" else origin,
                "detail": text,
            }
        )

        # The site answers, taking its own time about it.
        cursor = notified
        requeries = 0
        resolution = "ANSWERED"
        while True:
            response_days = max(
                0.5, rng.gauss(context.query_response_days, context.query_response_days * 0.4)
            )
            cursor = cursor + timedelta(days=response_days)
            out.query_events.append(
                {
                    "query_event_id": ids.next("QEV", width=7),
                    "query_id": query_id,
                    "state": "ANSWERED",
                    "occurred_at": cursor.isoformat(),
                    "actor": "SITE",
                    "detail": "Response provided by site",
                }
            )
            if (
                requeries < behaviour.max_requeries
                and rng.random() < behaviour.requery_probability
            ):
                requeries += 1
                cursor = cursor + timedelta(
                    days=max(0.5, rng.gauss(behaviour.closure_days.mean, behaviour.closure_days.sd))
                )
                out.query_events.append(
                    {
                        "query_event_id": ids.next("QEV", width=7),
                        "query_id": query_id,
                        "state": "RE_QUERIED",
                        "occurred_at": cursor.isoformat(),
                        "actor": "DATA_MANAGEMENT",
                        "detail": "Response did not address the query. Re-issued.",
                    }
                )
                continue
            break

        closed = cursor + timedelta(
            days=max(0.5, rng.gauss(behaviour.closure_days.mean, behaviour.closure_days.sd))
        )
        out.query_events.append(
            {
                "query_event_id": ids.next("QEV", width=7),
                "query_id": query_id,
                "state": "CLOSED",
                "occurred_at": closed.isoformat(),
                "actor": "DATA_MANAGEMENT",
                "detail": "Query closed",
            }
        )

        # Roughly half of answered queries change the value; the rest confirm it
        # as entered. A change has to actually change something: the item now
        # holds the corrected value and the audit trail holds what it was, which
        # is the record an inspector follows back.
        changed = rng.random() < 0.52
        if changed:
            row = next((r for r in item_rows if r["item_id"] == item_id), None)
            # A correction that lands outside the item's own edit-check range
            # would immediately fire the check again, which is not what a
            # correction is.
            check_bounds = next(
                (
                    (float(c.low), float(c.high))
                    for c in crf.checks_for(form.form_id)
                    if c.item_id == item_id
                    and c.kind == "RANGE"
                    and c.low is not None
                    and c.high is not None
                ),
                None,
            )
            corrected = _correct(values.get(item_id), rng, check_bounds)
            if row is not None and corrected != row["value"]:
                out.audit.append(
                    {
                        "audit_id": ids.next("AUD", width=8),
                        "subject_id": context.subject_id,
                        "site_id": context.site_id,
                        "form_instance_id": form_instance_id,
                        "item_id": item_id,
                        "old_value": row["value"],
                        "new_value": corrected,
                        "changed_at": cursor.isoformat(),
                        "actor": "SITE",
                        "reason": "Data correction in response to query",
                        "query_id": query_id,
                    }
                )
                row["value"] = corrected
                values[item_id] = corrected
            else:
                changed = False

        out.queries.append(
            {
                "query_id": query_id,
                "subject_id": context.subject_id,
                "site_id": context.site_id,
                "form_instance_id": form_instance_id,
                "form_id": form.form_id,
                "visitnum": visit.visitnum,
                "item_id": item_id,
                "check_id": check_id,
                "origin": origin,
                "severity": severity,
                "text": text,
                "raised_at": notified.isoformat(),
                "answered_at": cursor.isoformat(),
                "closed_at": closed.isoformat(),
                "requeries": requeries,
                "age_days": (closed - notified).days,
                "resolution": "VALUE_CHANGED" if changed else "CONFIRMED_AS_ENTERED",
                "state": "CLOSED",
            }
        )
