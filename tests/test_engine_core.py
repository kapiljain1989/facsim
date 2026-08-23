"""Clock, scheduler, event bus, RNG and ID generation."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from pharma_sim.engine.clock import ClockMode, ClockState, SimulationClock
from pharma_sim.engine.event_bus import ALL_EVENTS, EventBus
from pharma_sim.engine.ids import IdFactory
from pharma_sim.engine.rng import (
    RngRegistry,
    probability_from_rate,
    truncated_normal,
    weibull_hazard,
)
from pharma_sim.engine.scheduler import Priority, Scheduler

START = datetime(2026, 1, 1, 6, 0, 0)


class TestClock:
    def test_starts_stopped_at_the_start_instant(self):
        clock = SimulationClock(START)
        assert clock.state is ClockState.STOPPED
        assert clock.now == START
        assert clock.elapsed_hours == 0.0

    def test_lifecycle_transitions(self):
        clock = SimulationClock(START)
        clock.start()
        assert clock.is_running
        clock.pause()
        assert clock.state is ClockState.PAUSED
        clock.resume()
        assert clock.is_running
        clock.stop()
        assert clock.state is ClockState.STOPPED

    def test_advance_accumulates_simulated_time(self):
        clock = SimulationClock(START)
        clock.start()
        clock.advance(timedelta(hours=5))
        clock.advance(timedelta(minutes=30))
        assert clock.now == START + timedelta(hours=5, minutes=30)
        assert clock.elapsed_hours == pytest.approx(5.5)

    def test_time_never_moves_backwards(self):
        clock = SimulationClock(START)
        clock.advance(timedelta(hours=3))
        clock.advance_to(START)  # earlier target is ignored
        assert clock.now == START + timedelta(hours=3)

    def test_negative_advance_is_rejected(self):
        clock = SimulationClock(START)
        with pytest.raises(ValueError):
            clock.advance(timedelta(hours=-1))

    def test_reset_returns_to_the_start(self):
        clock = SimulationClock(START)
        clock.start()
        clock.advance(timedelta(days=2))
        clock.reset()
        assert clock.now == START
        assert clock.state is ClockState.STOPPED

    def test_fast_forward_never_sleeps(self):
        slept: list[float] = []
        clock = SimulationClock(
            START, mode=ClockMode.FAST_FORWARD, sleeper=slept.append
        )
        clock.start()
        clock.advance(timedelta(days=30))
        assert slept == []

    def test_paced_mode_sleeps_by_the_configured_ratio(self):
        """1 real second per 60 simulated minutes means 2 h costs 2 s."""
        slept: list[float] = []
        fake_time = [0.0]
        clock = SimulationClock(
            START,
            mode=ClockMode.PACED,
            sim_minutes_per_real_second=60.0,
            monotonic=lambda: fake_time[0],
            sleeper=slept.append,
        )
        clock.start()
        clock.advance(timedelta(hours=2))
        assert sum(slept) == pytest.approx(2.0, abs=1e-6)

    def test_speed_ratio_scales_the_sleep(self):
        slept: list[float] = []
        clock = SimulationClock(
            START,
            mode=ClockMode.PACED,
            sim_minutes_per_real_second=600.0,
            monotonic=lambda: 0.0,
            sleeper=slept.append,
        )
        clock.start()
        clock.advance(timedelta(hours=10))
        assert sum(slept) == pytest.approx(1.0, abs=1e-6)

    def test_zero_speed_is_rejected(self):
        with pytest.raises(ValueError):
            SimulationClock(START, sim_minutes_per_real_second=0.0)

    def test_mode_can_change_mid_run(self):
        clock = SimulationClock(START)
        clock.start()
        clock.set_mode(ClockMode.PACED, 120.0)
        assert clock.mode is ClockMode.PACED


class TestScheduler:
    def test_runs_tasks_in_time_order(self):
        scheduler = Scheduler()
        seen: list[str] = []
        scheduler.at(START + timedelta(hours=2), lambda now: seen.append("b"))
        scheduler.at(START + timedelta(hours=1), lambda now: seen.append("a"))
        scheduler.at(START + timedelta(hours=3), lambda now: seen.append("c"))
        scheduler.run_until(START + timedelta(hours=5), lambda when: None)
        assert seen == ["a", "b", "c"]

    def test_priority_orders_tasks_at_the_same_instant(self):
        scheduler = Scheduler()
        seen: list[str] = []
        at = START + timedelta(hours=1)
        scheduler.at(at, lambda now: seen.append("sensor"), priority=Priority.SENSOR)
        scheduler.at(at, lambda now: seen.append("shift"), priority=Priority.SHIFT)
        scheduler.at(at, lambda now: seen.append("machine"), priority=Priority.MACHINE)
        scheduler.run_until(at, lambda when: None)
        assert seen == ["shift", "machine", "sensor"]

    def test_equal_priority_keeps_insertion_order(self):
        """Reproducibility depends on this: no dependence on heap internals."""
        scheduler = Scheduler()
        seen: list[int] = []
        at = START + timedelta(hours=1)
        for index in range(20):
            scheduler.at(at, lambda now, i=index: seen.append(i), priority=Priority.MACHINE)
        scheduler.run_until(at, lambda when: None)
        assert seen == list(range(20))

    def test_horizon_is_respected(self):
        scheduler = Scheduler()
        seen: list[str] = []
        scheduler.at(START + timedelta(hours=1), lambda now: seen.append("in"))
        scheduler.at(START + timedelta(hours=9), lambda now: seen.append("out"))
        scheduler.run_until(START + timedelta(hours=5), lambda when: None)
        assert seen == ["in"]
        assert scheduler.pending == 1

    def test_cancelled_tasks_do_not_run(self):
        scheduler = Scheduler()
        seen: list[str] = []
        task = scheduler.at(START + timedelta(hours=1), lambda now: seen.append("x"))
        task.cancel()
        scheduler.run_until(START + timedelta(hours=2), lambda when: None)
        assert seen == []

    def test_callbacks_observe_their_scheduled_instant(self):
        scheduler = Scheduler()
        clock = SimulationClock(START)
        observed: list[datetime] = []
        target = START + timedelta(hours=7)
        scheduler.at(target, lambda now: observed.append(clock.now))
        scheduler.run_until(target, clock.advance_to)
        assert observed == [target]

    def test_tasks_may_schedule_more_tasks(self):
        scheduler = Scheduler()
        count = [0]

        def repeat(now: datetime) -> None:
            count[0] += 1
            if count[0] < 5:
                scheduler.at(now + timedelta(hours=1), repeat)

        scheduler.at(START, repeat)
        scheduler.run_until(START + timedelta(hours=10), lambda when: None)
        assert count[0] == 5


class TestEventBus:
    def _bus(self, registries) -> tuple[EventBus, IdFactory]:
        ids = IdFactory()
        return (
            EventBus(
                registries.event_types,
                plant_id="PLANT-01",
                run_id="RUN-0001",
                next_id=ids.event,
            ),
            ids,
        )

    def test_publishes_to_a_typed_subscriber(self, registries):
        bus, _ = self._bus(registries)
        received = []
        bus.subscribe("SHIFT_STARTED", received.append)
        bus.publish(
            "SHIFT_STARTED",
            START,
            payload={"shift_code": "A", "shift_instance_id": "SHF-000001"},
        )
        assert len(received) == 1
        assert received[0].event_type == "SHIFT_STARTED"
        assert received[0].plant_id == "PLANT-01"
        assert received[0].run_id == "RUN-0001"

    def test_wildcard_subscriber_sees_everything(self, registries):
        bus, _ = self._bus(registries)
        received = []
        bus.subscribe(ALL_EVENTS, received.append)
        bus.publish(
            "SHIFT_STARTED",
            START,
            payload={"shift_code": "A", "shift_instance_id": "S"},
        )
        bus.publish("SHIFT_ENDED", START, payload={"shift_code": "A", "shift_instance_id": "S"})
        assert len(received) == 2

    def test_undeclared_event_type_is_rejected(self, registries):
        bus, _ = self._bus(registries)
        with pytest.raises(KeyError):
            bus.publish("NOT_DECLARED", START, payload={})

    def test_missing_required_payload_field_is_rejected(self, registries):
        bus, _ = self._bus(registries)
        with pytest.raises(ValueError) as excinfo:
            bus.publish("SHIFT_STARTED", START, payload={"shift_code": "A"})
        assert "shift_instance_id" in str(excinfo.value)

    def test_cannot_subscribe_to_an_undeclared_type(self, registries):
        bus, _ = self._bus(registries)
        with pytest.raises(KeyError):
            bus.subscribe("NOT_DECLARED", lambda event: None)

    def test_default_severity_comes_from_the_declaration(self, registries):
        bus, _ = self._bus(registries)
        received = []
        bus.subscribe("MACHINE_FAILURE", received.append)
        bus.publish(
            "MACHINE_FAILURE",
            START,
            payload={"failure_id": "F", "failure_mode": "M", "category": "MECHANICAL"},
        )
        assert received[0].severity == "MAJOR"

    def test_a_failing_subscriber_does_not_stop_the_others(self, registries):
        bus, _ = self._bus(registries)
        seen = []
        bus.subscribe("SHIFT_ENDED", lambda event: (_ for _ in ()).throw(RuntimeError("boom")))
        bus.subscribe("SHIFT_ENDED", seen.append)
        bus.publish("SHIFT_ENDED", START, payload={"shift_code": "A", "shift_instance_id": "S"})
        assert len(seen) == 1

    def test_event_ids_are_sequential_not_random(self, registries):
        bus, _ = self._bus(registries)
        received = []
        bus.subscribe(ALL_EVENTS, received.append)
        for _ in range(3):
            bus.publish(
                "SHIFT_ENDED", START, payload={"shift_code": "A", "shift_instance_id": "S"}
            )
        assert [e.event_id for e in received] == [
            "EVT-000000001",
            "EVT-000000002",
            "EVT-000000003",
        ]

    def test_emitter_self_check_catches_undeclared_types(self, registries):
        from pharma_sim.registry.event_types import UndeclaredEventTypes

        with pytest.raises(UndeclaredEventTypes):
            registries.event_types.verify_emitters(frozenset({"MADE_UP_EVENT"}))

    def test_engine_emits_only_declared_event_types(self, registries):
        """The startup self-check that turns a typo into an immediate failure."""
        from pharma_sim.simulator import EMITTED_EVENT_TYPES

        registries.event_types.verify_emitters(EMITTED_EVENT_TYPES)


class TestRng:
    def test_same_seed_and_name_give_the_same_stream(self):
        a = RngRegistry(42).stream("sensor:TP-001:vibration")
        b = RngRegistry(42).stream("sensor:TP-001:vibration")
        assert [a.random() for _ in range(5)] == [b.random() for _ in range(5)]

    def test_different_names_give_different_streams(self):
        registry = RngRegistry(42)
        a = [registry.stream("one").random() for _ in range(5)]
        b = [registry.stream("two").random() for _ in range(5)]
        assert a != b

    def test_different_seeds_give_different_streams(self):
        a = RngRegistry(1).stream("x").random()
        b = RngRegistry(2).stream("x").random()
        assert a != b

    def test_streams_are_independent_of_draw_order(self):
        """Interleaving must not change what any one stream produces."""
        first = RngRegistry(42)
        a1 = [first.stream("a").random() for _ in range(3)]
        b1 = [first.stream("b").random() for _ in range(3)]

        second = RngRegistry(42)
        b2, a2 = [], []
        for _ in range(3):
            b2.append(second.stream("b").random())
            a2.append(second.stream("a").random())
        assert a1 == a2 and b1 == b2

    def test_child_builds_hierarchical_names(self):
        registry = RngRegistry(42)
        assert registry.child("sensor", "TP-001", "vibration").random() == (
            RngRegistry(42).stream("sensor:TP-001:vibration").random()
        )

    def test_truncated_normal_respects_bounds(self):
        registry = RngRegistry(42)
        stream = registry.stream("t")
        values = [truncated_normal(stream, 0.0, 5.0, low=-1.0, high=1.0) for _ in range(200)]
        assert all(-1.0 <= v <= 1.0 for v in values)

    def test_weibull_hazard_rises_with_age_when_beta_above_one(self):
        early = weibull_hazard(100.0, 4000.0, 2.4)
        late = weibull_hazard(3000.0, 4000.0, 2.4)
        assert late > early * 10

    def test_weibull_hazard_is_flat_when_beta_is_one(self):
        assert weibull_hazard(10.0, 4000.0, 1.0) == pytest.approx(
            weibull_hazard(3000.0, 4000.0, 1.0)
        )

    def test_probability_from_rate_is_bounded(self):
        assert probability_from_rate(0.0, 10.0) == 0.0
        assert 0.0 < probability_from_rate(0.001, 1.0) < 1.0
        assert probability_from_rate(1e6, 1.0) == pytest.approx(1.0)


class TestIdFactory:
    def test_ids_are_sequential_and_padded(self):
        ids = IdFactory()
        assert [ids.next("DEV", width=4) for _ in range(3)] == [
            "DEV-0001",
            "DEV-0002",
            "DEV-0003",
        ]

    def test_dated_ids_embed_the_year(self):
        ids = IdFactory(year=2026)
        assert ids.batch() == "BATCH-2026-000001"

    def test_prefixes_count_independently(self):
        ids = IdFactory()
        ids.failure()
        ids.failure()
        ids.deviation()
        assert ids.count("FAIL") == 2
        assert ids.count("DEV") == 1

    def test_two_factories_do_not_interfere(self):
        first, second = IdFactory(), IdFactory()
        first.event()
        assert second.event() == "EVT-000000001"
