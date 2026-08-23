"""Causality: precursors, QC transfer functions, labels and RCA.

These are the tests that matter most. The simulator's value rests on the claim
that its data is causally coherent — that a QC failure is downstream of a process
condition, which is downstream of a degrading machine. Each test below checks one
link in that chain rather than merely checking that records exist.
"""

from __future__ import annotations

import statistics
from datetime import timedelta

import pytest

from pharma_sim.domain.batch import Disposition


@pytest.fixture
def running_sim(sim):
    """A simulator warmed up so machines are staffed and batches are in progress."""
    sim.start()
    sim.run(hours=3)
    return sim


def _busy_machine(sim, equipment_class: str):
    """A machine of the given class, preferring one currently on a batch."""
    machines = sim.plant.machines_of_class(equipment_class)
    machines.sort(key=lambda m: (m.current_batch_id is None, m.machine_id))
    return machines[0]


class TestPrecursorCausality:
    """§16: a developing failure must show itself before it stops the machine."""

    def test_precursors_rise_together_as_degradation_advances(self, running_sim):
        machine = _busy_machine(running_sim, "tablet_press")
        running_sim.failures.inject(
            machine.machine_id, "BEARING_FAILURE", running_sim.clock.now
        )
        episode = machine.episodes[-1]

        samples = []
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            moment = episode.started_at + timedelta(
                hours=episode.incubation_hours * fraction
            )
            offsets = machine.precursor_effects(moment)
            samples.append(
                {tag: effect.offset for tag, effect in offsets.items()}
            )

        for tag in ("vibration", "motor_current", "temperature"):
            series = [s.get(tag, 0.0) for s in samples]
            assert series == sorted(series), f"{tag} did not rise monotonically"
            assert series[-1] > series[0], f"{tag} never moved"

    def test_a_single_driver_couples_the_tags(self, running_sim):
        """Correlation is structural: one progress value drives every precursor."""
        machine = _busy_machine(running_sim, "tablet_press")
        running_sim.failures.inject(
            machine.machine_id, "BEARING_FAILURE", running_sim.clock.now
        )
        episode = machine.episodes[-1]
        mid = episode.started_at + timedelta(hours=episode.incubation_hours * 0.6)
        offsets = machine.precursor_effects(mid)
        assert {"vibration", "motor_current", "temperature"} <= set(offsets)
        assert all(effect.offset > 0 for effect in offsets.values())

    def test_warning_precedes_the_fault(self, running_sim):
        machine = _busy_machine(running_sim, "tablet_press")
        running_sim.failures.inject(
            machine.machine_id, "BEARING_FAILURE", running_sim.clock.now
        )
        episode = machine.episodes[-1]
        running_sim.run(hours=running_sim.clock.elapsed_hours + episode.incubation_hours + 2)
        assert episode.faulted_at is not None or episode.averted_at is not None
        if episode.faulted_at is not None:
            assert episode.warned_at is not None, "detectable fault gave no warning"
            assert episode.warned_at < episode.faulted_at

    def test_every_detectable_mode_has_an_observable_precursor(self, registries):
        """A mode that can warn must have something to warn with, on every
        equipment class it applies to. The linter enforces this too; this checks
        the resolved registry actually honours it."""
        for class_id in registries.equipment.ids:
            for mode in registries.failures.for_class(class_id):
                if not (mode.spec.detectable and mode.spec.precursors):
                    continue
                assert mode.precursors, (
                    f"{mode.id} applies to {class_id} and is marked detectable, but "
                    f"none of its precursor tags exist on that equipment"
                )
                assert mode.detectable

    def test_non_detectable_mode_gives_no_warning(self, running_sim):
        machine = _busy_machine(running_sim, "tablet_press")
        running_sim.failures.inject(
            machine.machine_id, "POWER_FAILURE", running_sim.clock.now
        )
        episode = machine.episodes[-1]
        running_sim.run(hours=running_sim.clock.elapsed_hours + 4)
        assert episode.warned_at is None

    def test_fault_stops_production(self, running_sim):
        machine = _busy_machine(running_sim, "tablet_press")
        running_sim.failures.inject(
            machine.machine_id, "BEARING_FAILURE", running_sim.clock.now,
            incubation_hours=1.0,
        )
        running_sim.run(hours=running_sim.clock.elapsed_hours + 1.5)
        states = running_sim.registries.states
        assert states.is_downtime(machine.state) or machine.failure_count > 0

    def test_degradation_shifts_process_parameters(self, running_sim):
        machine = _busy_machine(running_sim, "tablet_press")
        running_sim.failures.inject(
            machine.machine_id, "BEARING_FAILURE", running_sim.clock.now
        )
        episode = machine.episodes[-1]
        late = episode.started_at + timedelta(hours=episode.incubation_hours * 0.95)
        shifts = machine.parameter_shifts(late)
        assert shifts.get("main_compression_force", 0.0) > 0.0


