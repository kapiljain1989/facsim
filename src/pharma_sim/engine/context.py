"""Shared simulation context.

Every engine — failures, maintenance, batches, quality, deviations, RCA — needs
the same handful of collaborators. Passing one context keeps their constructors
honest and makes them straightforward to build in a test with a stub or two
swapped out, rather than each reaching for module-level state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from pharma_sim.config.models import FactoryConfig
from pharma_sim.domain.environment import Environment
from pharma_sim.domain.plant import Plant
from pharma_sim.engine.clock import SimulationClock
from pharma_sim.engine.event_bus import EventBus
from pharma_sim.engine.ids import IdFactory
from pharma_sim.engine.rng import RngRegistry
from pharma_sim.engine.scheduler import Scheduler
from pharma_sim.registry import Registries

__all__ = ["SimContext", "RecordSink"]


class RecordSink(Protocol):
    """Where domain records go. Implemented by the storage facade.

    Declared as a Protocol so the engines depend on the shape of persistence
    rather than on a concrete backend — the same code writes to SQLite, Postgres,
    Parquet or ClickHouse.
    """

    def write(self, table: str, row: dict) -> None: ...

    def write_many(self, table: str, rows: list[dict]) -> None: ...


@dataclass(frozen=True, slots=True)
class SimContext:
    """Collaborators shared by every engine."""

    config: FactoryConfig
    registries: Registries
    plant: Plant
    clock: SimulationClock
    scheduler: Scheduler
    bus: EventBus
    rngs: RngRegistry
    ids: IdFactory
    environment: Environment
    records: RecordSink
    run_id: str

    @property
    def now(self) -> datetime:
        return self.clock.now

    @property
    def plant_id(self) -> str:
        return self.plant.plant_id
