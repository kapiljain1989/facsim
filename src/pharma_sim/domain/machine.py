"""Machine model: state machine, health, degradation and production accounting.

Three things here are load-bearing for the rest of the simulator:

* **Transitions are validated.** The configured transition table is enforced, so
  an impossible sequence such as FAULT straight to RUNNING cannot appear in the
  data even if some caller asks for it.
* **Degradation is scheduled, not sampled.** When a failure mode begins
  incubating, the fault instant is fixed. That is what produces an observable
  precursor window and what makes exact remaining-useful-life labels possible.
* **Time is accounted by role, not by state name.** Every second lands in a
  bucket chosen from the state's semantic roles, which is how OEE keeps working
  when the state model changes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from pharma_sim.config.models import EquipmentClassSpec, SensorSpec
from pharma_sim.domain.plc import Plc
from pharma_sim.domain.sensor import PrecursorEffect, SensorModel
from pharma_sim.registry.failures import ApplicableMode, degradation_curve
from pharma_sim.registry.states import StateRegistry

__all__ = ["DegradationEpisode", "ProductionWindow", "Machine", "StateInterval"]


@dataclass(slots=True)
class DegradationEpisode:
    """One failure mode developing on one machine.

    ``fault_at`` is decided at onset. Everything observable — precursor
    trajectories, the warning transition, remaining-useful-life labels — is
    derived from it, which is what keeps the labels honest: they describe a fault
    time that really is going to happen unless maintenance intervenes.
    """

    episode_id: str
    failure_id: str
    mode: ApplicableMode
    started_at: datetime
    fault_at: datetime
    injected: bool = False
    warned_at: datetime | None = None
    faulted_at: datetime | None = None
    averted_at: datetime | None = None
    resolved_at: datetime | None = None

    @property
    def mode_id(self) -> str:
        return self.mode.id

    @property
    def incubation_hours(self) -> float:
        return (self.fault_at - self.started_at).total_seconds() / 3600.0

    @property
    def active(self) -> bool:
        return self.resolved_at is None and self.averted_at is None

    @property
    def averted(self) -> bool:
        return self.averted_at is not None

    def progress(self, now: datetime) -> float:
        """Linear fraction of the incubation period elapsed, clamped to [0, 1]."""
        total = (self.fault_at - self.started_at).total_seconds()
        if total <= 0.0:
            return 1.0
        elapsed = (now - self.started_at).total_seconds()
        return min(1.0, max(0.0, elapsed / total))

    def degradation(self, now: datetime) -> float:
        """Machine-level degradation from this episode, in ``[0, 1]``.

        Uses an exponential shape: quiet for most of the incubation, then a sharp
        rise. Individual precursor tags apply their own configured curve on top.
        """
        if self.faulted_at is not None and now >= self.faulted_at:
            return 1.0
        return degradation_curve(self.progress(now), "exponential")

    def remaining_hours(self, now: datetime) -> float:
        return max(0.0, (self.fault_at - now).total_seconds() / 3600.0)


@dataclass(slots=True)
class StateInterval:
    """A completed period spent in one state, retained for timelines and OEE.

    ``sequence`` exists because several transitions can occur at the same
    instant — routing through IDLE and STARTING to reach RUNNING happens
    atomically — and a timeline keyed only on the entry timestamp would collapse
    those zero-duration intervals into one another.
    """

    machine_id: str
    sequence: int
    state: str
    entered_at: datetime
    exited_at: datetime
    reason: str
    batch_id: str | None
    seconds: float


@dataclass(slots=True)
class ProductionWindow:
    """Production and time accounting over one window, typically a shift."""

    planned_quantity: float = 0.0
    actual_quantity: float = 0.0
    good_quantity: float = 0.0
    reject_quantity: float = 0.0
    scrap_quantity: float = 0.0
    runtime_seconds: float = 0.0
    idle_seconds: float = 0.0
    downtime_seconds: float = 0.0
    planned_stop_seconds: float = 0.0
    offline_seconds: float = 0.0
    #: Idle time with no work assigned. Standard OEE excludes unscheduled time
    #: from loading time: a machine with nothing to make is not "unavailable".
    unscheduled_seconds: float = 0.0
    energy_kwh: float = 0.0
    cycle_count: int = 0

    def add_time(self, bucket: str, seconds: float) -> None:
        if bucket == "runtime":
            self.runtime_seconds += seconds
        elif bucket == "idle":
            self.idle_seconds += seconds
        elif bucket == "downtime":
            self.downtime_seconds += seconds
        elif bucket == "planned_stop":
            self.planned_stop_seconds += seconds
        elif bucket == "unscheduled":
            self.unscheduled_seconds += seconds
        else:
            self.offline_seconds += seconds

    def reset(self) -> None:
        for name in self.__slots__:
            setattr(self, name, 0 if name == "cycle_count" else 0.0)

    def snapshot(self) -> ProductionWindow:
        clone = ProductionWindow()
        for name in self.__slots__:
            setattr(clone, name, getattr(self, name))
        return clone

    def as_row(self) -> dict[str, Any]:
        return {
            "planned_quantity": round(self.planned_quantity, 2),
            "actual_quantity": round(self.actual_quantity, 2),
            "good_quantity": round(self.good_quantity, 2),
            "reject_quantity": round(self.reject_quantity, 2),
            "scrap_quantity": round(self.scrap_quantity, 2),
            "runtime_seconds": round(self.runtime_seconds, 1),
            "idle_seconds": round(self.idle_seconds, 1),
            "downtime_seconds": round(self.downtime_seconds, 1),
            "planned_stop_seconds": round(self.planned_stop_seconds, 1),
            "offline_seconds": round(self.offline_seconds, 1),
            "unscheduled_seconds": round(self.unscheduled_seconds, 1),
            "energy_kwh": round(self.energy_kwh, 3),
            "cycle_count": self.cycle_count,
        }


class Machine:
    """One production machine and everything the simulator tracks about it."""

    __slots__ = (
        "machine_id",
        "unit_id",
        "plant_id",
        "equipment_class",
        "duty",
        "_line_active",
        "spec",
        "commissioned_on",
        "plc",
        "sensors",
        "_states",
        "state",
        "state_since",
        "state_reason",
        "state_history",
        "lifetime_state_seconds",
        "operating_hours",
        "episodes",
        "shift_window",
        "lifetime_window",
        "assigned_operators",
        "current_batch_id",
        "current_stage",
        "current_product_id",
        "target_rate_per_hour",
        "last_pm_at",
        "last_maintenance_at",
        "corrective_repairs",
        "pm_due_at",
        "maintenance_count",
        "failure_count",
        "process_values",
        "_last_accrual",
        "_operator_inexperience",
        "_transition_count",
        "batches_completed",
        "_pending_maintenance",
        "_stage_sums",
        "_stage_counts",
        "_has_derived",
        "_mode_age_base",
        "_mode_reset",
        "incidents",
        "_interval_seq",
    )

    def __init__(
        self,
        *,
        machine_id: str,
        unit_id: str,
        plant_id: str,
        equipment_class: str,
        spec: EquipmentClassSpec,
        commissioned_on: date,
        sensor_specs: tuple[SensorSpec, ...],
        states: StateRegistry,
        sensor_models: tuple[SensorModel, ...],
        start_time: datetime,
    ) -> None:
        self.machine_id = machine_id
        self.unit_id = unit_id
        self.plant_id = plant_id
        self.equipment_class = equipment_class
        self.spec = spec
        self.commissioned_on = commissioned_on
        self.plc = Plc(f"PLC-{machine_id}", machine_id, sensor_specs)
        self.sensors: dict[str, SensorModel] = {model.tag: model for model in sensor_models}
        self._has_derived = any(model.is_derived for model in sensor_models)
        self._mode_age_base: dict[str, float] = {}
        self._mode_reset: dict[str, float] = {}
        self.incidents: dict[str, list[datetime]] = {}
        self._interval_seq = 0
        self._states = states

        self.state = states.initial
        self.state_since = start_time
        self.state_reason = "INITIALISED"
        self.state_history: list[StateInterval] = []
        self.lifetime_state_seconds: dict[str, float] = {}
        self.operating_hours = 0.0
        self._transition_count = 0

        self.episodes: list[DegradationEpisode] = []
        self.shift_window = ProductionWindow()
        self.lifetime_window = ProductionWindow()

        self.assigned_operators: list[str] = []
        self._operator_inexperience = 0.5
        self.current_batch_id: str | None = None
        self.current_stage: str | None = None
        self.current_product_id: str | None = None
        self.target_rate_per_hour = spec.nominal_rate_per_hour

        # Duty decides what counts as "having work". A batch machine has work
        # when a stage is routed to it; a continuous utility always has work; a
        # coupled machine has work while its line is running, which the duty
        # manager keeps up to date.
        self.duty = spec.duty
        self._line_active = False

        self.last_pm_at: datetime | None = None
        self.last_maintenance_at: datetime | None = None
        self.corrective_repairs: list[datetime] = []
        self.pm_due_at = start_time + timedelta(hours=spec.pm_interval_hours)
        self.maintenance_count = 0
        self.failure_count = 0
        self.batches_completed = 0
        self._pending_maintenance = False

        self.process_values: dict[str, float] = {}
        self._stage_sums: dict[str, float] = {}
        self._stage_counts: dict[str, int] = {}
        self._last_accrual = start_time

    # ------------------------------------------------------------------ basics
    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Machine({self.machine_id}, {self.equipment_class}, state={self.state})"

    @property
    def transition_count(self) -> int:
        return self._transition_count

    def age_years(self, now: datetime) -> float:
        days = (now.date() - self.commissioned_on).days
        return max(0.0, days / 365.25)

    def as_row(self) -> dict[str, Any]:
        return {
            "machine_id": self.machine_id,
            "unit_id": self.unit_id,
            "plant_id": self.plant_id,
            "equipment_class": self.equipment_class,
            "name": self.spec.name,
            "commissioned_on": self.commissioned_on,
            "nominal_rate_per_hour": self.spec.nominal_rate_per_hour,
            "pm_interval_hours": self.spec.pm_interval_hours,
            "sensor_profile": self.spec.sensor_profile,
            "plc_id": self.plc.plc_id,
            "sensor_count": len(self.sensors),
        }

    # ------------------------------------------------------------------- health
    def health_at(self, now: datetime) -> float:
        """Worst active degradation across episodes, in ``[0, 1]``."""
        if not self.episodes:
            return 0.0
        worst = 0.0
        for episode in self.episodes:
            if episode.active:
                worst = max(worst, episode.degradation(now))
        return min(1.0, worst)

    def active_episodes(self) -> list[DegradationEpisode]:
        return [episode for episode in self.episodes if episode.active]

    def precursor_effects(self, now: datetime) -> dict[str, PrecursorEffect]:
        """Combined per-tag offset and variance gain from every active episode.

        Fractional deltas are resolved against each tag's own baseline here,
        because only the machine knows its resolved sensor specs — the same
        ``delta_fraction: 1.10`` therefore means "+110% of this tag's baseline"
        whether the tag is a 2 mm/s vibration or a 5,200 m3/h airflow.
        """
        if not self.episodes:
            return {}
        combined: dict[str, PrecursorEffect] = {}
        for episode in self.episodes:
            if not episode.active:
                continue
            progress = episode.progress(now)
            for precursor in episode.mode.precursors:
                sensor = self.sensors.get(precursor.tag)
                if sensor is None:
                    continue
                shaped = degradation_curve(progress, precursor.curve)
                offset = (
                    precursor.delta_absolute * shaped
                    + precursor.delta_fraction * shaped * abs(sensor.spec.baseline)
                )
                sigma_gain = precursor.sigma_growth * shaped
                existing = combined.get(precursor.tag)
                if existing is None:
                    combined[precursor.tag] = PrecursorEffect(offset, sigma_gain)
                else:
                    combined[precursor.tag] = PrecursorEffect(
                        existing.offset + offset,
                        max(existing.sigma_gain, sigma_gain),
                    )
        return combined

    def parameter_shifts(self, now: datetime) -> dict[str, float]:
        """Process parameter offsets caused by active degradation (§17)."""
        if not self.episodes:
            return {}
        shifts: dict[str, float] = {}
        for episode in self.episodes:
            if not episode.active:
                continue
            degradation = episode.degradation(now)
            for name, magnitude in episode.mode.parameter_shifts:
                shifts[name] = shifts.get(name, 0.0) + magnitude * degradation
        return shifts

    def variability_gain(self, now: datetime) -> float:
        """Extra process variability from active degradation."""
        if not self.episodes:
            return 0.0
        gain = 0.0
        for episode in self.episodes:
            if episode.active:
                gain += episode.mode.spec.effects.process_variability_gain * episode.degradation(now)
        return gain

    def extra_reject_rate(self, now: datetime) -> float:
        if not self.episodes:
            return 0.0
        rate = 0.0
        for episode in self.episodes:
            if episode.active:
                rate += episode.mode.spec.effects.reject_rate_add * episode.degradation(now)
        return rate

    # -------------------------------------------------------------- transitions
    def can_enter(self, state: str) -> bool:
        return self._states.can_transition(self.state, state)

    def transition_to(
        self, state: str, now: datetime, reason: str, *, strict: bool = True
    ) -> bool:
        """Move to ``state``, accounting for the time spent in the previous one.

        Args:
            strict: when true an illegal transition raises; when false it is
                refused and reported, which suits opportunistic callers such as
                the shift scheduler trying to idle a machine that is mid-repair.

        Returns:
            Whether the transition happened.
        """
        if state == self.state:
            return False
        if not self._states.can_transition(self.state, state):
            if strict:
                self._states.require_transition(self.machine_id, self.state, state)
            return False

        self.accrue_time(now)
        seconds = (now - self.state_since).total_seconds()
        self._interval_seq += 1
        self.state_history.append(
            StateInterval(
                machine_id=self.machine_id,
                sequence=self._interval_seq,
                state=self.state,
                entered_at=self.state_since,
                exited_at=now,
                reason=self.state_reason,
                batch_id=self.current_batch_id,
                seconds=seconds,
            )
        )
        self.state = state
        self.state_since = now
        self.state_reason = reason
        self._transition_count += 1
        self.plc.scan(now, self._states.ordinal(state))
        return True

    def force_route_to(self, state: str, now: datetime, reason: str) -> bool:
        """Walk the transition graph to reach ``state`` legally.

        Callers express intent ("get this machine running") without encoding the
        configured transition table, so a config change to that table does not
        require a code change here.
        """
        route = self._states.route(self.state, state)
        if route is None:
            return False
        for step in route:
            if not self.transition_to(step, now, reason, strict=False):
                return False
        return True

    def time_in_state(self, now: datetime) -> float:
        return (now - self.state_since).total_seconds()

    # ------------------------------------------------------------------- duty
    @property
    def is_continuous(self) -> bool:
        """A utility on continuous duty: always scheduled, never batch-driven."""
        return self.duty == "continuous"

    @property
    def has_work(self) -> bool:
        """Whether this machine is currently expected to be producing.

        Reading duty here rather than ``current_batch_id is not None`` is what
        keeps utilities and inline support out of the "unscheduled" bucket: a
        purified-water skid with no batch assigned is not unscheduled, it is
        supposed to be running.
        """
        if self.duty == "continuous":
            return True
        if self.duty == "coupled":
            return self._line_active
        return self.current_batch_id is not None

    def set_line_active(self, active: bool) -> None:
        """Tell a coupled machine whether the line it sits on is producing."""
        self._line_active = active

    # ----------------------------------------------------------- time accounting
    def _bucket_for(self, state: str) -> str:
        """Which accounting bucket this state's time belongs to.

        Idle splits two ways, and the distinction is what makes OEE meaningful:
        idle *while holding work* is an availability loss, but idle with nothing
        to do is unscheduled time and is excluded from loading time.
        """
        states = self._states
        if states.is_productive(state):
            return "runtime"
        if states.is_downtime(state):
            return "downtime"
        if states.is_planned_stop(state):
            return "planned_stop"
        if states.is_idle(state):
            return "idle" if self.has_work else "unscheduled"
        if states.is_offline(state):
            return "offline"
        return "idle" if self.has_work else "unscheduled"

    def accrue_time(self, now: datetime) -> float:
        """Bank elapsed time and production since the last accrual.

        Called on state changes and on the production tick, so counters stay
        correct however irregularly the machine is visited.
        """
        seconds = (now - self._last_accrual).total_seconds()
        if seconds <= 0.0:
            return 0.0
        self._last_accrual = now

        state = self.state
        bucket = self._bucket_for(state)
        self.shift_window.add_time(bucket, seconds)
        self.lifetime_window.add_time(bucket, seconds)
        self.lifetime_state_seconds[state] = self.lifetime_state_seconds.get(state, 0.0) + seconds

        hours = seconds / 3600.0
        energy = self._states.energy_factor(state) * hours * self._energy_rating()
        self.shift_window.energy_kwh += energy
        self.lifetime_window.energy_kwh += energy

        if self._states.is_productive(state):
            self.operating_hours += hours
            self._accrue_production(now, hours)
        elif self._states.requires_operator(state) or self._states.is_starting(state):
            # Setup and changeover consume machine life without making product.
            self.operating_hours += hours * 0.35
        return seconds

    def _energy_rating(self) -> float:
        """Nominal power draw in kW, scaled from throughput.

        A crude but monotonic proxy: a machine rated for more output draws more.
        """
        return 2.0 + math.log10(max(10.0, self.spec.nominal_rate_per_hour)) * 3.0

    def _accrue_production(self, now: datetime, hours: float) -> None:
        state_factor = self._states.production_rate_factor(self.state)
        if state_factor <= 0.0:
            return
        if not self.has_work:
            return

        # Planned is nameplate; achieved is a little under it. Rate loss from
        # operator skill and machine condition is what makes the performance
        # component of OEE mean something rather than sitting at exactly 1.0.
        rate_loss = 0.02 + 0.06 * self._operator_inexperience + 0.10 * self.health_at(now)
        effective_rate = self.target_rate_per_hour * max(0.35, 1.0 - rate_loss)
        produced = effective_rate * state_factor * hours
        planned = self.spec.nominal_rate_per_hour * hours

        reject_rate = min(
            0.85,
            self.spec.base_reject_rate
            + self._states.reject_rate_add(self.state)
            + self.extra_reject_rate(now)
            + 0.010 * self._operator_inexperience,
        )
        rejects = produced * reject_rate
        good = produced - rejects

        for window in (self.shift_window, self.lifetime_window):
            window.planned_quantity += planned
            window.actual_quantity += produced
            window.good_quantity += good
            window.reject_quantity += rejects

    def flush_shift_window(self) -> ProductionWindow:
        """Snapshot and clear the shift accumulator."""
        snapshot = self.shift_window.snapshot()
        self.shift_window.reset()
        return snapshot

    def load_factor(self) -> float:
        """Fraction of nominal throughput currently demanded, for the hazard model."""
        if not self.has_work:
            return 0.0
        return min(1.5, self.target_rate_per_hour / self.spec.nominal_rate_per_hour)

    # ---------------------------------------------------------------- operators
    def assign_operators(self, employee_ids: list[str], inexperience: float) -> None:
        self.assigned_operators = list(employee_ids)
        self._operator_inexperience = min(1.0, max(0.0, inexperience))

    @property
    def operator_inexperience(self) -> float:
        return self._operator_inexperience

    @property
    def has_operator(self) -> bool:
        return bool(self.assigned_operators)

    # -------------------------------------------------------------- maintenance
    def pm_overdue_hours(self, now: datetime) -> float:
        return max(0.0, (now - self.pm_due_at).total_seconds() / 3600.0)

    def pm_overdue_ratio(self, now: datetime) -> float:
        """Overdue time as a fraction of the PM interval, capped for stability."""
        overdue = self.pm_overdue_hours(now)
        if overdue <= 0.0:
            return 0.0
        return min(3.0, overdue / self.spec.pm_interval_hours)

    def hours_since_maintenance(self, now: datetime) -> float:
        if self.last_maintenance_at is None:
            return (now - self.state_since).total_seconds() / 3600.0 + 720.0
        return (now - self.last_maintenance_at).total_seconds() / 3600.0

    def corrective_repairs_since(self, now: datetime, days: float = 90.0) -> int:
        cutoff = now - timedelta(days=days)
        return sum(1 for stamp in self.corrective_repairs if stamp >= cutoff)

    def record_maintenance(
        self, now: datetime, maintenance_type: str, effectiveness: float
    ) -> None:
        """Apply a completed maintenance action to the machine's condition."""
        self.last_maintenance_at = now
        self.maintenance_count += 1
        self._pending_maintenance = False
        if maintenance_type == "PREVENTIVE" or maintenance_type == "PREDICTIVE":
            self.pm_due_at = now + timedelta(hours=self.spec.pm_interval_hours)
        if maintenance_type in {"CORRECTIVE", "EMERGENCY"}:
            self.corrective_repairs.append(now)
            self.pm_due_at = now + timedelta(hours=self.spec.pm_interval_hours)
        for sensor in self.sensors.values():
            sensor.relieve_degradation(effectiveness)

    def defer_pm(self, hours: float) -> None:
        self.pm_due_at = self.pm_due_at + timedelta(hours=hours)

    @property
    def maintenance_pending(self) -> bool:
        return self._pending_maintenance

    def mark_maintenance_pending(self) -> None:
        self._pending_maintenance = True

    # --------------------------------------------------------------- incidents
    def record_incident(self, kind: str, at: datetime) -> None:
        """Log an observable operational incident against this machine.

        These are the things a plant genuinely records at the time — a setup
        error, a missed in-process check, a wait on material — and they are what
        RCA reads later. Deriving them from live episode state instead would make
        them vanish the moment the machine was repaired, which is precisely when
        the investigation runs.
        """
        self.incidents.setdefault(kind, []).append(at)

    def incident_count(self, kind: str, since: datetime) -> int:
        return sum(1 for stamp in self.incidents.get(kind, ()) if stamp >= since)

    # --------------------------------------------------------------- mode ages
    def seed_mode_age(self, mode_id: str, hours: float) -> None:
        """Place this mode's wear clock somewhere in its life at build time.

        Without this every machine would start with zero accumulated wear, so a
        Weibull mode with beta > 1 — a bearing, a gearbox, a pump seal — would
        have an almost-zero hazard for the whole run and never fail. Real
        equipment is spread across its wear cycle, and seeding the clock is what
        reproduces that.
        """
        self._mode_age_base[mode_id] = max(0.0, hours)

    def mode_age_hours(self, mode_id: str) -> float:
        """Operating hours accumulated against this mode since its last reset."""
        base = self._mode_age_base.get(mode_id, 0.0)
        reset_at = self._mode_reset.get(mode_id, 0.0)
        return max(0.0, base + self.operating_hours - reset_at)

    def reset_mode_age(self, mode_id: str) -> None:
        """Restart a mode's wear clock, as replacing the part would."""
        self._mode_reset[mode_id] = self.operating_hours
        self._mode_age_base[mode_id] = 0.0

    # ---------------------------------------------------------------- episodes
    def add_episode(self, episode: DegradationEpisode) -> None:
        self.episodes.append(episode)

    def episode_for_mode(self, mode_id: str) -> DegradationEpisode | None:
        for episode in self.episodes:
            if episode.active and episode.mode_id == mode_id:
                return episode
        return None

    def has_active_mode(self, mode_id: str) -> bool:
        return self.episode_for_mode(mode_id) is not None

    def active_mode_count(self) -> int:
        return sum(1 for episode in self.episodes if episode.active)

    def resolve_episodes(self, now: datetime, effectiveness: float) -> list[DegradationEpisode]:
        """Close out active episodes after a repair.

        An episode whose fault had not yet arrived is marked **averted**, which
        the label writer needs: without it the forward-looking labels would claim
        a failure that never happened.
        """
        resolved: list[DegradationEpisode] = []
        for episode in self.episodes:
            if not episode.active:
                continue
            if episode.faulted_at is None:
                episode.averted_at = now
            episode.resolved_at = now
            # The repair replaced the worn part, so that mode's wear clock
            # restarts. Leaving it running would make the same machine fail the
            # same way again almost immediately.
            self.reset_mode_age(episode.mode_id)
            resolved.append(episode)
        if effectiveness > 0.0:
            for sensor in self.sensors.values():
                sensor.relieve_degradation(effectiveness)
        return resolved

    # ------------------------------------------------------------------ sensors
    @property
    def has_derived_tags(self) -> bool:
        """Whether any tag mirrors machine state instead of generating a value."""
        return self._has_derived

    def derived_values(self, now: datetime) -> dict[str, float]:
        """Live values for tags that mirror machine state rather than generate one."""
        state_factor = self._states.production_rate_factor(self.state)
        load = 1.0 if self.has_work else 0.0
        return {
            "production_rate": self.target_rate_per_hour * state_factor * load,
            "good_count": self.shift_window.good_quantity,
            "reject_count": self.shift_window.reject_quantity,
            "total_count": self.shift_window.actual_quantity,
            "batch_counter": float(self.batches_completed),
            "energy_kw": self._states.energy_factor(self.state) * self._energy_rating(),
            "health_index": self.health_at(now),
            "run_state_code": float(self._states.ordinal(self.state)),
            "operator_present": 1.0 if self.assigned_operators else 0.0,
        }

    def record_process_value(self, tag: str, value: float) -> None:
        """Record a process-parameter reading, both latest and stage-cumulative.

        The stage mean of the *measured* tag is what QC consumes, so a QC result
        is arithmetically downstream of the telemetry rather than a parallel
        invention. A degradation that shifts compression force therefore shows up
        in the sensor stream and in the hardness result, consistently.
        """
        self.process_values[tag] = value
        self._stage_sums[tag] = self._stage_sums.get(tag, 0.0) + value
        self._stage_counts[tag] = self._stage_counts.get(tag, 0) + 1

    def begin_stage(self, batch_id: str, stage: str, product_id: str) -> None:
        self.current_batch_id = batch_id
        self.current_stage = stage
        self.current_product_id = product_id
        self._stage_sums.clear()
        self._stage_counts.clear()

    def end_stage(self) -> dict[str, float]:
        """Mean achieved value per process parameter over the stage just finished."""
        means = {
            tag: self._stage_sums[tag] / count
            for tag, count in self._stage_counts.items()
            if count > 0
        }
        self.current_batch_id = None
        self.current_stage = None
        self.current_product_id = None
        for model in self.sensors.values():
            model.set_setpoint(None)
        self._stage_sums.clear()
        self._stage_counts.clear()
        return means

    def apply_setpoints(self, setpoints: dict[str, float]) -> None:
        """Point this machine's tags at a product's setpoints for the stage."""
        for tag, value in setpoints.items():
            model = self.sensors.get(tag)
            if model is not None:
                model.set_setpoint(value)

    def sensor_ids(self) -> tuple[str, ...]:
        return tuple(model.sensor_id for model in self.sensors.values())

    def sensor_rows(self) -> list[dict[str, Any]]:
        """Persistable sensor dimension, so every reading resolves to a real sensor."""
        rows: list[dict[str, Any]] = []
        for model in self.sensors.values():
            spec = model.spec
            rows.append(
                {
                    "sensor_id": model.sensor_id,
                    "machine_id": self.machine_id,
                    "unit_id": self.unit_id,
                    "plant_id": self.plant_id,
                    "tag": spec.tag,
                    "unit": spec.unit,
                    "plc_area": spec.plc_area,
                    "baseline": spec.baseline,
                    "sample_interval_s": spec.rate_s,
                    "warn_low": spec.warn_low,
                    "warn_high": spec.warn_high,
                    "alarm_low": spec.alarm_low,
                    "alarm_high": spec.alarm_high,
                    "is_process_parameter": spec.process_parameter,
                    "derived_from": spec.derived_from,
                }
            )
        return rows