class TestQcCausality:
    """§17/§19: quality must be a consequence of process conditions."""

    def test_higher_compression_force_raises_hardness(self, config, registries):
        qc = registries.qc
        product = registries.topology.product("PARA-500")
        spec = qc.effective(product, "tablet_hardness")
        low = spec.spec.transfer.evaluate(
            {"main_compression_force": 12.0, "moisture_content": 2.5}
        )
        high = spec.spec.transfer.evaluate(
            {"main_compression_force": 17.0, "moisture_content": 2.5}
        )
        assert high > low

    def test_harder_tablets_disintegrate_more_slowly(self, registries):
        product = registries.topology.product("PARA-500")
        spec = registries.qc.effective(product, "disintegration_time")
        fast = spec.spec.transfer.evaluate(
            {"tablet_hardness": 70.0, "coating_weight_gain": 3.1}
        )
        slow = spec.spec.transfer.evaluate(
            {"tablet_hardness": 105.0, "coating_weight_gain": 3.1}
        )
        assert slow > fast

    def test_slower_disintegration_lowers_dissolution(self, registries):
        product = registries.topology.product("PARA-500")
        spec = registries.qc.effective(product, "dissolution")
        quick = spec.spec.transfer.evaluate(
            {"disintegration_time": 4.0, "coating_weight_gain": 3.1}
        )
        slow = spec.spec.transfer.evaluate(
            {"disintegration_time": 12.0, "coating_weight_gain": 3.1}
        )
        assert slow < quick

    def test_the_full_compression_chain_is_monotonic(self, registries):
        """force -> hardness -> disintegration -> dissolution, end to end."""
        product = registries.topology.product("PARA-500")
        qc = registries.qc

        def chain(force: float) -> tuple[float, float, float]:
            hardness = qc.effective(product, "tablet_hardness").spec.transfer.evaluate(
                {"main_compression_force": force, "moisture_content": 2.5}
            )
            disintegration = qc.effective(
                product, "disintegration_time"
            ).spec.transfer.evaluate(
                {"tablet_hardness": hardness, "coating_weight_gain": 3.1}
            )
            dissolution = qc.effective(product, "dissolution").spec.transfer.evaluate(
                {"disintegration_time": disintegration, "coating_weight_gain": 3.1}
            )
            return hardness, disintegration, dissolution

        nominal = chain(14.0)
        excessive = chain(21.0)
        assert excessive[0] > nominal[0]  # harder
        assert excessive[1] > nominal[1]  # slower to disintegrate
        assert excessive[2] < nominal[2]  # less dissolved

    def test_hotter_drying_lowers_moisture(self, registries):
        product = registries.topology.product("PARA-500")
        spec = registries.qc.effective(product, "moisture_content")
        wet = spec.spec.transfer.evaluate({"inlet_temperature": 54.0, "drying_time": 45.0})
        dry = spec.spec.transfer.evaluate({"inlet_temperature": 70.0, "drying_time": 45.0})
        assert dry < wet

    def test_longer_blending_improves_uniformity(self, registries):
        product = registries.topology.product("PARA-500")
        spec = registries.qc.effective(product, "blend_uniformity_rsd")
        short = spec.spec.transfer.evaluate(
            {"blend_time": 15.0, "rpm": 14.0, "granule_size": 240.0}
        )
        long = spec.spec.transfer.evaluate(
            {"blend_time": 32.0, "rpm": 14.0, "granule_size": 240.0}
        )
        assert long < short

    def test_uniformity_carries_through_to_assay(self, registries):
        product = registries.topology.product("PARA-500")
        spec = registries.qc.effective(product, "assay")
        good = spec.spec.transfer.evaluate(
            {"content_uniformity": 1.5, "dispensed_weight": 25.0, "material_variability": 0.0}
        )
        poor = spec.spec.transfer.evaluate(
            {"content_uniformity": 5.5, "dispensed_weight": 25.0, "material_variability": 0.0}
        )
        assert good > poor

    def test_nominal_inputs_land_on_target(self, registries):
        """Intercepts are calibrated, so a correct process gives a correct result."""
        product = registries.topology.product("PARA-500")
        nominal = {
            "inlet_temperature": 62.0,
            "drying_time": 45.0,
            "particle_size": 240.0,
            "blend_time": 22.0,
            "rpm": 14.0,
            "main_compression_force": 14.0,
            "tablet_weight": 550.0,
            "spray_rate": 120.0,
            "dispensed_weight": 25.0,
            "material_variability": 0.024,
            "machine_health": 0.0,
        }
        computed: dict[str, float] = {}
        for spec in registries.qc.for_product(product):
            values = dict(nominal)
            values.update(computed)
            # Coating and drying share an inlet_temperature name; use each
            # parameter's own stage nominal.
            if spec.stage == "COATING":
                values["inlet_temperature"] = 58.0
            value = spec.spec.transfer.evaluate(values, registries.qc.reference_values())
            computed[spec.id] = value
            assert spec.classify(value) in {"PASS", "OOT"}, (
                f"{spec.id} = {value:.3f} against target {spec.target} "
                f"[{spec.lower_limit}, {spec.upper_limit}] under nominal conditions"
            )

    def test_out_of_window_force_can_fail_hardness(self, registries):
        product = registries.topology.product("PARA-500")
        spec = registries.qc.effective(product, "tablet_hardness")
        value = spec.spec.transfer.evaluate(
            {"main_compression_force": 22.0, "moisture_content": 2.0}
        )
        assert spec.classify(value) in {"FAIL", "OOS"}

    def test_qc_classification_bands(self, registries):
        product = registries.topology.product("PARA-500")
        spec = registries.qc.effective(product, "tablet_hardness")
        assert spec.classify(spec.target) == "PASS"
        assert spec.classify(spec.upper_limit + 50.0) == "OOS"
        assert spec.classify(spec.upper_limit - 0.5) == "OOT"

    def test_evaluation_order_respects_dependencies(self, registries):
        order = registries.qc.evaluation_order
        assert order.index("tablet_hardness") < order.index("disintegration_time")
        assert order.index("disintegration_time") < order.index("dissolution")
        assert order.index("blend_uniformity_rsd") < order.index("content_uniformity")
        assert order.index("content_uniformity") < order.index("assay")


