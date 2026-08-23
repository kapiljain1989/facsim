"""Shared fixtures.

Tests assert against *loaded configuration* rather than literals wherever the
value is configurable. That is deliberate: if a test hard-coded "10 units", then
changing units.yaml — the thing this simulator is built to allow — would break
the suite for no good reason. A separate test checks that the shipped default
config produces the numbers the brief asks for.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from pharma_sim.config.loader import load_config
from pharma_sim.registry import Registries

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
MINIMAL_CONFIG_DIR = CONFIG_DIR / "examples" / "minimal_factory"


@pytest.fixture(scope="session")
def config_dir() -> Path:
    return CONFIG_DIR


@pytest.fixture(scope="session")
def config():
    """The shipped default configuration, loaded once for the session."""
    return load_config(CONFIG_DIR)


@pytest.fixture(scope="session")
def registries(config):
    return Registries.build(config)


@pytest.fixture
def temp_config(tmp_path: Path) -> Path:
    """A writable copy of the default config, for tests that mutate it."""
    destination = tmp_path / "config"
    shutil.copytree(CONFIG_DIR, destination)
    return destination


@pytest.fixture
def storage_config(tmp_path: Path):
    """Storage pointed entirely at a temp directory, so tests never share state."""
    from pharma_sim.config.models import (
        EvaluationStorage,
        StorageConfig,
        TimeseriesStorage,
        TransactionalStorage,
    )

    return StorageConfig(
        transactional=TransactionalStorage(
            backend="sqlite", dsn=str(tmp_path / "factory.db"), batch_size=200
        ),
        timeseries=TimeseriesStorage(
            backend="parquet",
            dsn=str(tmp_path / "telemetry"),
            partition_by=["date"],
            batch_size=5000,
        ),
        evaluation=EvaluationStorage(
            backend="parquet", dsn=str(tmp_path / "eval"), batch_size=1000
        ),
    )


def _apply_test_profile(config) -> None:
    """Coarsen the telemetry cadence for integration tests.

    Sensor sampling dominates runtime, and none of the integration assertions
    depend on the cadence — the statistical sensor tests drive the model directly.
    Sampling every five minutes instead of every minute makes the suite usable
    without weakening anything it checks.
    """
    simulation = config.plant.simulation
    object.__setattr__(simulation, "sensor_sample_interval_s", 300.0)
    object.__setattr__(simulation, "production_tick_min", 10.0)


def _build_simulator(config_dir, storage_config, **kwargs):
    from pharma_sim.simulator import Simulator

    config = load_config(config_dir)
    object.__setattr__(config, "storage", storage_config)
    _apply_test_profile(config)
    return Simulator(
        config_dir,
        config=config,
        reset_storage=True,
        configure_logs=False,
        log_level="ERROR",
        **kwargs,
    )


@pytest.fixture
def simulator_factory(temp_config, storage_config):
    """Builds simulators wired to temp storage; closes them on teardown."""
    created = []

    def build(**kwargs):
        sim = _build_simulator(temp_config, storage_config, **kwargs)
        created.append(sim)
        return sim

    yield build
    for sim in created:
        try:
            sim.close()
        except Exception:  # pragma: no cover - teardown best effort
            pass


@pytest.fixture
def sim(simulator_factory):
    """A ready-to-run simulator on the default config with a fixed seed."""
    return simulator_factory(seed=42)


@pytest.fixture(scope="session")
def completed_run(tmp_path_factory):
    """One shared multi-day run, for assertions that only read the results.

    Session-scoped on purpose: these tests inspect the same dataset from
    different angles, and running a fresh simulation per test would make the
    suite far slower without testing anything more.
    """
    import shutil

    from pharma_sim.config.models import (
        EvaluationStorage,
        StorageConfig,
        TimeseriesStorage,
        TransactionalStorage,
    )

    root = tmp_path_factory.mktemp("shared_run")
    config_dir = root / "config"
    shutil.copytree(CONFIG_DIR, config_dir)
    storage = StorageConfig(
        transactional=TransactionalStorage(
            backend="sqlite", dsn=str(root / "factory.db"), batch_size=500
        ),
        timeseries=TimeseriesStorage(
            backend="parquet", dsn=str(root / "telemetry"), partition_by=["date"]
        ),
        evaluation=EvaluationStorage(backend="parquet", dsn=str(root / "eval")),
    )
    sim = _build_simulator(config_dir, storage, seed=42)
    sim.run(days=5)
    yield sim
    sim.close()
