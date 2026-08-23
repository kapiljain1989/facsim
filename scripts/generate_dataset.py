#!/usr/bin/env python
"""Generate a full dataset and export it.

This is the one-command path from an empty directory to a complete synthetic
dataset with its evaluation labels.

    python scripts/generate_dataset.py --days 30 --output data/export
"""

from __future__ import annotations

import argparse
import time

from pharma_sim.exports.exporter import DatasetExporter
from pharma_sim.logging_config import configure_logging
from pharma_sim.simulator import Simulator


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config")
    parser.add_argument("--days", type=float, default=30.0)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--output", default="data/export")
    parser.add_argument("--format", default="both", choices=["csv", "parquet", "both"])
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--sensor-interval-s",
        type=float,
        help="override the telemetry cadence; lower means far more rows",
    )
    args = parser.parse_args()

    configure_logging(level="INFO")
    sim = Simulator(
        args.config, seed=args.seed, reset_storage=not args.keep, configure_logs=False
    )
    if args.sensor_interval_s:
        object.__setattr__(
            sim.config.plant.simulation,
            "sensor_sample_interval_s",
            args.sensor_interval_s,
        )
        sim.telemetry.set_interval(args.sensor_interval_s)

    try:
        started = time.monotonic()
        summary = sim.run(days=args.days)
        print("\n" + summary.render())

        print("\nverifying referential integrity across stores")
        report = sim.storage.verify_integrity()
        print(report.render())
        if not report.ok:
            print("\nintegrity checks FAILED", file=__import__("sys").stderr)
            return 1

        result = DatasetExporter(
            sim.storage, output_dir=args.output, fmt=args.format
        ).export()
        print(f"\nexported {result.total_rows:,} rows to {args.output}")
        for name, count in sorted(result.files.items()):
            print(f"  {name:<30} {count:>12,}")
        print(f"\n  telemetry stays in    {result.telemetry_location}")
        print(f"  evaluation labels in  {result.evaluation_location}")
        print(f"\ntotal wall time {time.monotonic() - started:,.0f}s")
    finally:
        sim.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