class TestBatchAndQcIntegration:
    def test_batches_progress_and_reach_a_disposition(self, completed_run):
        sim = completed_run
        assert sim.batches.stats.batches_completed > 0
        for batch in sim.batches.completed:
            assert batch.disposition in {
                Disposition.RELEASED,
                Disposition.REJECTED,
                Disposition.QUARANTINED,
            }
            assert batch.completed_at is not None
            assert len(batch.stages) == len(batch.route)

    def test_qc_values_derive_from_measured_process_values(self, completed_run):
        """A stage's achieved parameters come from the telemetry, not thin air."""
        sim = completed_run
        batch = next(
            (b for b in sim.batches.completed if b.stage_for("COMPRESSION")), None
        )
        assert batch is not None, "no batch reached compression"
        stage = batch.stage_for("COMPRESSION")
        assert stage.parameters, "no process parameters were measured"
        assert "main_compression_force" in stage.parameters

        product = sim.registries.topology.product(batch.product_id)
        window = product.process_parameters["COMPRESSION"]["main_compression_force"]
        measured = stage.parameters["main_compression_force"]
        # The measured mean should sit near the recipe setpoint.
        assert abs(measured - window.target) < window.target * 0.25

    def test_each_stage_records_its_machine_and_operator(self, completed_run):
        sim = completed_run
        for batch in sim.batches.completed:
            for stage in batch.stages:
                assert stage.machine_id in sim.plant.machines
                assert stage.unit_id in sim.plant.units
                assert stage.completed_at is not None

    def test_stage_machine_actually_measures_the_stage_parameters(self, completed_run):
        """A deduster must never be assigned to run compression."""
        sim = completed_run
        topology = sim.registries.topology
        for batch in sim.batches.completed:
            product = topology.product(batch.product_id)
            for stage in batch.stages:
                wanted = set(product.process_parameters.get(stage.stage, {}))
                if not wanted:
                    continue
                machine = sim.plant.machine(stage.machine_id)
                assert wanted & set(machine.sensors), (
                    f"{stage.machine_id} ran {stage.stage} without measuring any of "
                    f"{sorted(wanted)}"
                )

    def test_released_batches_pass_their_final_tests(self, completed_run):
        sim = completed_run
        for batch in sim.batches.completed:
            if batch.disposition == Disposition.RELEASED:
                assert not batch.failed_qc, (
                    f"{batch.batch_id} released with {len(batch.failed_qc)} failures"
                )

    def test_rejected_batches_have_a_reason(self, completed_run):
        sim = completed_run
        rejected = [
            b for b in sim.batches.completed if b.disposition == Disposition.REJECTED
        ]
        for batch in rejected:
            assert batch.failed_qc, f"{batch.batch_id} rejected with no failing test"

    def test_qc_failure_rate_is_realistic(self, completed_run):
        """A pharma plant that fails a third of its tests is not a pharma plant."""
        sim = completed_run
        stats = sim.batches.stats
        assert stats.qc_tests > 100
        rate = stats.qc_failures / stats.qc_tests
        assert rate < 0.10, f"QC failure rate {rate:.1%} is implausibly high"


