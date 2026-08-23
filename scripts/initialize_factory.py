#!/usr/bin/env python
"""Build the factory and persist its topology, then report what was created.

Equivalent to `python -m pharma_sim init`, kept as a script because the brief's
project layout calls for it and because it is a convenient place to look when
asking "what exactly does initialisation write?".
"""

from __future__ import annotations

import argparse

from pharma_sim.logging_config import configure_logging
from pharma_sim.simulator import Simulator


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config")
    parser.add_argument("--seed", type=int)
    parser.add_argument("--keep", action="store_true", help="do not reset the stores")
    args = parser.parse_args()

    configure_logging(level="INFO")
    sim = Simulator(
        args.config, seed=args.seed, reset_storage=not args.keep, configure_logs=False
    )
    try:
        sim.start()
        status = sim.status()
        print(f"\nfactory {sim.plant.plant_id} initialised as run {sim.run_id}")
        for key, value in status["topology"].items():
            print(f"  {key:<12} {value}")
        print("\nper unit:")
        for unit in sim.plant.units.values():
            print(
                f"  {unit.unit_id}  {unit.spec.name:<28} stage={unit.process_stage:<16} "
                f"machines={len(unit.machines):<3} workers={len(unit.worker_ids)}"
            )
        print("\nstores:")
        for name, description in sim.storage.describe().items():
            print(f"  {name:<14} {description}")
        counts = sim.storage.written()
        print("\nrows written:")
        for table, count in counts.items():
            print(f"  {table:<22} {count:>8,}")
        sim.finish()
    finally:
        sim.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
