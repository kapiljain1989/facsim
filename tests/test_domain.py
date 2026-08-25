"""Topology, state machine, sensors, shifts, PLC and OEE."""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta

import pytest

from pharma_sim.domain.employee import Employee
from pharma_sim.domain.environment import Environment
from pharma_sim.domain.machine import ProductionWindow
from pharma_sim.domain.oee import aggregate_windows, compute_oee
from pharma_sim.domain.plant import FactoryBuilder
from pharma_sim.domain.sensor import Quality
from pharma_sim.domain.shift import ShiftScheduler
from pharma_sim.engine.rng import RngRegistry
from pharma_sim.registry import Registries
from pharma_sim.registry.states import IllegalTransition

START = datetime(2026, 1, 1, 6, 0, 0)


@pytest.fixture(scope="module")
def plant(config, registries):
    return FactoryBuilder(config, registries, RngRegistry(42)).build(START)


class TestTopology:
    """Counts are asserted against config, so a config edit does not break these."""

    def test_unit_count_matches_config(self, plant, config):
        assert len(plant.units) == len(config.units.units)

    def test_machine_count_matches_the_declared_layout(self, plant, config):
        declared = sum(
            group.count for groups in config.machines.layout.values() for group in groups
        )
        assert plant.machine_count == declared

    def test_every_unit_has_its_configured_worker_count(self, plant, config):
        for spec in config.units.units:
            assert len(plant.unit(spec.id).worker_ids) == spec.worker_count

    def test_every_machine_resolves_to_a_real_unit_and_class(self, plant, registries):
        for machine in plant.machines.values():
            assert machine.unit_id in plant.units
            assert machine.equipment_class in registries.equipment.ids

    def test_machine_ids_are_unique(self, plant):
        ids = [m.machine_id for m in plant.machines.values()]
        assert len(ids) == len(set(ids))

    def test_sensor_ids_are_globally_unique_and_resolve_to_their_machine(self, plant):
        seen: set[str] = set()
        for machine in plant.machines.values():
            for sensor_id in machine.sensor_ids():
                assert sensor_id not in seen
                seen.add(sensor_id)
                assert sensor_id.startswith(f"{machine.machine_id}:")

    def test_every_machine_has_at_least_one_sensor(self, plant):
        assert all(machine.sensors for machine in plant.machines.values())

    def test_plc_tag_count_matches_the_sensor_count(self, plant):
        for machine in plant.machines.values():
            assert len(machine.plc) == len(machine.sensors)

    def test_shipped_default_config_matches_the_brief(self, plant, config):
        """The shipped numbers are the default config, not an assumption.

        Asserted as a composition rather than as totals. The commercial plant is
        the ten units and hundred machines of the original brief; the containment
        suite for the oncology programme is four more units of one machine each,
        added because an OEB 4 compound cannot be handled in shared equipment.
        Pinning only the totals would let a change to either part hide inside the
        other.
        """
        contained = {
            unit_id
            for unit_id, unit in plant.units.items()
            if unit.spec.process_stage.startswith("CONTAINED_")
        }
        commercial = set(plant.units) - contained

        assert len(commercial) == 10, "the commercial plant is the original brief"
        assert len(contained) == 4, "one contained unit per stage of the oncology route"
        assert sum(
            1 for m in plant.machines.values() if m.unit_id in commercial
        ) == 100
        assert sum(1 for m in plant.machines.values() if m.unit_id in contained) == 4

        assert len(plant.units) == sum(1 for u in plant.units.values() if u.manager_ids)
        managers = [e for e in plant.employees.values() if e.role == "UNIT_MANAGER"]
        assert len(managers) == len(plant.units)
        assert sum(1 for e in plant.employees.values() if e.role == "PLANT_MANAGER") == 1
        assert len(config.shifts.shifts) == 3
        assert len(config.states.states) == 9

        # Headcount follows the declared worker_count per unit, so it is derived
        # rather than restated: gowning limits how many people are in a
        # containment suite, and the config says four.
        assert plant.worker_count == sum(u.worker_count for u in config.units.units)

    def test_workforce_is_spread_across_every_shift(self, plant, config):
        codes = {spec.code for spec in config.shifts.shifts}
        for code in codes:
            assert plant.employees_on_shift(code), f"no one on shift {code}"

    def test_build_is_reproducible_for_a_given_seed(self, config, registries):
        first = FactoryBuilder(config, registries, RngRegistry(42)).build(START)
        second = FactoryBuilder(config, registries, RngRegistry(42)).build(START)
        assert [m.commissioned_on for m in first.machines.values()] == [
            m.commissioned_on for m in second.machines.values()
        ]
        assert [e.name for e in first.employees.values()] == [
            e.name for e in second.employees.values()
        ]

    def test_different_seed_gives_a_different_plant(self, config, registries):
        first = FactoryBuilder(config, registries, RngRegistry(42)).build(START)
        second = FactoryBuilder(config, registries, RngRegistry(99)).build(START)
        assert [e.name for e in first.employees.values()] != [
            e.name for e in second.employees.values()
        ]


