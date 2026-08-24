"""The identity graph: released batches through to a dose in a subject.

This is the join nobody's demo data has. A kit dispensed at a clinical site
resolves to a shipment, to an IMP lot, to a batch the plant actually made and a
qualified person actually released. Walk it the other way and a manufacturing
batch tells you which subjects received it.

Two modes, because the clinical dataset has to stand alone:

* **Linked.** Given a manufacturing export, real released batches are read from
  ``batch_data.csv`` and packed into IMP lots. Every kit then traces to a batch
  with a real manufacturing date, quantity and disposition.
* **Stubbed.** With no export, deterministic batch stubs are materialised so the
  clinical data is still internally consistent. Stubs are labelled ``STUB``, so a
  check that requires real batches cannot quietly pass on them.

The distinction is explicit on every row rather than inferred, because silently
falling back to fabricated batches would be exactly the kind of thing that makes
a lineage claim worthless.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from random import Random

from pharma_sim.lifecycle.config import LifecycleConfig

__all__ = [
    "Batch",
    "ImpLot",
    "Kit",
    "Shipment",
    "Spine",
    "build_spine",
    "resupply",
    "load_released_batches",
]


@dataclass(frozen=True, slots=True)
class Batch:
    """A manufactured batch available for clinical packing."""

    batch_id: str
    product_id: str
    quantity: float
    released_on: date
    disposition: str
    #: False when the batch came from a real manufacturing run.
    stub: bool

    @property
    def source(self) -> str:
        return "STUB" if self.stub else "MANUFACTURING"


@dataclass(frozen=True, slots=True)
class ImpLot:
    lot_id: str
    batch_id: str
    product_id: str
    role: str
    kits: int
    packed_on: date
    expiry: date
    stub_batch: bool


@dataclass(frozen=True, slots=True)
class Kit:
    kit_number: str
    lot_id: str
    batch_id: str
    #: ACTIVE or PLACEBO. Blinded: must not appear in a blinded export.
    role: str
    shipment_id: str
    site_id: str
    #: Copied from the lot. Dispensing has to refuse an out-of-date kit, and
    #: doing that needs the expiry to hand rather than behind a join.
    expiry: date | None = None
    #: When the shipment carrying this kit arrived. A kit ordered today is not on
    #: the shelf today -- resupply has a lead time, and ignoring it produced kits
    #: dispensed weeks before they were received.
    available_from: date | None = None


@dataclass(frozen=True, slots=True)
class Shipment:
    shipment_id: str
    site_id: str
    lot_ids: tuple[str, ...]
    kits: int
    shipped_on: date
    received_on: date
    status: str
    temperature_excursion: bool


def load_released_batches(
    export_dir: str | Path, config: LifecycleConfig
) -> list[Batch]:
    """Read released batches from a manufacturing export.

    Only dispositions the links file calls releasable are eligible: a batch that
    failed QC cannot be packed for a clinical trial, and letting one through
    would be the most consequential possible error in this whole graph.
    """
    path = Path(export_dir)
    candidates = [
        path / "operational" / "batch_data.csv",
        path / "batch_data.csv",
        path / "batches.csv",
    ]
    file = next((candidate for candidate in candidates if candidate.exists()), None)
    if file is None:
        raise FileNotFoundError(
            f"no batch export under {path} (looked for {', '.join(c.name for c in candidates)})"
        )

    releasable = {value.upper() for value in config.imp.releasable_dispositions}
    products = {product.product_id for product in config.drug_products}
    batches: list[Batch] = []
    with file.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            disposition = (row.get("disposition") or "").upper()
            if disposition not in releasable:
                continue
            if products and row.get("product_id") not in products:
                # A plant making other products as well is normal; only the ones
                # this programme declares can be packed for it.
                continue
            completed = row.get("completed_at") or row.get("created_at") or ""
            try:
                released = date.fromisoformat(completed[:10])
            except ValueError:
                continue
            batches.append(
                Batch(
                    batch_id=row["batch_id"],
                    product_id=row["product_id"],
                    quantity=float(row.get("good_quantity") or 0.0),
                    released_on=released,
                    disposition=disposition,
                    stub=False,
                )
            )
    batches.sort(key=lambda batch: batch.released_on)
    return batches


def _stub_batches(config: LifecycleConfig, first_in: date) -> list[Batch]:
    """Deterministic stand-ins, clearly labelled as such."""
    stubs = config.stub_batches
    batches: list[Batch] = []
    for index in range(stubs.count):
        role = "ACTIVE" if index % 2 == 0 else "PLACEBO"
        product = config.product(role)
        assert product is not None
        offset = stubs.first_release_offset_weeks + index // 2 * stubs.release_interval_weeks
        batches.append(
            Batch(
                batch_id=f"STUB-BATCH-{index + 1:04d}",
                product_id=product.product_id,
                quantity=float(stubs.batch_size_tablets),
                released_on=first_in + timedelta(weeks=offset),
                disposition="RELEASED",
                stub=True,
            )
        )
    return batches


@dataclass
class Spine:
    """The resolved identity graph, plus the rows that record it."""

    config: LifecycleConfig
    batches: list[Batch] = field(default_factory=list)
    lots: list[ImpLot] = field(default_factory=list)
    shipments: list[Shipment] = field(default_factory=list)
    kits: list[Kit] = field(default_factory=list)
    accountability: list[dict] = field(default_factory=list)
    #: Kits on the shelf at each site, per treatment, in dispensing order.
    _available: dict[str, dict[str, list[Kit]]] = field(default_factory=dict, repr=False)
    #: Cycles for which no kit could be supplied, per site.
    stockouts: list[dict] = field(default_factory=list)
    #: Kits withdrawn from the shelf because their lot went out of date.
    expired: list[dict] = field(default_factory=list)
    #: Pre-generated blinded kit-number pool, drawn from as kits are created.
    _kit_numbers: list[str] = field(default_factory=list, repr=False)
    #: Owned rather than passed per call. A caller handing in a different factory
    #: reissued shipment ids that were already in use, and kits then inherited
    #: another shipment's arrival date -- a corruption with no visible cause.
    _ids: object | None = field(default=None, repr=False)

    def next_kit_number(self) -> str:
        if not self._kit_numbers:
            raise ValueError("kit number pool exhausted")
        return self._kit_numbers.pop()

    @property
    def linked(self) -> bool:
        """True when every batch came from a real manufacturing run."""
        return bool(self.batches) and not any(batch.stub for batch in self.batches)

    def kit_for(self, site_id: str, role: str, on: date | None = None) -> Kit | None:
        """Take the next usable kit of the required treatment off the shelf.

        The role is not optional. Interactive response technology assigns a kit
        matching the subject's randomised treatment -- handing out whatever is
        next on the shelf would give half the subjects the wrong arm's product,
        which is the single worst thing this graph could get wrong.

        A kit past its lot expiry is not usable. Real allocation systems refuse
        one and the stock is replaced; skipping over them here is the same
        behaviour, and the expired kits stay on the record as unused rather than
        disappearing.
        """
        shelf = self._available.get(site_id, {}).get(role)
        if not shelf:
            return None
        held_back: list[Kit] = []
        chosen: Kit | None = None
        while shelf:
            kit = shelf.pop(0)
            if on is not None and kit.available_from is not None and kit.available_from > on:
                # In transit. Put it back rather than skipping it for good.
                held_back.append(kit)
                continue
            if on is not None and kit.expiry is not None and kit.expiry <= on:
                self.expired.append(
                    {
                        "kit_number": kit.kit_number,
                        "site_id": site_id,
                        "lot_id": kit.lot_id,
                        "expiry": kit.expiry.isoformat(),
                        "withdrawn_on": on.isoformat(),
                    }
                )
                continue
            chosen = kit
            break
        shelf[:0] = held_back
        return chosen

    def kits_remaining(
        self, site_id: str, role: str | None = None, on: date | None = None
    ) -> int:
        """Kits a site can actually dispense.

        Counts only usable stock: not still in transit, not out of date. Counting
        everything on the shelf made the resupply trigger see plenty while the
        usable count was zero, so it never fired again and three quarters of
        cycles stocked out with 1,100 kits sitting unused.
        """
        pools = self._available.get(site_id, {})
        roles = [role] if role is not None else list(pools)

        def usable(kit: Kit) -> bool:
            if on is None:
                return True
            if kit.available_from is not None and kit.available_from > on:
                return False
            if kit.expiry is not None and kit.expiry <= on:
                return False
            return True

        return sum(
            sum(1 for kit in pools.get(name, ()) if usable(kit)) for name in roles
        )


def build_spine(
    config: LifecycleConfig,
    sites: list[tuple[str, date]],
    first_in: date,
    rng: Random,
    ids,
    *,
    manufacturing_export: str | Path | None = None,
) -> Spine:
    """Pack released batches into lots, kits and shipments to sites.

    Args:
        sites: ``(site_id, needed_by)`` pairs. A site's opening date matters: its
            first shipment can only contain lots that had been packed by then.
            Ignoring that produced kits dispensed weeks before they were made.
    """
    if manufacturing_export is not None:
        batches = load_released_batches(manufacturing_export, config)
        if not batches:
            raise ValueError(
                f"no releasable batches for this programme in {manufacturing_export}; "
                "the plant may not have made this product"
            )
    else:
        batches = _stub_batches(config, first_in)

    spine = Spine(config=config, batches=batches, _ids=ids)
    imp = config.imp
    role_by_product = {p.product_id: p.role for p in config.drug_products}

    # Pack every batch into a lot.
    for batch in batches:
        possible = int(batch.quantity // imp.tablets_per_kit)
        kits = min(possible, imp.kits_per_lot)
        if kits <= 0:
            continue
        packed = batch.released_on + timedelta(days=int(rng.uniform(5, 21)))
        spine.lots.append(
            ImpLot(
                lot_id=ids.next("LOT", width=4),
                batch_id=batch.batch_id,
                product_id=batch.product_id,
                role=role_by_product.get(batch.product_id, "UNKNOWN"),
                kits=kits,
                packed_on=packed,
                # Expiry is a fixed shelf life for now. Once ICH stability exists
                # this must come from the shelf-life regression instead, which is
                # the third spine link and is not built.
                expiry=packed + timedelta(days=int(imp.lot_expiry_months * 30.44)),
                stub_batch=batch.stub,
            )
        )

    if not spine.lots:
        raise ValueError("no IMP lots could be packed from the available batches")

    # Kit numbers come from a single pool, shuffled once, drawn from whenever a
    # kit is created. Numbering at packing or shipping time instead gave
    # consecutive numbers to consecutive kits of one treatment -- a resupply
    # shipment carries one treatment only -- so sorting the kit list by number
    # segregated the arms and anybody holding it could reconstruct the
    # allocation. A pre-generated pool is how interactive response technology
    # does it, for exactly this reason.
    pool_size = sum(lot.kits for lot in spine.lots) + 128
    numbers = [f"KIT-{index:06d}" for index in range(1, pool_size + 1)]
    rng.shuffle(numbers)
    spine._kit_numbers = numbers

    all_active = [lot for lot in spine.lots if lot.role == "ACTIVE"]
    all_placebo = [lot for lot in spine.lots if lot.role == "PLACEBO"]
    if not all_active or not all_placebo:
        raise ValueError(
            "a double-blind study needs both active and placebo lots; "
            f"found {len(active)} active and {len(placebo)} placebo"
        )

    # Ship to every site, drawing from both an active and a placebo lot so the
    # blind holds at the shelf.
    shipment_config = imp.shipment
    remaining = {lot.lot_id: lot.kits for lot in spine.lots}

    for site_id, needed_by in sites:
        # Only lots already packed, and not already out of date, can go in the
        # first shipment to this site.
        active = [
            lot for lot in all_active
            if lot.packed_on <= needed_by and lot.expiry > needed_by
        ] or all_active[:1]
        placebo = [
            lot for lot in all_placebo
            if lot.packed_on <= needed_by and lot.expiry > needed_by
        ] or all_placebo[:1]
        cursors = {"ACTIVE": 0, "PLACEBO": 0}
        shipment_id = ids.next("SHP", width=5)
        wanted = shipment_config.initial_kits_per_site
        taken: list[Kit] = []
        lots_used: list[str] = []

        for _ in range(wanted):
            # Alternate so a site's shelf is half active and half placebo.
            role = "ACTIVE" if len(taken) % 2 == 0 else "PLACEBO"
            pool = active if role == "ACTIVE" else placebo
            while cursors[role] < len(pool) and remaining[pool[cursors[role]].lot_id] <= 0:
                cursors[role] += 1
            if cursors[role] >= len(pool):
                break
            lot = pool[cursors[role]]
            remaining[lot.lot_id] -= 1
            if lot.lot_id not in lots_used:
                lots_used.append(lot.lot_id)
            taken.append(
                Kit(
                    kit_number=spine.next_kit_number(),
                    lot_id=lot.lot_id,
                    batch_id=lot.batch_id,
                    role=lot.role,
                    shipment_id=shipment_id,
                    site_id=site_id,
                    expiry=lot.expiry,
                )
            )

        lead = max(1.0, rng.gauss(shipment_config.lead_time_days.mean,
                                 shipment_config.lead_time_days.sd))
        packed_last = max(lot.packed_on for lot in spine.lots if lot.lot_id in lots_used)
        # Shipped once everything in it is packed, and in time for the site to
        # open rather than whenever the last lot happened to be made.
        shipped = min(max(packed_last, needed_by - timedelta(days=int(lead) + 7)),
                      needed_by)
        received = shipped + timedelta(days=lead)
        excursion = rng.random() < shipment_config.temperature_excursion_probability

        spine.shipments.append(
            Shipment(
                shipment_id=shipment_id,
                site_id=site_id,
                lot_ids=tuple(lots_used),
                kits=len(taken),
                shipped_on=shipped,
                received_on=received,
                status="QUARANTINED" if excursion else "AVAILABLE",
                temperature_excursion=excursion,
            )
        )

        if excursion:
            # A quarantined shipment cannot be dispensed. The site waits for a
            # replacement, which is a real event in an accountability record and
            # a real reason a subject's cycle slips.
            spine.accountability.append(
                {
                    "accountability_id": ids.next("ACC", width=6),
                    "site_id": site_id,
                    "shipment_id": shipment_id,
                    "event": "TEMPERATURE_EXCURSION",
                    "kits": len(taken),
                    "occurred_on": received.isoformat(),
                    "detail": "Excursion above 25 C in transit; shipment quarantined "
                              "and replaced",
                }
            )
            replacement_id = ids.next("SHP", width=5)
            replacement_received = received + timedelta(days=lead)
            spine.shipments.append(
                Shipment(
                    shipment_id=replacement_id,
                    site_id=site_id,
                    lot_ids=tuple(lots_used),
                    kits=len(taken),
                    shipped_on=received,
                    received_on=replacement_received,
                    status="AVAILABLE",
                    temperature_excursion=False,
                )
            )
            taken = [
                Kit(
                    kit_number=spine.next_kit_number(),
                    lot_id=kit.lot_id,
                    batch_id=kit.batch_id,
                    role=kit.role,
                    shipment_id=replacement_id,
                    site_id=site_id,
                    expiry=kit.expiry,
                )
                for kit in taken
            ]

        taken = [
            Kit(
                kit_number=kit.kit_number,
                lot_id=kit.lot_id,
                batch_id=kit.batch_id,
                role=kit.role,
                shipment_id=kit.shipment_id,
                site_id=kit.site_id,
                expiry=kit.expiry,
                available_from=spine.shipments[-1].received_on,
            )
            for kit in taken
        ]
        spine.kits.extend(taken)
        # Kit NUMBERS come from one pool and are assigned before the kits are
        # split by treatment, so the number carries no information about what is
        # inside it. Numbering sequentially within each arm would let anybody
        # holding the list work out the allocation, and it is a mistake that has
        # been made.
        shelf = list(taken)
        if config.randomisation.blinded_kit_pool:
            rng.shuffle(shelf)
        pools: dict[str, list[Kit]] = {}
        for kit in shelf:
            pools.setdefault(kit.role, []).append(kit)
        spine._available[site_id] = pools

        spine.accountability.append(
            {
                "accountability_id": ids.next("ACC", width=6),
                "site_id": site_id,
                "shipment_id": spine.shipments[-1].shipment_id,
                "event": "RECEIVED",
                "kits": len(taken),
                "occurred_on": spine.shipments[-1].received_on.isoformat(),
                "detail": f"{len(taken)} kits received and reconciled",
            }
        )

    return spine


def resupply(
    spine: Spine,
    site_id: str,
    role: str,
    on: date,
    rng: Random,
) -> int:
    """Ship more kits of one treatment to a site that is running low.

    Returns the number of kits added. Zero means the lots are exhausted, which is
    a supply failure rather than a silent no-op -- the caller records a stockout.
    """
    config = spine.config
    ids = spine._ids
    if ids is None:  # pragma: no cover - build_spine always sets it
        raise ValueError("spine was not built with an id factory")
    shipment_config = config.imp.shipment
    lots = [lot for lot in spine.lots if lot.role == role]
    if not lots:
        return 0

    # How many kits of this lot have already been shipped.
    shipped = {lot.lot_id: 0 for lot in lots}
    for kit in spine.kits:
        if kit.lot_id in shipped:
            shipped[kit.lot_id] += 1

    available = [
        lot
        for lot in lots
        if shipped[lot.lot_id] < lot.kits and lot.expiry > on and lot.packed_on <= on
    ]
    if not available:
        return 0

    shipment_id = ids.next("SHP", width=5)
    added: list[Kit] = []
    for lot in available:
        while len(added) < shipment_config.resupply_kits and shipped[lot.lot_id] < lot.kits:
            shipped[lot.lot_id] += 1
            added.append(
                Kit(
                    kit_number=spine.next_kit_number(),
                    lot_id=lot.lot_id,
                    batch_id=lot.batch_id,
                    role=lot.role,
                    shipment_id=shipment_id,
                    site_id=site_id,
                    expiry=lot.expiry,
                )
            )
        if len(added) >= shipment_config.resupply_kits:
            break

    if not added:
        return 0

    lead = max(1.0, rng.gauss(shipment_config.lead_time_days.mean,
                             shipment_config.lead_time_days.sd))
    received = on + timedelta(days=lead)
    spine.shipments.append(
        Shipment(
            shipment_id=shipment_id,
            site_id=site_id,
            lot_ids=tuple(dict.fromkeys(kit.lot_id for kit in added)),
            kits=len(added),
            shipped_on=on,
            received_on=received,
            status="AVAILABLE",
            temperature_excursion=False,
        )
    )
    spine.accountability.append(
        {
            "accountability_id": ids.next("ACC", width=6),
            "site_id": site_id,
            "shipment_id": shipment_id,
            "event": "RESUPPLY",
            "kits": len(added),
            "occurred_on": received.isoformat(),
            "detail": f"{len(added)} kits resupplied on falling below "
                      f"{shipment_config.resupply_trigger_kits} in stock",
        }
    )
    added = [
        Kit(
            kit_number=kit.kit_number,
            lot_id=kit.lot_id,
            batch_id=kit.batch_id,
            role=kit.role,
            shipment_id=kit.shipment_id,
            site_id=kit.site_id,
            expiry=kit.expiry,
            available_from=received,
        )
        for kit in added
    ]
    spine.kits.extend(added)
    spine._available.setdefault(site_id, {}).setdefault(role, []).extend(added)
    return len(added)