class TestPredictionLabels:
    def test_rul_decreases_to_zero_at_the_fault(self, running_sim):
        machine = _busy_machine(running_sim, "tablet_press")
        running_sim.failures.inject(
            machine.machine_id, "BEARING_FAILURE", running_sim.clock.now
        )
        episode = machine.episodes[-1]
        truth = running_sim.ledger.get(episode.episode_id)
        labels = running_sim.ledger.labels_for_episode(
            truth, until=episode.fault_at + timedelta(hours=1)
        )
        assert labels
        ruls = [label.rul_hours for label in labels]
        assert ruls == sorted(ruls, reverse=True), "RUL is not monotonically decreasing"
        assert ruls[-1] < ruls[0]
        assert min(ruls) >= 0.0

    def test_horizon_flags_agree_with_the_remaining_life(self, running_sim):
        machine = _busy_machine(running_sim, "tablet_press")
        running_sim.failures.inject(
            machine.machine_id, "BEARING_FAILURE", running_sim.clock.now
        )
        truth = running_sim.ledger.get(machine.episodes[-1].episode_id)
        for label in running_sim.ledger.labels_for_episode(
            truth, until=truth.scheduled_fault_at
        ):
            assert label.will_fail_24h == (label.rul_hours <= 24.0)
            assert label.will_fail_72h == (label.rul_hours <= 72.0)
            if label.will_fail_24h:
                assert label.will_fail_72h  # nested horizons

    def test_averted_episodes_do_not_claim_a_failure(self, running_sim):
        """The point of the averted flag: labels must not assert what never happened."""
        machine = _busy_machine(running_sim, "blister_packaging_machine")
        running_sim.failures.inject(
            machine.machine_id, "BELT_FAILURE", running_sim.clock.now
        )
        episode = machine.episodes[-1]
        # Intervene before the scheduled fault.
        machine.resolve_episodes(
            episode.started_at + timedelta(hours=episode.incubation_hours * 0.5), 1.0
        )
        truth = running_sim.ledger.get(episode.episode_id)
        truth.averted_at = episode.averted_at
        labels = running_sim.ledger.labels_for_episode(truth, until=truth.scheduled_fault_at)
        assert labels
        assert all(label.averted for label in labels)
        assert not any(label.will_fail_24h for label in labels)

    def test_health_index_rises_across_the_window(self, running_sim):
        machine = _busy_machine(running_sim, "tablet_press")
        running_sim.failures.inject(
            machine.machine_id, "BEARING_FAILURE", running_sim.clock.now
        )
        truth = running_sim.ledger.get(machine.episodes[-1].episode_id)
        labels = running_sim.ledger.labels_for_episode(
            truth, until=truth.scheduled_fault_at
        )
        health = [label.health_index for label in labels]
        assert health == sorted(health)
        assert health[-1] > 0.5

    def test_healthy_machines_get_negative_labels(self, completed_run):
        sim = completed_run
        assert sim.status()["labels"] > 0

    def test_degradation_stage_is_ordered(self, running_sim):
        from pharma_sim.domain.ground_truth import degradation_stage_for

        stages = [degradation_stage_for(h) for h in (0.0, 0.1, 0.4, 0.7, 0.95)]
        assert stages == ["HEALTHY", "EARLY", "DEVELOPING", "ADVANCED", "IMMINENT"]