class TestDuty:
    """A machine's duty decides what "has work" means (machine.py `has_work`).

    Without it, every utility and inline-support machine would sit unscheduled
    forever: `_select_machine` only routes a batch stage to equipment whose
    sensor tags measure that stage's process parameters, which correctly
    excludes them from ever holding a batch.
    """

    def test_every_machine_has_a_declared_duty(self, plant, config):
        duty_by_class = {c.id: c.duty for c in config.machines.equipment_classes}
        for machine in plant.machines.values():
            assert machine.duty == duty_by_class[machine.equipment_class]
            assert machine.duty in ("batch", "continuous", "coupled")

    def test_continuous_machines_always_have_work(self, plant):
        continuous = [m for m in plant.machines.values() if m.duty == "continuous"]
        assert continuous, "shipped config should declare some continuous-duty equipment"
        for machine in continuous:
            assert machine.is_continuous
            assert machine.has_work
            assert machine.current_batch_id is None

    def test_coupled_machines_follow_line_state(self, plant):
        coupled = [m for m in plant.machines.values() if m.duty == "coupled"]
        assert coupled, "shipped config should declare some coupled equipment"
        machine = coupled[0]
        assert not machine.has_work
        machine.set_line_active(True)
        assert machine.has_work
        machine.set_line_active(False)
        assert not machine.has_work

    def test_batch_machines_have_work_only_when_holding_a_batch(self, plant):
        batch = next(m for m in plant.machines.values() if m.duty == "batch")
        assert not batch.has_work
        batch.current_batch_id = "BATCH-TEST"
        assert batch.has_work
        batch.current_batch_id = None
        assert not batch.has_work

    def test_shipped_layout_has_all_three_duty_classes(self, plant):
        """Utilities and inline support exist as real equipment, not just batch stages."""
        duties = {m.duty for m in plant.machines.values()}
        assert duties == {"batch", "continuous", "coupled"}


class TestSensorBinding:
    def test_profile_is_inherited(self, registries):
        press = registries.equipment.get("tablet_press")
        assert {"vibration", "motor_current", "turret_speed"} <= press.tags

    def test_remove_drops_an_inherited_tag(self, registries):
        """cartoner removes the sealing tags it does not have."""
        cartoner = registries.equipment.get("cartoner")
        assert "sealing_temperature" not in cartoner.tags
        assert "line_speed" in cartoner.tags

    def test_override_patches_a_field_only(self, registries):
        cartoner = registries.equipment.get("cartoner")
        line_speed = next(s for s in cartoner.sensors if s.tag == "line_speed")
        base = next(
            s
            for s in registries.equipment.get("blister_packaging_machine").sensors
            if s.tag == "line_speed"
        )
        assert line_speed.baseline != base.baseline
        assert line_speed.rho == base.rho  # untouched fields inherit

    def test_inline_addition_of_a_tag_the_profile_lacks(self, registries):
        wet = registries.equipment.get("wet_granulator")
        assert "binder_flow_rate" in wet.tags
        assert "pressure" not in wet.tags  # removed by the class

    def test_class_with_no_profile_is_inline_only(self, registries):
        scale = registries.equipment.get("weighing_scale")
        assert scale.spec.sensor_profile is None
        assert "dispensed_weight" in scale.tags

    def test_resolution_order_is_deterministic(self, config, registries):
        from pharma_sim.registry.equipment import EquipmentRegistry

        again = EquipmentRegistry(config.machines, config.sensors)
        for class_id in registries.equipment.ids:
            assert [s.tag for s in registries.equipment.get(class_id).sensors] == [
                s.tag for s in again.get(class_id).sensors
            ]


