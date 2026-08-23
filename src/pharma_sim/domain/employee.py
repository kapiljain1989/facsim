"""Employee model.

Operator attributes are not decoration: ``inexperience`` feeds the failure hazard
model, the reject rate and the setup-error probability, so a junior crew on a
short-staffed night shift genuinely produces a worse dataset than a senior day
crew. The influence is probabilistic rather than deterministic, as §7 requires.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from random import Random
from typing import Any

__all__ = ["Employee", "EmployeeAttendance"]

#: Inexperience implied by each skill level. Interpolated with experience years
#: so two SENIOR operators of different tenure are not identical.
_SKILL_BASE: dict[str, float] = {
    "JUNIOR": 0.80,
    "INTERMEDIATE": 0.45,
    "SENIOR": 0.18,
}


@dataclass(slots=True)
class Employee:
    """One member of the workforce."""

    employee_id: str
    name: str
    plant_id: str
    unit_id: str | None
    role: str
    skill_level: str
    shift_code: str
    experience_years: float
    attendance_probability: float
    machine_certifications: tuple[str, ...] = ()
    hired_on: datetime | None = None

    @property
    def inexperience(self) -> float:
        """Composite inexperience in ``[0, 1]``: skill level tempered by tenure."""
        base = _SKILL_BASE.get(self.skill_level, 0.5)
        tenure_relief = min(0.35, self.experience_years * 0.035)
        return max(0.02, min(1.0, base - tenure_relief))

    def certified_for(self, equipment_class: str) -> bool:
        return equipment_class in self.machine_certifications

    def as_row(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "name": self.name,
            "plant_id": self.plant_id,
            "unit_id": self.unit_id,
            "role": self.role,
            "skill_level": self.skill_level,
            "shift_code": self.shift_code,
            "experience_years": round(self.experience_years, 2),
            "attendance_probability": round(self.attendance_probability, 4),
            "machine_certifications": ",".join(self.machine_certifications),
            "inexperience": round(self.inexperience, 4),
            "hired_on": self.hired_on,
        }


@dataclass(slots=True)
class EmployeeAttendance:
    """One employee's participation in one shift instance."""

    employee_id: str
    shift_instance_id: str
    present: bool
    clock_in: datetime | None = None
    clock_out: datetime | None = None
    overtime_minutes: float = 0.0
    breaks_taken: int = 0
    assigned_machines: list[str] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        return {
            "employee_id": self.employee_id,
            "shift_instance_id": self.shift_instance_id,
            "present": self.present,
            "clock_in": self.clock_in,
            "clock_out": self.clock_out,
            "overtime_minutes": round(self.overtime_minutes, 1),
            "breaks_taken": self.breaks_taken,
            "assigned_machines": ",".join(self.assigned_machines),
        }


def draw_attendance(employee: Employee, rng: Random, absenteeism_rate: float) -> bool:
    """Decide whether an employee turns up.

    Combines the plant-wide absenteeism rate with the individual's own
    reliability, so absence is neither uniform nor deterministic.
    """
    absence_probability = absenteeism_rate * (2.0 - employee.attendance_probability)
    return rng.random() >= min(0.6, max(0.0, absence_probability))
