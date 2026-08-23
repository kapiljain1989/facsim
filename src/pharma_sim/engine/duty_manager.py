"""Keeps non-batch equipment running the way a real plant runs it.

The batch manager routes stages to the machines that measure a stage's process
parameters, which is correct — a deduster must not be handed a compression
stage. But it leaves everything else with nothing to do, and a plant where the
HVAC, purified-water and compressed-air systems sit idle is not a plant.

So duty is declared in ``machines.yaml`` and honoured here:

* ``continuous`` — utilities run around the clock, unattended, and stop only for
  failure or maintenance. Brought back up as soon as a repair finishes.
* ``coupled`` — inline support follows the line it sits on. A deduster runs when
  a press in its unit is producing and idles when the unit goes quiet.

Nothing in this module names a machine, a state or a unit: duty comes from
config and the target states come from the state roles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from pharma_sim.domain.machine import Machine

__all__ = ["DutyManager", "DutyStats"]


@dataclass
class DutyStats:
    continuous_starts: int = 0
    coupled_starts: int = 0
    coupled_stops: int = 0
    blocked: int = 0


@dataclass
class DutyManager:
    ctx: object
    stats: DutyStats = field(default_factory=DutyStats)

    # ------------------------------------------------------------------ helpers
    def _available(self, machine: Machine) -> bool:
        """Whether the machine is free to be driven to a productive state.

        Downtime, maintenance and an in-progress planned stop all mean "leave it
        alone" — the duty manager must never yank a machine out of a repair or
        interrupt a cleaning cycle.

        Offline is the exception. State models commonly put offline in the
        planned-stop role, since it is excluded from OEE loading time the same
        way cleaning is, but "powered down" is precisely the state duty exists to
        bring a machine out of. Excluding it leaves every utility parked forever.
        """
        states = self.ctx.registries.states
        state = machine.state
        if machine.maintenance_pending:
            return False
        if states.is_offline(state):
            return True
        return not (
            states.is_downtime(state)
            or states.is_planned_stop(state)
            or states.is_starting(state)
        )

    # --------------------------------------------------------------------- tick
    def tick(self, now: datetime) -> None:
        states = self.ctx.registries.states
        productive = states.first("productive")
        idle = states.first("idle")

        for unit in self.ctx.plant.units.values():
            machines = unit.machines
            # A line is active when something in it is actually making product.
            # Read from batch-duty machines only, so coupled equipment can never
            # hold itself active.
            line_active = any(
                machine.duty == "batch"
                and machine.current_batch_id is not None
                and states.is_productive(machine.state)
                for machine in machines
            )

            for machine in machines:
                if machine.duty == "batch":
                    continue

                if machine.duty == "coupled":
                    machine.set_line_active(line_active)
                    want_productive = line_active
                else:  # continuous
                    want_productive = True

                if states.is_productive(machine.state) == want_productive:
                    continue

                if want_productive:
                    if not self._available(machine):
                        self.stats.blocked += 1
                        continue
                    machine.accrue_time(now)
                    reason = "DUTY:CONTINUOUS" if machine.is_continuous else "DUTY:LINE_ACTIVE"
                    if machine.force_route_to(productive, now, reason):
                        if machine.is_continuous:
                            self.stats.continuous_starts += 1
                        else:
                            self.stats.coupled_starts += 1
                    else:
                        self.stats.blocked += 1
                elif states.is_productive(machine.state):
                    # Only coupled machines get here; a continuous utility is
                    # never asked to stop by duty.
                    machine.accrue_time(now)
                    if machine.force_route_to(idle, now, "DUTY:LINE_IDLE"):
                        self.stats.coupled_stops += 1
