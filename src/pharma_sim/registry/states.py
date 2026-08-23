"""Machine state registry.

The engine asks this registry what a state *means* instead of comparing against a
name. That indirection is what lets a deployment rename ``RUNNING`` or add a
``QUALIFICATION`` state without touching production, OEE, downtime or sensor
code.
"""

from __future__ import annotations

from pharma_sim.config.models import StateFactor, StateRolesConfig, StatesConfig, StateSpec

__all__ = ["StateRegistry", "IllegalTransition"]

#: Role names in precedence order. When a sensor declares factors for more than
#: one role that a state belongs to, the earliest here wins. Fixed order keeps
#: resolution deterministic rather than dependent on dict iteration.
_ROLE_PRECEDENCE: tuple[str, ...] = (
    "fault",
    "maintenance",
    "cleaning",
    "changeover",
    "offline",
    "starting",
    "warning",
    "idle",
    "planned_stop",
    "productive",
    "requires_operator",
    "downtime",
)


class IllegalTransition(Exception):
    """Raised when a transition is not permitted by the configured table."""

    def __init__(self, machine_id: str, from_state: str, to_state: str, allowed: list[str]) -> None:
        self.machine_id = machine_id
        self.from_state = from_state
        self.to_state = to_state
        self.allowed = allowed
        super().__init__(
            f"{machine_id}: {from_state} -> {to_state} is not a legal transition; "
            f"allowed from {from_state}: {allowed or '<none>'}"
        )