class TestStateMachine:
    def test_starts_in_the_configured_initial_state(self, plant, registries):
        machine = next(iter(plant.machines.values()))
        assert machine.state == registries.states.initial

    def test_legal_transition_is_accepted(self, plant, registries):
        machine = plant.machine("TP-001")
        machine.state = registries.states.initial
        assert machine.transition_to("IDLE", START, "TEST")
        assert machine.state == "IDLE"

    def test_illegal_transition_raises(self, plant):
        machine = plant.machine("TP-002")
        machine.state = "FAULT"
        machine.state_since = START
        with pytest.raises(IllegalTransition) as excinfo:
            machine.transition_to("RUNNING", START + timedelta(minutes=1), "TEST")
        assert "FAULT -> RUNNING" in str(excinfo.value)

    def test_illegal_transition_is_refused_quietly_when_not_strict(self, plant):
        machine = plant.machine("TP-003")
        machine.state = "FAULT"
        machine.state_since = START
        assert not machine.transition_to(
            "RUNNING", START + timedelta(minutes=1), "TEST", strict=False
        )
        assert machine.state == "FAULT"

    def test_routing_walks_the_graph_legally(self, plant, registries):
        machine = plant.machine("TP-004")
        machine.state = "FAULT"
        machine.state_since = START
        assert machine.force_route_to("RUNNING", START + timedelta(minutes=1), "TEST")
        assert machine.state == "RUNNING"
        visited = [interval.state for interval in machine.state_history]
        # It cannot have jumped: MAINTENANCE or IDLE must appear on the way.
        assert "FAULT" in visited
        assert any(state in visited for state in ("IDLE", "MAINTENANCE"))

    def test_state_history_records_durations(self, plant):
        machine = plant.machine("TP-005")
        machine.state = "IDLE"
        machine.state_since = START
        machine._last_accrual = START
        machine.transition_to("STARTING", START + timedelta(minutes=30), "TEST")
        interval = machine.state_history[-1]
        assert interval.state == "IDLE"
        assert interval.seconds == pytest.approx(1800.0)

    def test_transition_to_the_same_state_is_a_no_op(self, plant):
        machine = plant.machine("TP-006")
        machine.state = "IDLE"
        assert not machine.transition_to("IDLE", START, "TEST")

    def test_roles_drive_behaviour_not_names(self, registries):
        """Downstream logic must read roles; these are the accessors it uses."""
        states = registries.states
        assert states.is_productive(states.first("productive"))
        assert states.is_downtime(states.first("downtime"))
        assert states.is_fault(states.first("fault"))
        assert not states.is_productive(states.first("offline"))

    def test_unreachable_state_would_be_caught_by_the_linter(self, registries):
        for state_id in registries.states.ids:
            if state_id == registries.states.initial:
                continue
            assert registries.states.path_exists(registries.states.initial, state_id)


