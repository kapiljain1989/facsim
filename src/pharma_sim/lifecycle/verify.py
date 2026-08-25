"""Spine integrity checks.

A lineage claim is worth nothing unless something walks it. These checks walk
every link from a dose in a subject back to a manufactured batch and fail when
one does not resolve.

The checks are ordered by consequence rather than by table. The first one is the
one that matters: a subject who received the other arm's product invalidates
their data and, if it happened at scale, the study. Everything else is
bookkeeping by comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pharma_sim.lifecycle.config import LifecycleConfig

__all__ = ["SpineCheck", "SpineReport", "verify_spine"]


@dataclass(frozen=True, slots=True)
class SpineCheck:
    name: str
    description: str
    passed: bool
    checked: int
    failures: tuple[str, ...] = ()

    def render(self) -> str:
        mark = "ok  " if self.passed else "FAIL"
        line = f"  {mark}  {self.name:<38} {self.checked:>7,} checked"
        if not self.passed:
            shown = list(self.failures[:3])
            line += f"  {len(self.failures)} failed"
            for failure in shown:
                line += f"\n          {failure}"
            if len(self.failures) > len(shown):
                line += f"\n          ... and {len(self.failures) - len(shown)} more"
        return line


@dataclass
class SpineReport:
    checks: list[SpineCheck] = field(default_factory=list)
    linked: bool = False

    @property
    def ok(self) -> bool:
        return all(check.passed for check in self.checks)

    def render(self) -> str:
        source = "manufacturing batches" if self.linked else "STUB batches"
        lines = [f"spine integrity ({source})"]
        lines.extend(check.render() for check in self.checks)
        failed = sum(1 for check in self.checks if not check.passed)
        lines.append(
            f"  {len(self.checks) - failed}/{len(self.checks)} checks passed"
            + ("" if self.ok else "  — SPINE BROKEN")
        )
        return "\n".join(lines)


def verify_spine(study, lifecycle: LifecycleConfig) -> SpineReport:
    """Walk every link in the identity graph."""
    report = SpineReport(linked=study.spine_linked)

    kits = {row["kit_number"]: row for row in study.imp_kits}
    lots = {row["lot_id"]: row for row in study.imp_lots}
    shipments = {row["shipment_id"]: row for row in study.imp_shipments}
    arms = {row["USUBJID"]: row["ARM"] for row in study.subjects}

    # 1. The product a subject received must be the product they were randomised
    #    to. Nothing else here matters if this fails.
    failures: list[str] = []
    for row in study.dosing:
        expected = lifecycle.randomisation.role_for(arms.get(row["subject_id"], ""))
        kit = kits.get(row["kit_number"])
        if kit is None or expected is None:
            failures.append(f"{row['subject_id']} cycle {row['cycle']}: kit unresolvable")
        elif kit["role"] != expected:
            failures.append(
                f"{row['subject_id']} ({arms[row['subject_id']]}) received a "
                f"{kit['role']} kit, expected {expected}"
            )
    report.checks.append(
        SpineCheck(
            "dispensed product matches arm",
            "every dose is the treatment the subject was randomised to",
            not failures, len(study.dosing), tuple(failures),
        )
    )

    # 2. Every kit is dispensed at most once.
    seen: dict[str, str] = {}
    failures = []
    for row in study.dosing:
        previous = seen.get(row["kit_number"])
        if previous is not None:
            failures.append(
                f"{row['kit_number']} dispensed to {previous} and {row['subject_id']}"
            )
        seen[row["kit_number"]] = row["subject_id"]
    report.checks.append(
        SpineCheck(
            "each kit dispensed once",
            "a kit contains one subject's supply and cannot be reused",
            not failures, len(study.dosing), tuple(failures),
        )
    )

    # 3. Kit -> lot -> batch resolves all the way down.
    failures = []
    for kit_number, kit in kits.items():
        lot = lots.get(kit["lot_id"])
        if lot is None:
            failures.append(f"{kit_number} references unknown lot {kit['lot_id']}")
        elif lot["batch_id"] != kit["batch_id"]:
            failures.append(
                f"{kit_number} claims batch {kit['batch_id']} but its lot "
                f"was packed from {lot['batch_id']}"
            )
    report.checks.append(
        SpineCheck(
            "kit resolves to lot and batch",
            "the chain from a kit back to a manufactured batch is unbroken",
            not failures, len(kits), tuple(failures),
        )
    )

    # 4. Nothing is dispensed before it arrived.
    received: dict[str, str] = {}
    for kit_number, kit in kits.items():
        shipment = shipments.get(kit["shipment_id"])
        if shipment is not None:
            received[kit_number] = shipment["received_on"]
    failures = [
        f"{row['kit_number']} dispensed {row['dosed_on']} but received "
        f"{received[row['kit_number']]}"
        for row in study.dosing
        if row["kit_number"] in received and row["dosed_on"] < received[row["kit_number"]]
    ]
    report.checks.append(
        SpineCheck(
            "not dispensed before receipt",
            "a site cannot dispense a kit that has not arrived",
            not failures, len(study.dosing), tuple(failures),
        )
    )

    # 5. Nothing expired is dispensed.
    failures = [
        f"{row['kit_number']} dispensed {row['dosed_on']} after lot expiry "
        f"{lots[kits[row['kit_number']]['lot_id']]['expiry']}"
        for row in study.dosing
        if row["kit_number"] in kits
        and kits[row["kit_number"]]["lot_id"] in lots
        and row["dosed_on"] > lots[kits[row["kit_number"]]["lot_id"]]["expiry"]
    ]
    report.checks.append(
        SpineCheck(
            "no expired kit dispensed",
            "a kit past its lot expiry cannot be given to a subject",
            not failures, len(study.dosing), tuple(failures),
        )
    )

    # 6. A quarantined shipment is not a source of supply.
    quarantined = {
        shipment_id
        for shipment_id, shipment in shipments.items()
        if shipment["status"] == "QUARANTINED"
    }
    failures = [
        f"{row['kit_number']} came from quarantined shipment "
        f"{kits[row['kit_number']]['shipment_id']}"
        for row in study.dosing
        if row["kit_number"] in kits
        and kits[row["kit_number"]]["shipment_id"] in quarantined
    ]
    report.checks.append(
        SpineCheck(
            "no supply from a quarantined shipment",
            "a temperature excursion takes its shipment out of use",
            not failures, len(study.dosing), tuple(failures),
        )
    )

    # 7. Every exposure record reconciles to a dispensing record.
    dosed = {(row["subject_id"], row["cycle"]) for row in study.dosing}
    failures = [
        f"EX for {row['USUBJID']} cycle {row['EXSEQ']} has no dispensing record"
        for row in study.ex
        if (row["USUBJID"], row["EXSEQ"]) not in dosed
    ]
    report.checks.append(
        SpineCheck(
            "SDTM EX reconciles to accountability",
            "every exposure record is backed by a kit that was handed over",
            not failures, len(study.ex), tuple(failures),
        )
    )

    # 8. A lot cannot ship more kits than it packed.
    shipped: dict[str, int] = {}
    for kit in kits.values():
        shipped[kit["lot_id"]] = shipped.get(kit["lot_id"], 0) + 1
    failures = [
        f"lot {lot_id} shipped {count} kits but packed {lots[lot_id]['kits']}"
        for lot_id, count in shipped.items()
        if lot_id in lots and count > lots[lot_id]["kits"]
    ]
    report.checks.append(
        SpineCheck(
            "lot not over-shipped",
            "a lot cannot supply more kits than were packed from it",
            not failures, len(shipped), tuple(failures),
        )
    )

    # 9. Expiry provenance is consistent. A lot dated from a fitted shelf life
    #    and one dated from a constant are different claims, and a dataset that
    #    mixes them without saying so is asserting the stronger one for both.
    sources = {row.get("expiry_source") for row in study.imp_lots}
    failures = (
        (f"lots carry mixed expiry provenance: {sorted(s for s in sources if s)}",)
        if len(sources) > 1
        else ()
    )
    report.checks.append(
        SpineCheck(
            "expiry provenance consistent",
            "every lot's expiry comes from the same kind of source",
            not failures, len(study.imp_lots), failures,
        )
    )

    # 10. Identifiers are unique. A reissued shipment id attributes kits to the
    #    wrong shipment and gives them the wrong arrival date, which then makes
    #    every date-based check meaningless rather than failing loudly.
    for label, rows, key in (
        ("shipment", study.imp_shipments, "shipment_id"),
        ("lot", study.imp_lots, "lot_id"),
        ("kit", study.imp_kits, "kit_number"),
    ):
        seen_ids: set[str] = set()
        duplicates: list[str] = []
        for row in rows:
            if row[key] in seen_ids:
                duplicates.append(f"{label} {row[key]} appears more than once")
            seen_ids.add(row[key])
        report.checks.append(
            SpineCheck(
                f"{label} identifiers unique",
                f"no {label} id is reissued",
                not duplicates, len(rows), tuple(duplicates),
            )
        )

    # 11. Kit numbers must carry no information about treatment. If sorting the
    #    kit list by number segregated the arms, anybody holding it could
    #    reconstruct the allocation.
    ordered = sorted(kits.values(), key=lambda row: row["kit_number"])
    roles = [row["role"] for row in ordered]
    runs = 1 + sum(1 for a, b in zip(roles, roles[1:]) if a != b)
    # Random interleaving of two roughly equal groups gives about n/2 runs;
    # perfect segregation gives 2. Anything below a quarter of expected is a leak.
    expected_runs = len(roles) / 2.0
    leaks = runs < expected_runs * 0.25 if len(roles) > 20 else False
    report.checks.append(
        SpineCheck(
            "kit number reveals nothing",
            "sorting kits by number must not segregate the treatments",
            not leaks, len(roles),
            () if not leaks else (
                f"{runs} runs across {len(roles)} kits, expected about "
                f"{expected_runs:.0f} — numbering leaks the allocation",
            ),
        )
    )

    return report
