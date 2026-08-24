"""Reproducibility across processes.

``engine/rng.py`` claims that named streams make a run reproducible regardless of
interleaving. That claim is only true if nothing the engine iterates depends on
``PYTHONHASHSEED`` — and a single ``tuple({...})`` in the factory builder was
enough to break it, because the tuple is zipped against RNG draws when deciding
which equipment a worker is certified on.

The in-process tests cannot catch that class of bug: the hash seed is fixed for
the life of the interpreter, so two builds in one process always agree. These
tests therefore shell out with the seed set explicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Builds the plant and digests every field that a later RNG draw could depend
#: on. Deliberately does not run the simulation: a build-time divergence is what
#: cascades, and building is fast enough to run three times in a unit test.
_PROBE = """
import hashlib, json, sys
from datetime import datetime
from pharma_sim.config.loader import load_config
from pharma_sim.domain.plant import FactoryBuilder
from pharma_sim.engine.rng import RngRegistry
from pharma_sim.registry import Registries

config = load_config(sys.argv[1])
plant = FactoryBuilder(config, Registries.build(config), RngRegistry(42)).build(
    datetime(2026, 1, 1, 6, 0, 0)
)
payload = {
    "employees": [
        [e.employee_id, e.name, e.role, e.skill_level, e.shift_code,
         e.experience_years, e.attendance_probability,
         list(e.machine_certifications)]
        for e in plant.employees.values()
    ],
    "machines": [[m.machine_id, m.equipment_class, m.unit_id] for m in plant.machines.values()],
    # Order preserved, not sorted: sorting here would mask exactly the kind
    # of ordering bug this test exists to catch.
    "sensors": [s.sensor_id for m in plant.machines.values() for s in m.sensors.values()],
}
blob = json.dumps(payload, sort_keys=False, default=str)
print(hashlib.sha256(blob.encode()).hexdigest())
"""


def _digest(hash_seed: str, config_dir: Path) -> str:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(config_dir)],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("config_name", ["config", "config/examples/minimal_factory"])
def test_build_is_identical_across_hash_seeds(config_name: str) -> None:
    """The built factory must not depend on PYTHONHASHSEED.

    A set iterated into an ordered container is the usual cause. Three seeds
    rather than two, because a two-element set agrees half the time by chance.
    """
    config_dir = PROJECT_ROOT / config_name
    digests = {seed: _digest(seed, config_dir) for seed in ("0", "1", "12345")}
    assert len(set(digests.values())) == 1, (
        "factory build differs across PYTHONHASHSEED values — something the "
        f"builder iterates is hash-ordered: {digests}"
    )


def test_no_ordered_container_is_built_from_a_set() -> None:
    """Static guard on the pattern that caused it, so it cannot come back.

    ``tuple(set(...))`` / ``list({...})`` and iteration over a set literal are
    all reproducibility hazards the type checker will not flag.
    """
    import ast

    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "src" / "pharma_sim").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"tuple", "list"}
                and node.args
            ):
                arg = node.args[0]
                if isinstance(arg, (ast.SetComp, ast.DictComp)) or (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Name)
                    and arg.func.id in {"set", "frozenset"}
                ):
                    offenders.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno} "
                        f"{node.func.id}() over an unordered collection"
                    )
            if isinstance(node, (ast.For, ast.comprehension)):
                iterated = node.iter
                if isinstance(iterated, ast.SetComp) or (
                    isinstance(iterated, ast.Call)
                    and isinstance(iterated.func, ast.Name)
                    and iterated.func.id in {"set", "frozenset"}
                ):
                    offenders.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{iterated.lineno} "
                        "iteration over an unordered collection"
                    )

    assert not offenders, (
        "unordered collections materialised into ordered ones — use "
        "dict.fromkeys() to dedupe while preserving declaration order, or "
        "sorted() where a canonical order is wanted:\n  "
        + "\n  ".join(offenders)
    )
