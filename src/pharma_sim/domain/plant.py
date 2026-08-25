"""Plant and unit aggregates, plus the builder that turns config into objects.

The builder is the only place that knows how configuration becomes an object
graph. Because counts, equipment, staffing and instrumentation all come from
config, the same builder produces the shipped 10-unit / 100-machine plant and the
deliberately different two-unit example used to prove schema-agnosticism.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from pharma_sim.config.models import FactoryConfig, UnitSpec
from pharma_sim.domain.employee import Employee
from pharma_sim.domain.machine import Machine
from pharma_sim.domain.sensor import SensorModel
from pharma_sim.engine.rng import RngRegistry
from pharma_sim.registry import Registries

__all__ = ["Unit", "Plant", "FactoryBuilder"]


@dataclass(slots=True)
class Unit:
    """A production unit: its machines, its people and the stage it performs."""

    spec: UnitSpec
    plant_id: str
    machines: list[Machine] = field(default_factory=list)
    manager_ids: list[str] = field(default_factory=list)
    worker_ids: list[str] = field(default_factory=list)

    @property
    def unit_id(self) -> str:
        return self.spec.id

    @property
    def process_stage(self) -> str:
        return self.spec.process_stage

    def as_row(self) -> dict[str, Any]:
        return {
            "unit_id": self.spec.id,
            "plant_id": self.plant_id,
            "name": self.spec.name,
            "sequence": self.spec.sequence,
            "process_stage": self.spec.process_stage,
            "worker_count": len(self.worker_ids),
            "manager_count": len(self.manager_ids),
            "machine_count": len(self.machines),
            "environment_sensitivity": self.spec.environment_sensitivity,
        }


@dataclass(slots=True)
class Plant:
    """The whole factory: units, machines and workforce, indexed for lookup."""

    plant_id: str
    name: str
    location: str
    timezone: str
    units: dict[str, Unit] = field(default_factory=dict)
    machines: dict[str, Machine] = field(default_factory=dict)
    employees: dict[str, Employee] = field(default_factory=dict)
    plant_manager_id: str | None = None

    # ------------------------------------------------------------------ lookups
    def unit(self, unit_id: str) -> Unit:
        try:
            return self.units[unit_id]
        except KeyError:
            raise KeyError(f"unknown unit {unit_id!r}") from None

    def machine(self, machine_id: str) -> Machine:
        try:
            return self.machines[machine_id]
        except KeyError:
            raise KeyError(f"unknown machine {machine_id!r}") from None

    def employee(self, employee_id: str) -> Employee:
        try:
            return self.employees[employee_id]
        except KeyError:
            raise KeyError(f"unknown employee {employee_id!r}") from None

    def machines_in(self, unit_id: str) -> list[Machine]:
        return self.unit(unit_id).machines

    def machines_of_class(self, equipment_class: str) -> list[Machine]:
        return [m for m in self.machines.values() if m.equipment_class == equipment_class]

    def units_for_stage(self, stage: str) -> list[Unit]:
        return [unit for unit in self.units.values() if unit.process_stage == stage]

    def workers_in(self, unit_id: str, shift_code: str) -> list[Employee]:
        return [
            self.employees[eid]
            for eid in self.unit(unit_id).worker_ids
            if self.employees[eid].shift_code == shift_code
        ]

    def employees_on_shift(self, shift_code: str) -> list[Employee]:
        return [emp for emp in self.employees.values() if emp.shift_code == shift_code]

    def technicians(self, role: str) -> list[Employee]:
        return [emp for emp in self.employees.values() if emp.role == role]

    # ------------------------------------------------------------------- summary
    @property
    def machine_count(self) -> int:
        return len(self.machines)

    @property
    def worker_count(self) -> int:
        return sum(len(unit.worker_ids) for unit in self.units.values())

    def as_row(self) -> dict[str, Any]:
        return {
            "plant_id": self.plant_id,
            "name": self.name,
            "location": self.location,
            "timezone": self.timezone,
            "unit_count": len(self.units),
            "machine_count": len(self.machines),
            "employee_count": len(self.employees),
            "plant_manager_id": self.plant_manager_id,
        }


#: Given names and surnames used to generate a plausible workforce. Drawn from a
#: seeded stream, so the roster is identical across runs with the same seed.
_GIVEN_NAMES = (
    "Aarav", "Ananya", "Rohan", "Priya", "Vikram", "Meera", "Arjun", "Kavya",
    "Siddharth", "Divya", "Nikhil", "Sneha", "Karthik", "Pooja", "Rahul",
    "Ishita", "Manish", "Anjali", "Suresh", "Deepa", "Amit", "Neha", "Ravi",
    "Shreya", "Gaurav", "Lakshmi", "Ajay", "Nandini", "Vivek", "Radha",
)
_SURNAMES = (
    "Sharma", "Patel", "Reddy", "Iyer", "Nair", "Desai", "Joshi", "Mehta",
    "Kulkarni", "Rao", "Gupta", "Bhat", "Chauhan", "Pillai", "Sinha", "Verma",
)


class FactoryBuilder:
    """Builds a :class:`Plant` from validated configuration.

    Args:
        config: the validated configuration set.
        registries: runtime registries built from the same config.
        rngs: seeded stream registry, so the plant is reproducible.
    """

    def __init__(
        self, config: FactoryConfig, registries: Registries, rngs: RngRegistry
    ) -> None:
        self._config = config
        self._registries = registries
        self._rngs = rngs

    def build(self, start_time: datetime) -> Plant:
        config = self._config
        plant = Plant(
            plant_id=config.plant.plant_id,
            name=config.plant.name,
            location=config.plant.location,
            timezone=config.plant.timezone,
        )

        for unit_spec in self._registries.topology.units():
            plant.units[unit_spec.id] = Unit(spec=unit_spec, plant_id=plant.plant_id)

        self._build_machines(plant, start_time)
        self._build_workforce(plant, start_time)
        return plant

    # ------------------------------------------------------------------ machines
    def _build_machines(self, plant: Plant, start_time: datetime) -> None:
        equipment = self._registries.equipment
        states = self._registries.states

        for unit_id in self._registries.topology.unit_ids:
            unit = plant.units[unit_id]
            for group in equipment.groups_for_unit(unit_id):
                resolved = equipment.get(group.equipment_class)
                sensor_specs = equipment.sensors_for_group(unit_id, group)
                layout_rng = self._rngs.child("layout", unit_id, group.id_prefix)

                for index in range(1, group.count + 1):
                    machine_id = f"{group.id_prefix}-{index:03d}"
                    commissioned = self._sample_date(
                        layout_rng, group.commissioned_from, group.commissioned_to
                    )
                    models = tuple(
                        SensorModel(
                            spec,
                            machine_id=machine_id,
                            unit_id=unit_id,
                            states=states,
                            # Per-tag stream: one machine's vibration history is
                            # independent of every other tag and machine.
                            rng=self._rngs.child("sensor", machine_id, spec.tag),
                        )
                        for spec in sensor_specs
                    )
                    machine = Machine(
                        machine_id=machine_id,
                        unit_id=unit_id,
                        plant_id=plant.plant_id,
                        equipment_class=group.equipment_class,
                        spec=resolved.spec,
                        commissioned_on=commissioned,
                        sensor_specs=sensor_specs,
                        states=states,
                        sensor_models=models,
                        start_time=start_time,
                    )
                    # Stagger PM due dates by machine age, so the plant does not
                    # arrive at its first PM window all at once.
                    offset = layout_rng.uniform(0.05, 0.95) * resolved.spec.pm_interval_hours
                    machine.pm_due_at = start_time + timedelta(hours=offset)
                    self._seed_wear(machine, machine_id)
                    unit.machines.append(machine)
                    plant.machines[machine_id] = machine

    def _seed_wear(self, machine: Machine, machine_id: str) -> None:
        """Spread each machine across its wear cycle for every applicable mode.

        A plant whose every machine starts with zero accumulated wear would see
        no wear-out failures at all for a long time, and then see them arrive
        together. Seeding each mode's clock independently gives the staggered,
        uncorrelated population a real fleet has.
        """
        for mode in self._registries.failures.for_class(machine.equipment_class):
            rng = self._rngs.child("wear", machine_id, mode.id)
            if mode.spec.weibull_beta <= 1.0:
                # Memoryless: its hazard does not depend on accumulated age.
                machine.seed_mode_age(mode.id, 0.0)
                continue
            machine.seed_mode_age(
                mode.id, rng.uniform(0.25, 1.10) * mode.spec.mtbf_operating_hours
            )

    @staticmethod
    def _sample_date(rng, earliest: date, latest: date) -> date:
        span = (latest - earliest).days
        if span <= 0:
            return earliest
        return earliest + timedelta(days=rng.randint(0, span))

    # ----------------------------------------------------------------- workforce
    def _build_workforce(self, plant: Plant, start_time: datetime) -> None:
        topology = self._registries.topology
        shift_codes = [spec.code for spec in self._config.shifts.shifts]
        rng = self._rngs.child("workforce")
        counter = 0

        def next_id() -> str:
            nonlocal counter
            counter += 1
            return f"EMP-{counter:04d}"

        def make_name() -> str:
            return f"{rng.choice(_GIVEN_NAMES)} {rng.choice(_SURNAMES)}"

        tenure_rng = self._rngs.child("workforce_tenure")
        tenure_floor, tenure_ceiling = topology.tenure_years

        def hire_date(experience_years: float) -> datetime:
            """Site tenure, bounded by the person's total industry experience.

            A plant is not staffed on the morning the simulation starts, and
            nobody has worked here longer than they have worked anywhere. The
            mode sits at the shortest tenure because attrition leaves a real
            workforce with more recent hires than long-serving ones.

            Drawn from its own stream so that adding it leaves every other
            workforce draw -- names, skills, experience, attendance -- unchanged.
            """
            ceiling = min(tenure_ceiling, max(experience_years, tenure_floor))
            years = tenure_rng.triangular(tenure_floor, ceiling, tenure_floor)
            return start_time - timedelta(days=years * 365.25)

        # Plant manager first, so EMP-0001 is the most senior person.
        manager_id = next_id()
        manager_experience = round(rng.uniform(14.0, 26.0), 1)
        plant.employees[manager_id] = Employee(
            employee_id=manager_id,
            name=self._config.plant.plant_manager_name,
            plant_id=plant.plant_id,
            unit_id=None,
            role="PLANT_MANAGER",
            skill_level=topology.skill_levels[-1],
            shift_code=shift_codes[0],
            experience_years=manager_experience,
            attendance_probability=0.99,
            hired_on=hire_date(manager_experience),
        )
        plant.plant_manager_id = manager_id

        for unit_spec in topology.units():
            unit = plant.units[unit_spec.id]
            # dict.fromkeys, not a set: this tuple is zipped against RNG draws
            # below, and a set's iteration order for strings varies with
            # PYTHONHASHSEED — which would pair a different equipment class with
            # each draw in every process and make the whole run irreproducible.
            # Declaration order is also what a reader of units.yaml expects.
            classes_here = tuple(
                dict.fromkeys(
                    group.equipment_class
                    for group in self._registries.equipment.groups_for_unit(unit_spec.id)
                )
            )

            for _ in range(unit_spec.manager_count):
                employee_id = next_id()
                unit_mgr_experience = round(rng.uniform(8.0, 20.0), 1)
                plant.employees[employee_id] = Employee(
                    employee_id=employee_id,
                    name=make_name(),
                    plant_id=plant.plant_id,
                    unit_id=unit_spec.id,
                    role=topology.manager_role,
                    skill_level=topology.skill_levels[-1],
                    shift_code=shift_codes[0],
                    experience_years=unit_mgr_experience,
                    attendance_probability=round(rng.uniform(0.96, 0.995), 4),
                    machine_certifications=classes_here,
                    hired_on=hire_date(unit_mgr_experience),
                )
                unit.manager_ids.append(employee_id)

            for index in range(unit_spec.worker_count):
                employee_id = next_id()
                skill = self._sample_skill(rng, topology.skill_levels)
                experience = self._experience_for(rng, skill)
                # A worker is certified on most, but not all, of the unit's
                # equipment; missing certification is what makes assignment
                # occasionally fall to a less suitable operator.
                certified = tuple(
                    class_id for class_id in classes_here if rng.random() < 0.82
                ) or classes_here[:1]
                employee = Employee(
                    employee_id=employee_id,
                    name=make_name(),
                    plant_id=plant.plant_id,
                    unit_id=unit_spec.id,
                    role=(
                        topology.worker_roles[-1]
                        if skill == topology.skill_levels[-1] and len(topology.worker_roles) > 1
                        else topology.worker_roles[0]
                    ),
                    skill_level=skill,
                    shift_code=shift_codes[index % len(shift_codes)],
                    experience_years=experience,
                    attendance_probability=round(rng.uniform(0.90, 0.99), 4),
                    machine_certifications=certified,
                    hired_on=hire_date(experience),
                )
                plant.employees[employee_id] = employee
                unit.worker_ids.append(employee_id)

        self._build_support_staff(
            plant, start_time, rng, next_id, make_name, shift_codes, hire_date
        )

    def _build_support_staff(
        self,
        plant: Plant,
        start_time: datetime,
        rng,
        next_id,
        make_name,
        shift_codes: list[str],
        hire_date,
    ) -> None:
        """Maintenance technicians and QC analysts, sized from configuration."""
        topology = self._registries.topology
        all_classes = tuple(self._registries.equipment.ids)

        for index in range(self._config.maintenance.technician_pool):
            employee_id = next_id()
            tech_experience = round(rng.uniform(3.0, 22.0), 1)
            plant.employees[employee_id] = Employee(
                employee_id=employee_id,
                name=make_name(),
                plant_id=plant.plant_id,
                unit_id=None,
                role=topology.technician_role,
                skill_level=self._sample_skill(rng, topology.skill_levels),
                shift_code=shift_codes[index % len(shift_codes)],
                experience_years=tech_experience,
                attendance_probability=round(rng.uniform(0.93, 0.99), 4),
                machine_certifications=all_classes,
                hired_on=hire_date(tech_experience),
            )

        # One QC analyst per shift, plus one spare.
        for index in range(len(shift_codes) + 1):
            employee_id = next_id()
            qc_experience = round(rng.uniform(2.0, 18.0), 1)
            plant.employees[employee_id] = Employee(
                employee_id=employee_id,
                name=make_name(),
                plant_id=plant.plant_id,
                unit_id=None,
                role=topology.qc_analyst_role,
                skill_level=self._sample_skill(rng, topology.skill_levels),
                shift_code=shift_codes[index % len(shift_codes)],
                experience_years=qc_experience,
                attendance_probability=round(rng.uniform(0.94, 0.99), 4),
                hired_on=hire_date(qc_experience),
            )

    @staticmethod
    def _sample_skill(rng, levels: tuple[str, ...]) -> str:
        """Skill mix weighted toward the middle, as a real plant's would be."""
        if len(levels) == 1:
            return levels[0]
        weights = [0.30, 0.45, 0.25] if len(levels) == 3 else [1.0 / len(levels)] * len(levels)
        return rng.choices(list(levels), weights=weights[: len(levels)], k=1)[0]

    @staticmethod
    def _experience_for(rng, skill: str) -> float:
        spans = {"JUNIOR": (0.2, 2.5), "INTERMEDIATE": (2.0, 8.0), "SENIOR": (7.0, 24.0)}
        low, high = spans.get(skill, (1.0, 10.0))
        return round(rng.uniform(low, high), 1)