class StateRegistry:
    """Declared states, their legal transitions and their semantic roles."""

    __slots__ = ("_states", "_transitions", "_roles", "_order", "_role_members", "_roles_of")

    def __init__(self, config: StatesConfig) -> None:
        self._states: dict[str, StateSpec] = {spec.id: spec for spec in config.states}
        self._transitions: dict[str, frozenset[str]] = {
            source: frozenset(targets) for source, targets in config.transitions.items()
        }
        self._roles = config.roles
        self._order: tuple[str, ...] = tuple(spec.id for spec in config.states)

        self._role_members: dict[str, frozenset[str]] = {
            name: frozenset(getattr(config.roles, name))
            for name in StateRolesConfig.model_fields
            if name != "initial"
        }
        # Precomputed so per-sample sensor resolution stays cheap.
        self._roles_of: dict[str, tuple[str, ...]] = {
            state_id: tuple(
                role for role in _ROLE_PRECEDENCE if state_id in self._role_members.get(role, ())
            )
            for state_id in self._states
        }

    # ------------------------------------------------------------------ basics
    @property
    def initial(self) -> str:
        return self._roles.initial

    @property
    def ids(self) -> tuple[str, ...]:
        """Declared state ids in configuration order."""
        return self._order

    def __len__(self) -> int:
        return len(self._states)

    def __contains__(self, state_id: object) -> bool:
        return state_id in self._states

    def get(self, state_id: str) -> StateSpec:
        try:
            return self._states[state_id]
        except KeyError:
            raise KeyError(
                f"unknown state {state_id!r}; declared states: {sorted(self._states)}"
            ) from None

    def ordinal(self, state_id: str) -> int:
        """Position in the declared order, used as the PLC state word."""
        return self._order.index(state_id)

    # ------------------------------------------------------------- transitions
    def allowed_from(self, state_id: str) -> frozenset[str]:
        return self._transitions.get(state_id, frozenset())

    def can_transition(self, from_state: str, to_state: str) -> bool:
        return to_state in self._transitions.get(from_state, frozenset())

    def require_transition(self, machine_id: str, from_state: str, to_state: str) -> None:
        """Raise :class:`IllegalTransition` unless the move is permitted."""
        if to_state not in self._states:
            raise KeyError(f"unknown target state {to_state!r}")
        if not self.can_transition(from_state, to_state):
            raise IllegalTransition(
                machine_id, from_state, to_state, sorted(self.allowed_from(from_state))
            )

    def path_exists(self, from_state: str, to_state: str) -> bool:
        """Whether ``to_state`` is reachable from ``from_state`` at all."""
        seen = {from_state}
        frontier = [from_state]
        while frontier:
            current = frontier.pop()
            for nxt in self._transitions.get(current, frozenset()):
                if nxt == to_state:
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        return False

    def route(self, from_state: str, to_state: str) -> list[str] | None:
        """Shortest legal sequence of states from one state to another.

        Used when the engine wants a machine in some state but the transition
        table requires intermediate steps — going through IDLE to reach STARTING,
        for instance — so callers never need to know the table's shape.
        """
        if from_state == to_state:
            return []
        queue: list[tuple[str, list[str]]] = [(from_state, [])]
        seen = {from_state}
        while queue:
            current, path = queue.pop(0)
            for nxt in sorted(self._transitions.get(current, frozenset())):
                if nxt in seen:
                    continue
                extended = [*path, nxt]
                if nxt == to_state:
                    return extended
                seen.add(nxt)
                queue.append((nxt, extended))
        return None

    # -------------------------------------------------------------- semantics
    def members(self, role: str) -> frozenset[str]:
        try:
            return self._role_members[role]
        except KeyError:
            raise KeyError(
                f"unknown state role {role!r}; roles: {sorted(self._role_members)}"
            ) from None

    def has_role(self, state_id: str, role: str) -> bool:
        return state_id in self.members(role)

    def roles_of(self, state_id: str) -> tuple[str, ...]:
        """Roles a state belongs to, in resolution precedence order."""
        return self._roles_of.get(state_id, ())

    def is_productive(self, state_id: str) -> bool:
        return state_id in self._role_members["productive"]

    def is_downtime(self, state_id: str) -> bool:
        return state_id in self._role_members["downtime"]

    def is_planned_stop(self, state_id: str) -> bool:
        return state_id in self._role_members["planned_stop"]

    def is_fault(self, state_id: str) -> bool:
        return state_id in self._role_members["fault"]

    def is_warning(self, state_id: str) -> bool:
        return state_id in self._role_members["warning"]

    def is_offline(self, state_id: str) -> bool:
        return state_id in self._role_members["offline"]

    def is_idle(self, state_id: str) -> bool:
        return state_id in self._role_members["idle"]

    def is_maintenance(self, state_id: str) -> bool:
        return state_id in self._role_members["maintenance"]

    def is_starting(self, state_id: str) -> bool:
        return state_id in self._role_members["starting"]

    def is_changeover(self, state_id: str) -> bool:
        return state_id in self._role_members["changeover"]

    def is_cleaning(self, state_id: str) -> bool:
        return state_id in self._role_members["cleaning"]

    def requires_operator(self, state_id: str) -> bool:
        return state_id in self._role_members["requires_operator"]

    def first(self, role: str) -> str:
        """The canonical state for a role, e.g. which state to enter to fault.

        Uses declaration order so the choice is stable and configurable.
        """
        state_id = self.first_or_none(role)
        if state_id is None:
            raise KeyError(
                f"no state is assigned to role {role!r}; assign one in states.yaml"
            )
        return state_id

    def first_or_none(self, role: str) -> str | None:
        """Like :meth:`first`, but returns ``None`` for an unpopulated role.

        Some roles are genuinely optional. A plant model with no distinct
        cleaning or changeover state is a legitimate configuration, and the
        engine skips those steps rather than refusing to run — which is what
        makes the state model replaceable rather than merely renameable.
        """
        members = self.members(role)
        for state_id in self._order:
            if state_id in members:
                return state_id
        return None

    # ------------------------------------------------------- sensor behaviour
    def sensor_factor(
        self, state_id: str, factors: dict[str, StateFactor]
    ) -> StateFactor | None:
        """Resolve a sensor's state factor for ``state_id``.

        A factor keyed by the state id wins; otherwise the first matching role in
        precedence order applies. Returning ``None`` means the tag behaves at its
        baseline in this state.
        """
        direct = factors.get(state_id)
        if direct is not None:
            return direct
        for role in self._roles_of.get(state_id, ()):
            factor = factors.get(role)
            if factor is not None:
                return factor
        return None

    def production_rate_factor(self, state_id: str) -> float:
        return self._states[state_id].production_rate_factor

    def reject_rate_add(self, state_id: str) -> float:
        return self._states[state_id].reject_rate_add

    def energy_factor(self, state_id: str) -> float:
        return self._states[state_id].energy_factor