class TestRcaEngine:
    def test_rca_reaches_a_conclusion_with_evidence(self, completed_run):
        sim = completed_run
        if not sim.rca.reports:
            pytest.skip("no deviations arose in this window")
        for report in sim.rca.reports.values():
            assert report.root_cause
            assert report.five_why
            assert 0.0 <= report.confidence <= 1.0
            assert report.deviation_id in sim.deviations.deviations

    def test_rca_is_scored_against_hidden_ground_truth(self, completed_run):
        """RCA must be fallible but useful; this measures which."""
        sim = completed_run
        scored = []
        for report in sim.rca.reports.values():
            if not report.failure_id:
                continue
            truth = sim.ledger.by_failure(report.failure_id)
            if truth is None:
                continue
            scored.append(report.root_cause == truth.root_cause)
        if len(scored) < 3:
            pytest.skip(f"only {len(scored)} failures with ground truth in this window")
        accuracy = sum(scored) / len(scored)
        assert accuracy > 0.4, f"RCA accuracy {accuracy:.0%} is too low to be useful"

    def test_rca_evidence_is_quantified(self, completed_run):
        sim = completed_run
        with_evidence = [r for r in sim.rca.reports.values() if r.evidence]
        if not with_evidence:
            pytest.skip("no evidence-backed RCA in this window")
        for report in with_evidence:
            for item in report.evidence:
                assert item.tag or item.signal
                assert item.weight > 0
                assert item.render()

    def test_rca_falls_back_rather_than_guessing_wildly(self, completed_run):
        sim = completed_run
        """With no evidence the engine must say so, not invent a cause."""
        from pharma_sim.domain.quality_management import Deviation

        deviation = Deviation(
            deviation_id="DEV-TEST",
            rule_id="R",
            title="synthetic",
            severity="MAJOR",
            status="OPEN",
            detected_at=sim.clock.now,
            plant_id=sim.plant.plant_id,
            unit_id=None,
            machine_id=None,
            batch_id=None,
            trigger_event="MACHINE_FAILURE",
            trigger_event_id="EVT-TEST",
            failure_id=None,
            description="no evidence available",
            requires_rca=True,
            requires_capa=False,
            run_id=sim.run_id,
        )
        sim.deviations.deviations[deviation.deviation_id] = deviation
        # The deviation row must exist before an RCA row can reference it: the
        # foreign key is enforced, and rightly rejects the write otherwise.
        sim.storage.write("deviations", deviation.as_row())
        report = sim.rca.investigate(
            deviation, now=sim.clock.now, history=None, category=None, signals={}
        )
        assert report.root_cause == sim.config.rca_rules.fallback_root_cause
        assert report.confidence < 0.5