class TestSensorModel:
    def _model(self, plant, machine_id="TP-001", tag="temperature"):
        return plant.machine(machine_id).sensors[tag]

    def _series(self, model, ambient, *, state="RUNNING", health=0.0, n=240, step=60):
        values = []
        moment = START
        for _ in range(n):
            reading = model.sample(
                moment, state=state, health=health, ambient=ambient, run_id="R"
            )
            if reading is not None:
                values.append(reading.value)
            moment += timedelta(seconds=step)
        return values

    @pytest.fixture
    def ambient(self, config):
        return Environment(config.plant.ambient, RngRegistry(1).stream("amb")).at(START)

    def test_readings_carry_full_identity(self, plant, ambient):
        model = self._model(plant)
        reading = model.sample(
            START, state="RUNNING", health=0.0, ambient=ambient, run_id="RUN-1"
        )
        assert reading is not None
        assert reading.machine_id == "TP-001"
        assert reading.sensor_id == "TP-001:temperature"
        assert reading.unit_id == plant.machine("TP-001").unit_id
        assert reading.run_id == "RUN-1"
        assert reading.quality in {Quality.GOOD, Quality.UNCERTAIN, Quality.BAD}

    def test_values_are_autocorrelated_not_independent(self, plant, ambient):
        """The defining property of industrial time-series data."""
        model = self._model(plant)
        model.reset()
        series = self._series(model, ambient, n=400)
        mean = statistics.fmean(series)
        centred = [v - mean for v in series]
        variance = sum(v * v for v in centred)
        lag1 = sum(centred[i] * centred[i + 1] for i in range(len(centred) - 1))
        autocorrelation = lag1 / variance
        assert autocorrelation > 0.5, f"lag-1 autocorrelation only {autocorrelation:.3f}"

    def test_state_change_shifts_the_distribution(self, plant, ambient):
        model = self._model(plant, tag="motor_current")
        model.reset()
        running = statistics.fmean(self._series(model, ambient, state="RUNNING", n=120))
        model.reset()
        idle = statistics.fmean(self._series(model, ambient, state="IDLE", n=120))
        assert idle < running * 0.4

    def test_health_raises_coupled_tags_together(self, plant, ambient):
        """Vibration, current and temperature must move together, not apart."""
        machine = plant.machine("TP-001")
        healthy, sick = {}, {}
        for tag in ("vibration", "motor_current", "temperature"):
            model = machine.sensors[tag]
            model.reset()
            healthy[tag] = statistics.fmean(
                self._series(model, ambient, health=0.0, n=90)
            )
            model.reset()
            sick[tag] = statistics.fmean(self._series(model, ambient, health=1.0, n=90))
        for tag in healthy:
            assert sick[tag] > healthy[tag], f"{tag} did not rise with degradation"

    def test_values_stay_within_the_instrument_range(self, plant, ambient):
        machine = plant.machine("TP-001")
        for tag, model in machine.sensors.items():
            if model.is_derived:
                continue
            model.reset()
            for value in self._series(model, ambient, n=60):
                if model.spec.hard_min is not None:
                    assert value >= model.spec.hard_min, tag
                if model.spec.hard_max is not None:
                    assert value <= model.spec.hard_max, tag

    def test_drift_is_bounded_by_its_limit(self, plant, ambient):
        model = self._model(plant, tag="motor_current")
        model.reset()
        self._series(model, ambient, n=2000, step=600)
        assert abs(model.drift) <= model.spec.drift_limit + 1e-9

    def test_setpoint_moves_the_mean_and_the_limits(self, plant, ambient):
        model = self._model(plant, tag="main_compression_force")
        model.reset()
        at_baseline = statistics.fmean(self._series(model, ambient, n=80))
        model.set_setpoint(model.spec.baseline * 1.2)
        model.reset()
        at_setpoint = statistics.fmean(self._series(model, ambient, n=80))
        assert at_setpoint > at_baseline * 1.1
        # Limits scale with the recipe, so a valid product is not in alarm.
        assert model.limit_scale == pytest.approx(1.2)
        kind, _ = model.evaluate_limits(model.spec.baseline * 1.2)
        assert kind is None
        model.set_setpoint(None)

    def test_limits_are_not_checked_on_a_stopped_machine(self, plant):
        model = self._model(plant, tag="turret_speed")
        assert model.is_meaningful_in("RUNNING")
        assert not model.is_meaningful_in("IDLE")
        assert not model.is_meaningful_in("OFFLINE")

    def test_alarm_latch_fires_once_per_excursion(self, plant):
        model = self._model(plant, tag="motor_current")
        assert model.latch_alarm(True) is True
        assert model.latch_alarm(True) is False
        assert model.latch_alarm(False) is False
        assert model.latch_alarm(True) is True

    def test_derived_tag_mirrors_machine_state(self, plant, ambient):
        model = plant.machine("TP-001").sensors["production_rate"]
        reading = model.sample(
            START,
            state="RUNNING",
            health=0.0,
            ambient=ambient,
            derived_values={"production_rate": 12345.0},
            run_id="R",
        )
        assert reading is not None and reading.value == pytest.approx(12345.0)

    def test_repeatable_for_a_given_stream(self, plant, config, registries):
        from pharma_sim.domain.sensor import SensorModel

        spec = plant.machine("TP-001").sensors["vibration"].spec
        ambient = Environment(config.plant.ambient, RngRegistry(1).stream("a")).at(START)

        def run() -> list[float]:
            model = SensorModel(
                spec,
                machine_id="TP-001",
                unit_id="UNIT-06",
                states=registries.states,
                rng=RngRegistry(42).child("sensor", "TP-001", "vibration"),
            )
            return self._series(model, ambient, n=50)

        assert run() == run()


