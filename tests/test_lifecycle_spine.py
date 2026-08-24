"""The identity graph from a released batch to a dose in a subject.

Two things are being defended. The first is that every link resolves — which the
nine spine checks assert, and which a deliberately broken graph must fail. The
second is that the graph refuses to invent anything: given a plant export with no
batch of this programme's product in it, the spine raises rather than quietly
falling back to fabricated batches, because a lineage claim that silently
degrades is worse than no lineage claim.
"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path
from random import Random

import pytest

from pharma_sim.clinical.loader import load_clinical_config
from pharma_sim.clinical.study import run_study
from pharma_sim.config.errors import ConfigError
from pharma_sim.engine.ids import IdFactory
from pharma_sim.lifecycle.config import load_lifecycle_config
from pharma_sim.lifecycle.spine import (
    Kit,
    build_spine,
    load_released_batches,
    resupply,
)
from pharma_sim.lifecycle.verify import verify_spine

ROOT = Path(__file__).resolve().parents[1]
CLINICAL = ROOT / "config" / "clinical"
LIFECYCLE = ROOT / "config" / "lifecycle"

FIRST_IN = date(2026, 3, 16)
SITES = [(f"S-{index}", FIRST_IN + timedelta(weeks=14 + index)) for index in range(3)]


@pytest.fixture(scope="module")
def lifecycle():
    return load_lifecycle_config(LIFECYCLE)


@pytest.fixture(scope="module")
def study(lifecycle):
    return run_study(load_clinical_config(CLINICAL), seed=42, lifecycle=lifecycle)


def _batch_export(directory: Path, config, count: int = 8) -> Path:
    """A manufacturing export that does contain this programme's products."""
    active = config.product("ACTIVE")
    placebo = config.product("PLACEBO")
    rows = []
    for index in range(count):
        product = active if index % 2 == 0 else placebo
        rows.append(
            {
                "batch_id": f"BATCH-2026-{index + 1:06d}",
                "product_id": product.product_id,
                "good_quantity": 62000,
                "disposition": "RELEASED",
                "completed_at": (FIRST_IN + timedelta(weeks=-14 + index * 6)).isoformat(),
            }
        )
    # One rejected batch, which must never be packed.
    rows.append(
        {
            "batch_id": "BATCH-2026-999999",
            "product_id": active.product_id,
            "good_quantity": 62000,
            "disposition": "REJECTED",
            "completed_at": FIRST_IN.isoformat(),
        }
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "batch_data.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return directory


class TestConfiguration:
    def test_loads(self, lifecycle):
        assert lifecycle.product("ACTIVE") is not None
        assert lifecycle.product("PLACEBO") is not None

    def test_every_arm_maps_to_a_product(self, lifecycle):
        config = load_clinical_config(CLINICAL)
        for arm in config.protocol.arms:
            assert lifecycle.randomisation.role_for(arm.arm_id) is not None

    def test_rejects_an_arm_mapped_to_an_undeclared_product(self, tmp_path):
        source = (LIFECYCLE / "links.yaml").read_text()
        broken = tmp_path / "links.yaml"
        broken.write_text(source.replace("role: PLACEBO}", "role: SUGAR_PILL}", 1))
        with pytest.raises(ConfigError) as excinfo:
            load_lifecycle_config(broken)
        assert "SUGAR_PILL" in str(excinfo.value)


class TestBatchSourcing:
    def test_stubs_are_labelled_as_stubs(self, lifecycle):
        spine = build_spine(lifecycle, SITES, FIRST_IN, Random(1), IdFactory())
        assert not spine.linked
        assert all(batch.stub for batch in spine.batches)
        assert all(lot.stub_batch for lot in spine.lots)

    def test_reads_released_batches_from_an_export(self, lifecycle, tmp_path):
        export = _batch_export(tmp_path / "export", lifecycle)
        batches = load_released_batches(export, lifecycle)
        assert batches
        assert all(not batch.stub for batch in batches)

    def test_a_rejected_batch_is_never_packed(self, lifecycle, tmp_path):
        """The most consequential possible error in this graph."""
        export = _batch_export(tmp_path / "export", lifecycle)
        batches = load_released_batches(export, lifecycle)
        assert "BATCH-2026-999999" not in {batch.batch_id for batch in batches}
        assert all(batch.disposition == "RELEASED" for batch in batches)

    def test_a_batch_of_another_product_is_not_packed(self, lifecycle, tmp_path):
        export = tmp_path / "other"
        export.mkdir()
        (export / "batch_data.csv").write_text(
            "batch_id,product_id,good_quantity,disposition,completed_at\n"
            "BATCH-1,PARA-500,300000,RELEASED,2026-01-05\n"
        )
        assert load_released_batches(export, lifecycle) == []

    def test_linking_to_a_plant_without_this_product_raises(self, lifecycle, tmp_path):
        """Refusing is the point. Falling back to stubs here would mean a dataset
        that claims manufacturing lineage and does not have it."""
        export = tmp_path / "other"
        export.mkdir()
        (export / "batch_data.csv").write_text(
            "batch_id,product_id,good_quantity,disposition,completed_at\n"
            "BATCH-1,PARA-500,300000,RELEASED,2026-01-05\n"
        )
        with pytest.raises(ValueError, match="no releasable batches"):
            build_spine(
                lifecycle, SITES, FIRST_IN, Random(1), IdFactory(),
                manufacturing_export=export,
            )

    def test_a_missing_export_raises_rather_than_stubbing(self, lifecycle, tmp_path):
        with pytest.raises(FileNotFoundError):
            build_spine(
                lifecycle, SITES, FIRST_IN, Random(1), IdFactory(),
                manufacturing_export=tmp_path / "nothing",
            )

    def test_the_linked_path_produces_real_lineage(self, lifecycle, tmp_path):
        export = _batch_export(tmp_path / "export", lifecycle)
        spine = build_spine(
            lifecycle, SITES, FIRST_IN, Random(1), IdFactory(),
            manufacturing_export=export,
        )
        assert spine.linked
        assert all(not lot.stub_batch for lot in spine.lots)
        assert all(kit.batch_id.startswith("BATCH-") for kit in spine.kits)


class TestDispensing:
    @pytest.fixture
    def spine(self, lifecycle):
        return build_spine(lifecycle, SITES, FIRST_IN, Random(3), IdFactory())

    def test_returns_the_requested_treatment(self, spine):
        site = SITES[0][0]
        for role in ("ACTIVE", "PLACEBO"):
            kit = spine.kit_for(site, role, SITES[0][1] + timedelta(weeks=4))
            assert kit is not None and kit.role == role

    def test_refuses_a_kit_still_in_transit(self, spine):
        site, opened = SITES[0]
        # Long before the shipment can have arrived.
        assert spine.kit_for(site, "ACTIVE", opened - timedelta(days=120)) is None

    def test_a_held_back_kit_is_not_lost(self, spine):
        """A kit refused for being in transit has to still be there later."""
        site, opened = SITES[0]
        before = spine.kits_remaining(site, "ACTIVE")
        spine.kit_for(site, "ACTIVE", opened - timedelta(days=120))
        assert spine.kits_remaining(site, "ACTIVE") == before

    def test_refuses_an_expired_kit_and_records_it(self, spine):
        site, opened = SITES[0]
        far_future = opened + timedelta(days=365 * 6)
        assert spine.kit_for(site, "ACTIVE", far_future) is None
        assert spine.expired

    def test_remaining_counts_only_usable_stock(self, spine):
        site, opened = SITES[0]
        total = spine.kits_remaining(site, "ACTIVE")
        in_transit = spine.kits_remaining(site, "ACTIVE", opened - timedelta(days=120))
        assert total > 0
        assert in_transit == 0

    def test_resupply_adds_stock_that_arrives_later(self, spine, lifecycle):
        site, opened = SITES[0]
        when = opened + timedelta(weeks=20)
        added = resupply(spine, site, "ACTIVE", when, Random(5))
        assert added > 0
        latest = spine.shipments[-1]
        assert latest.received_on > when
        # Not usable on the day it was ordered.
        assert all(
            kit.available_from > when
            for kit in spine.kits
            if kit.shipment_id == latest.shipment_id
        )


class TestSpineIntegrity:
    def test_all_checks_pass_on_the_generated_study(self, study, lifecycle):
        report = verify_spine(study, lifecycle)
        assert report.ok, report.render()
        assert len(report.checks) == 12

    def test_the_report_says_whether_lineage_is_real(self, study, lifecycle):
        report = verify_spine(study, lifecycle)
        assert report.linked is study.spine_linked

    def test_every_dose_traces_to_a_batch(self, study):
        kits = {row["kit_number"]: row for row in study.imp_kits}
        lots = {row["lot_id"] for row in study.imp_lots}
        assert study.dosing
        for row in study.dosing:
            kit = kits[row["kit_number"]]
            assert kit["lot_id"] in lots
            assert row["batch_id"] == kit["batch_id"]

    def test_exposure_records_carry_the_kit_that_produced_them(self, study):
        kits = {row["kit_number"] for row in study.imp_kits}
        assert study.ex
        for row in study.ex:
            assert row["EXREFID"] in kits

    def test_treatment_label_stays_blinded_in_the_exposure_record(self, study):
        """EXTRT records what the form said, which under a double blind names
        neither treatment."""
        labels = {row["EXTRT"] for row in study.ex}
        assert labels == {"NELVORASIB OR PLACEBO"}


class TestTheChecksAreNotVacuous:
    """Each check is confirmed to fail against a graph broken in that one way."""

    def test_catches_a_subject_given_the_wrong_arms_product(self, study, lifecycle):
        broken = _shallow_copy(study)
        wrong = "PLACEBO" if broken.imp_kits[0]["role"] == "ACTIVE" else "ACTIVE"
        broken.imp_kits = [{**broken.imp_kits[0], "role": wrong}, *broken.imp_kits[1:]]
        target = broken.imp_kits[0]["kit_number"]
        if not any(row["kit_number"] == target for row in broken.dosing):
            pytest.skip("that kit was never dispensed in this run")
        report = verify_spine(broken, lifecycle)
        failed = [check for check in report.checks if not check.passed]
        assert any("matches arm" in check.name for check in failed)

    def test_catches_a_kit_dispensed_twice(self, study, lifecycle):
        broken = _shallow_copy(study)
        first = broken.dosing[0]
        broken.dosing = [*broken.dosing, {**first, "subject_id": "OTHER-SUBJECT"}]
        report = verify_spine(broken, lifecycle)
        assert any(
            not check.passed and "dispensed once" in check.name for check in report.checks
        )

    def test_catches_a_kit_whose_lot_does_not_exist(self, study, lifecycle):
        broken = _shallow_copy(study)
        broken.imp_kits = [
            {**broken.imp_kits[0], "lot_id": "LOT-NOPE"},
            *broken.imp_kits[1:],
        ]
        report = verify_spine(broken, lifecycle)
        assert any(
            not check.passed and "resolves to lot" in check.name for check in report.checks
        )

    def test_catches_an_exposure_record_with_no_dispensing_behind_it(
        self, study, lifecycle
    ):
        broken = _shallow_copy(study)
        broken.ex = [*broken.ex, {**broken.ex[0], "USUBJID": "GHOST", "EXSEQ": 99}]
        report = verify_spine(broken, lifecycle)
        assert any(
            not check.passed and "EX reconciles" in check.name for check in report.checks
        )

    def test_catches_kit_numbering_that_leaks_the_allocation(self, study, lifecycle):
        """Numbering sequentially within each arm is a real mistake, so the check
        that would catch it has to actually catch it."""
        broken = _shallow_copy(study)
        ordered = sorted(broken.imp_kits, key=lambda row: row["role"])
        broken.imp_kits = [
            {**row, "kit_number": f"KIT-{index:06d}"}
            for index, row in enumerate(ordered, start=1)
        ]
        report = verify_spine(broken, lifecycle)
        assert any(
            not check.passed and "reveals nothing" in check.name for check in report.checks
        )


def _shallow_copy(study):
    """A copy whose row lists can be mutated without touching the fixture."""
    import copy
    from dataclasses import fields

    clone = copy.copy(study)
    for spec in fields(study):
        value = getattr(study, spec.name)
        if isinstance(value, list):
            setattr(clone, spec.name, list(value))
    return clone


class TestIdentifierUniqueness:
    """Added after a caller passing its own id factory into resupply reissued a
    shipment id, so kits inherited another shipment's arrival date."""

    def test_resupply_cannot_reissue_a_shipment_id(self, lifecycle):
        spine = build_spine(lifecycle, SITES, FIRST_IN, Random(3), IdFactory())
        before = {shipment.shipment_id for shipment in spine.shipments}
        resupply(spine, SITES[0][0], "ACTIVE", SITES[0][1] + timedelta(weeks=20), Random(5))
        ids = [shipment.shipment_id for shipment in spine.shipments]
        assert len(ids) == len(set(ids))
        assert len(set(ids) - before) == 1

    def test_the_check_catches_a_reissued_identifier(self, study, lifecycle):
        broken = _shallow_copy(study)
        broken.imp_shipments = [*broken.imp_shipments, broken.imp_shipments[0]]
        report = verify_spine(broken, lifecycle)
        assert any(
            not check.passed and "shipment identifiers unique" in check.name
            for check in report.checks
        )
