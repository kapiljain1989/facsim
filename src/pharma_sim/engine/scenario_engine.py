"""Scenario engine (§39).

A scenario is a duration, a set of temporary config overrides, and a list of
timed interventions. Interventions go through the same machinery as
naturally-arising events, so an injected failure still incubates, raises
precursors, warns, faults, spoils quality and drives maintenance, deviation, RCA
and CAPA. Nothing is short-circuited.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from pharma_sim.config.models import ScenarioAction, ScenarioSpec
from pharma_sim.engine.scheduler import Priority
from pharma_sim.simulator import SimulationSummary, Simulator

__all__ = ["ScenarioEngine"]

logger = logging.getLogger(__name__)


class ScenarioEngine:
    """Applies a scenario's overrides and interventions to a simulator."""

    def __init__(self, simulator: Simulator) -> None:
        self._sim = simulator

    # ------------------------------------------------------------------ running
    def run(self, spec: ScenarioSpec) -> SimulationSummary:
        self._apply_overrides(spec)
        self._sim.start()
        for index, action in enumerate(spec.actions):
            self._schedule(spec, action, index)
        return self._sim.run(hours=spec.duration_hours)

    def _apply_overrides(self, spec: ScenarioSpec) -> None:
        """Patch dotted config paths for the scenario's duration.

        Applied to the loaded configuration objects rather than to the YAML, so
        the files on disk are untouched and the run remains reproducible from the
        scenario id plus the seed.
        """
        for path, value in spec.overrides.items():
            target: Any = self._sim.config
            parts = path.split(".")
            for part in parts[:-1]:
                target = getattr(target, part)
            field = parts[-1]
            if not hasattr(target, field):
                raise KeyError(
                    f"scenario {spec.id!r} override {path!r} does not name a "
                    f"configuration field"
                )
            # Config models are frozen, so mutate through the underlying dict.
            object.__setattr__(target, field, value)
            logger.info("scenario %s override: %s = %r", spec.id, path, value)

    def _schedule(self, spec: ScenarioSpec, action: ScenarioAction, index: int) -> None:
        when = self._sim.clock.start_time + timedelta(hours=action.at_hours)
        self._sim.scheduler.at(
            when,
            lambda now: self._apply(spec, action, now),
            priority=Priority.FAILURE,
            label=f"scenario:{spec.id}:{index}",
        )

    # ------------------------------------------------------------------ actions
    def _apply(self, spec: ScenarioSpec, action: ScenarioAction, now: datetime) -> None:
        handler = getattr(self, f"_do_{action.type}", None)
        if handler is None:
            logger.warning("scenario %s: no handler for action %s", spec.id, action.type)
            return
        try:
            detail = handler(action, now) or {}
        except (KeyError, ValueError) as exc:
            logger.warning("scenario %s action %s failed: %s", spec.id, action.type, exc)
            return
        self._sim.bus.publish(
            "SCENARIO_ACTION_APPLIED",
            now,
            unit_id=action.unit_id,
            machine_id=action.machine_id,
            payload={"scenario_id": spec.id, "action_type": action.type, **detail},
        )

    def _targets(self, action: ScenarioAction) -> list[str]:
        """Machines an action applies to, from the most specific selector given."""
        plant = self._sim.plant
        if action.machine_id:
            return [plant.machine(action.machine_id).machine_id]
        if action.equipment_class:
            machines = plant.machines_of_class(action.equipment_class)
            if action.unit_id:
                machines = [m for m in machines if m.unit_id == action.unit_id]
            if not machines:
                raise KeyError(
                    f"no machines of class {action.equipment_class!r}"
                    + (f" in {action.unit_id}" if action.unit_id else "")
                )
        elif action.unit_id:
            machines = plant.machines_in(action.unit_id)
        else:
            machines = list(plant.machines.values())
        # Prefer the busiest machines, so an injected fault lands where it will
        # actually affect a batch rather than on an idle spare.
        machines.sort(key=lambda m: (m.current_batch_id is None, m.machine_id))
        return [m.machine_id for m in machines[: action.count]]

    def _do_inject_failure(self, action: ScenarioAction, now: datetime) -> dict[str, Any]:
        if not action.failure_mode:
            raise ValueError("inject_failure requires failure_mode")
        injected = []
        for machine_id in self._targets(action):
            episode = self._sim.failures.inject(
                machine_id,
                action.failure_mode,
                now,
                severity=action.severity,
                incubation_hours=action.params.get("incubation_hours"),
            )
            injected.append({"machine_id": machine_id, "episode_id": episode.episode_id})
        return {"injected": injected}

    def _do_inject_failure_random(
        self, action: ScenarioAction, now: datetime
    ) -> dict[str, Any]:
        """Inject whatever mode the hazard model considers most likely here."""
        rng = self._sim._rngs.child("scenario-random", action.type, str(action.at_hours))
        results = []
        for machine_id in self._targets(action):
            machine = self._sim.plant.machine(machine_id)
            modes = [
                mode
                for mode in self._sim.registries.failures.for_class(machine.equipment_class)
                if mode.detectable
            ]
            if not modes:
                continue
            mode = rng.choice(modes)
            episode = self._sim.failures.initiate(machine, mode, now, injected=True)
            results.append({"machine_id": machine_id, "failure_mode": mode.id,
                            "episode_id": episode.episode_id})
        return {"injected": results}

    def _do_force_state(self, action: ScenarioAction, now: datetime) -> dict[str, Any]:
        state = action.params["state"]
        moved = []
        for machine_id in self._targets(action):
            machine = self._sim.plant.machine(machine_id)
            machine.accrue_time(now)
            if machine.force_route_to(state, now, f"SCENARIO:{action.type}"):
                moved.append(machine_id)
        return {"state": state, "machines": moved}

    def _do_material_shortage(self, action: ScenarioAction, now: datetime) -> dict[str, Any]:
        material_id = action.params.get("material_id")
        if not material_id:
            raise ValueError("material_shortage requires params.material_id")
        self._sim.batches.block_material(material_id)
        self._sim.bus.publish(
            "MATERIAL_SHORTAGE",
            now,
            unit_id=action.unit_id,
            severity="MAJOR",
            payload={"material_id": material_id},
        )
        hours = float(action.params.get("duration_hours", 4.0))
        self._sim.scheduler.at(
            now + timedelta(hours=hours),
            lambda moment: self._sim.batches.unblock_material(material_id),
            priority=Priority.BATCH,
            label=f"material-restored:{material_id}",
        )
        return {"material_id": material_id, "duration_hours": hours}

    def _do_ambient_excursion(self, action: ScenarioAction, now: datetime) -> dict[str, Any]:
        kind = action.params.get("kind", "TEMPERATURE")
        delta = float(action.params.get("delta", 6.0))
        hours = float(action.params.get("duration_hours", 4.0))
        self._sim.environment.force_excursion(now, kind, delta, hours)
        self._sim.bus.publish(
            "AMBIENT_EXCURSION_STARTED",
            now,
            severity="MINOR",
            payload={"kind": kind, "delta": delta},
        )
        return {"kind": kind, "delta": delta, "duration_hours": hours}

    def _do_power_interruption(self, action: ScenarioAction, now: datetime) -> dict[str, Any]:
        hours = float(action.params.get("duration_hours", 1.0))
        affected = []
        for machine_id in self._targets(action) or list(self._sim.plant.machines):
            machine = self._sim.plant.machine(machine_id)
            mode = self._sim.registries.failures.applicable(
                machine.equipment_class, "POWER_INTERRUPTION"
            )
            if mode is None:
                continue
            self._sim.failures.initiate(
                machine, mode, now, injected=True, incubation_hours=0.05
            )
            affected.append(machine_id)
        return {"duration_hours": hours, "machines": len(affected)}

    def _do_operator_error(self, action: ScenarioAction, now: datetime) -> dict[str, Any]:
        errored = []
        for machine_id in self._targets(action):
            machine = self._sim.plant.machine(machine_id)
            mode = self._sim.registries.failures.applicable(
                machine.equipment_class, "OPERATOR_ERROR"
            )
            if mode is None:
                continue
            self._sim.failures.initiate(
                machine, mode, now, injected=True, incubation_hours=0.25
            )
            errored.append(machine_id)
        return {"machines": errored}

    def _do_sensor_malfunction(self, action: ScenarioAction, now: datetime) -> dict[str, Any]:
        affected = []
        for machine_id in self._targets(action):
            machine = self._sim.plant.machine(machine_id)
            for mode_id in (
                "VIBRATION_SENSOR_FAILURE",
                "TEMPERATURE_SENSOR_FAILURE",
                "PRESSURE_SENSOR_FAILURE",
            ):
                mode = self._sim.registries.failures.applicable(
                    machine.equipment_class, mode_id
                )
                if mode is not None:
                    self._sim.failures.initiate(machine, mode, now, injected=True)
                    affected.append({"machine_id": machine_id, "failure_mode": mode_id})
                    break
        return {"injected": affected}

    def _do_defer_maintenance(self, action: ScenarioAction, now: datetime) -> dict[str, Any]:
        hours = float(action.params.get("hours", 72.0))
        for machine in self._sim.plant.machines.values():
            machine.defer_pm(hours)
        return {"hours": hours, "machines": len(self._sim.plant.machines)}

    def _do_set_demand(self, action: ScenarioAction, now: datetime) -> dict[str, Any]:
        multiplier = float(action.params.get("multiplier", 1.0))
        self._sim.batches.set_demand_multiplier(multiplier)
        self._sim.batches.replenish(now)
        return {"multiplier": multiplier}