class TestShiftScheduler:
    def test_all_configured_shifts_are_known(self, config):
        scheduler = ShiftScheduler(config.shifts, "PLANT-01")
        assert set(scheduler.codes) == {spec.code for spec in config.shifts.shifts}

    def test_day_shift_bounds(self, config):
        scheduler = ShiftScheduler(config.shifts, "PLANT-01")
        start, end = scheduler.bounds(date(2026, 1, 1), "A")
        assert start == datetime(2026, 1, 1, 6, 0)
        assert end == datetime(2026, 1, 1, 14, 0)

    def test_night_shift_crosses_midnight(self, config):
        scheduler = ShiftScheduler(config.shifts, "PLANT-01")
        start, end = scheduler.bounds(date(2026, 1, 1), "C")
        assert start == datetime(2026, 1, 1, 22, 0)
        assert end == datetime(2026, 1, 2, 6, 0)
        assert (end - start) == timedelta(hours=8)

    def test_instant_after_midnight_resolves_to_the_night_shift(self, config):
        scheduler = ShiftScheduler(config.shifts, "PLANT-01")
        resolved = scheduler.shift_for(datetime(2026, 1, 2, 2, 30))
        assert resolved == (date(2026, 1, 1), "C")

    def test_every_instant_of_a_day_belongs_to_exactly_one_shift(self, config):
        scheduler = ShiftScheduler(config.shifts, "PLANT-01")
        moment = datetime(2026, 1, 5, 0, 0)
        for _ in range(24 * 4):
            assert scheduler.shift_for(moment) is not None
            moment += timedelta(minutes=15)

    def test_night_shift_break_after_midnight_lands_on_the_next_day(self, config):
        scheduler = ShiftScheduler(config.shifts, "PLANT-01")
        instance = scheduler.instance(date(2026, 1, 1), "C", "SHF-1")
        labels = {window.label: window for window in instance.breaks}
        assert labels["TEA"].start == datetime(2026, 1, 2, 0, 30)
        assert all(instance.start <= w.start < instance.end for w in instance.breaks)

    def test_shift_chain_is_contiguous(self, config):
        scheduler = ShiftScheduler(config.shifts, "PLANT-01")
        starts = scheduler.upcoming_starts(datetime(2026, 1, 1, 5, 0), horizon_days=1)
        assert starts[0][0] == datetime(2026, 1, 1, 6, 0)
        assert [s[2] for s in starts[:3]] == ["A", "B", "C"]


class TestEmployee:
    def test_inexperience_ranks_by_skill(self, config):
        def make(skill: str, years: float) -> Employee:
            return Employee(
                employee_id="E",
                name="N",
                plant_id="P",
                unit_id="U",
                role="OPERATOR",
                skill_level=skill,
                shift_code="A",
                experience_years=years,
                attendance_probability=0.95,
            )

        junior = make("JUNIOR", 0.5)
        senior = make("SENIOR", 15.0)
        assert junior.inexperience > senior.inexperience
        assert 0.0 <= senior.inexperience <= 1.0

    def test_tenure_reduces_inexperience_within_a_level(self, config):
        base = dict(
            employee_id="E",
            name="N",
            plant_id="P",
            unit_id="U",
            role="OPERATOR",
            skill_level="INTERMEDIATE",
            shift_code="A",
            attendance_probability=0.95,
        )
        assert Employee(**base, experience_years=2.0).inexperience > Employee(
            **base, experience_years=8.0
        ).inexperience


class TestHireDates:
    """A workforce is not hired on the morning the simulation starts."""

    def test_hire_dates_are_spread_not_uniform(self, plant, config):
        dates = {e.hired_on.date() for e in plant.employees.values()}
        # The bug this guards against gave every employee the same date, making
        # any tenure- or cohort-based query on the dataset degenerate.
        assert len(dates) > len(plant.employees) // 2

    def test_nobody_predates_the_shortest_tenure(self, plant, config):
        floor = config.units.tenure_years_min
        for employee in plant.employees.values():
            tenure = (START - employee.hired_on).days / 365.25
            assert tenure >= floor - 1e-6, employee.employee_id

    def test_site_tenure_never_exceeds_industry_experience(self, plant, config):
        """Nobody has worked at this site longer than they have worked at all."""
        for employee in plant.employees.values():
            tenure = (START - employee.hired_on).days / 365.25
            assert tenure <= employee.experience_years + 1e-6, employee.employee_id

    def test_tenure_respects_the_configured_ceiling(self, plant, config):
        ceiling = config.units.tenure_years_max
        for employee in plant.employees.values():
            tenure = (START - employee.hired_on).days / 365.25
            assert tenure <= ceiling + 1e-6, employee.employee_id

    def test_hire_dates_are_reproducible(self, config, registries):
        first = FactoryBuilder(config, registries, RngRegistry(42)).build(START)
        second = FactoryBuilder(config, registries, RngRegistry(42)).build(START)
        assert [e.hired_on for e in first.employees.values()] == [
            e.hired_on for e in second.employees.values()
        ]

    def test_retuning_tenure_leaves_the_rest_of_the_workforce_alone(
        self, config, registries
    ):
        """Tenure draws from its own RNG stream, so widening the range moves
        hire dates and nothing else.

        Were it sharing the workforce stream, raising tenure_years_max would
        silently re-roll every name, skill and attendance value in the plant --
        a config knob for hire dates quietly rewriting unrelated columns.
        """
        base = FactoryBuilder(config, registries, RngRegistry(42)).build(START)
        widened = config.model_copy(
            update={"units": config.units.model_copy(update={"tenure_years_max": 3.0})},
            deep=True,
        )
        other = FactoryBuilder(
            widened, Registries.build(widened), RngRegistry(42)
        ).build(START)

        assert [e.name for e in other.employees.values()] == [
            e.name for e in base.employees.values()
        ]
        assert [e.experience_years for e in other.employees.values()] == [
            e.experience_years for e in base.employees.values()
        ]
        assert [e.attendance_probability for e in other.employees.values()] == [
            e.attendance_probability for e in base.employees.values()
        ]
        # The knob did do something.
        assert [e.hired_on for e in other.employees.values()] != [
            e.hired_on for e in base.employees.values()
        ]


class TestOee:
    def test_identity_holds(self):
        window = ProductionWindow(
            planned_quantity=1000.0,
            actual_quantity=900.0,
            good_quantity=850.0,
            reject_quantity=50.0,
            runtime_seconds=7200.0,
            idle_seconds=1800.0,
            downtime_seconds=900.0,
        )
        oee = compute_oee(window)
        assert oee.oee == pytest.approx(oee.availability * oee.performance * oee.quality)

    def test_availability_excludes_planned_stops(self):
        window = ProductionWindow(
            planned_quantity=100.0,
            actual_quantity=100.0,
            good_quantity=100.0,
            runtime_seconds=3600.0,
            planned_stop_seconds=3600.0,
        )
        assert compute_oee(window).availability == pytest.approx(1.0)

    def test_downtime_reduces_availability(self):
        window = ProductionWindow(
            planned_quantity=100.0,
            actual_quantity=100.0,
            good_quantity=100.0,
            runtime_seconds=3600.0,
            downtime_seconds=3600.0,
        )
        assert compute_oee(window).availability == pytest.approx(0.5)

    def test_rejects_reduce_quality(self):
        window = ProductionWindow(
            planned_quantity=100.0,
            actual_quantity=100.0,
            good_quantity=90.0,
            reject_quantity=10.0,
            runtime_seconds=3600.0,
        )
        assert compute_oee(window).quality == pytest.approx(0.9)

    def test_all_components_are_bounded(self):
        window = ProductionWindow(
            planned_quantity=10.0,
            actual_quantity=1000.0,
            good_quantity=1000.0,
            runtime_seconds=3600.0,
        )
        oee = compute_oee(window)
        assert 0.0 <= oee.performance <= 1.0
        assert 0.0 <= oee.oee <= 1.0

    def test_idle_machine_has_zero_oee_not_an_error(self):
        oee = compute_oee(ProductionWindow(idle_seconds=3600.0))
        assert oee.oee == 0.0

    def test_aggregation_sums_components(self):
        a = ProductionWindow(good_quantity=10.0, actual_quantity=12.0, runtime_seconds=100.0)
        b = ProductionWindow(good_quantity=20.0, actual_quantity=25.0, runtime_seconds=200.0)
        total = aggregate_windows([a, b])
        assert total.good_quantity == 30.0
        assert total.actual_quantity == 37.0
        assert total.runtime_seconds == 300.0


class TestEnvironment:
    def test_diurnal_cycle_varies_over_the_day(self, config):
        env = Environment(config.plant.ambient, RngRegistry(1).stream("e"))
        temps = [
            env.at(datetime(2026, 6, 1, hour, 0)).temperature_c for hour in range(24)
        ]
        assert max(temps) - min(temps) > 1.0

    def test_sampling_does_not_change_the_trajectory(self, config):
        """No RNG is drawn in at(), so sampling rate cannot alter the weather."""
        env = Environment(config.plant.ambient, RngRegistry(1).stream("e"))
        moment = datetime(2026, 6, 1, 9, 0)
        first = env.at(moment).temperature_c
        for _ in range(100):
            env.at(moment)
        assert env.at(moment).temperature_c == first

    def test_humidity_moves_against_temperature(self, config):
        env = Environment(config.plant.ambient, RngRegistry(1).stream("e"))
        warm = env.at(datetime(2026, 6, 1, 15, 0))
        cool = env.at(datetime(2026, 6, 1, 3, 0))
        assert warm.temperature_c > cool.temperature_c
        assert warm.humidity_pct < cool.humidity_pct

    def test_forced_excursion_raises_stress(self, config):
        env = Environment(config.plant.ambient, RngRegistry(1).stream("e"))
        moment = datetime(2026, 6, 1, 9, 0)
        calm = env.at(moment).stress
        env.force_excursion(moment, "TEMPERATURE", 8.0, 4.0)
        assert env.at(moment).stress > calm
