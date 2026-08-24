#!/usr/bin/env python
"""Generate the analytical development dataset.

Runs every validation declared in ``config/lab/validation.yaml`` and writes the
result as CSV, plus the chromatogram traces as Parquet.

    python scripts/generate_lab_dataset.py --output data/lab
    python scripts/generate_lab_dataset.py --output data/lab --no-traces

The traces are the bulk of it: one 15-minute injection at 5 Hz is 4,501 points,
so a validation of ~250 injections carries about a million. They are written to
the columnar store rather than CSV for that reason, and can be skipped entirely
when only the peak table is wanted.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any, Iterable

from pharma_sim.engine.ids import IdFactory
from pharma_sim.engine.rng import RngRegistry
from pharma_sim.lab.loader import load_lab_config
from pharma_sim.lab.validation import ValidationOutput, ValidationRunner

#: Written as CSV. Order is deliberate: dimensions before the facts that
#: reference them, so the directory reads top-down.
_TABLES = (
    "substances",
    "excipients",
    "methods",
    "method_analytes",
    "instruments",
    "columns",
    "analysts",
    "reference_standards",
    "sequences",
    "injections",
    "peaks",
    "system_suitability",
    "validation_results",
    "audit_trail",
)


def _flatten(row: dict[str, Any]) -> dict[str, Any]:
    """One level of nesting into ``parent_child`` columns, for a flat CSV."""
    flat: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, dict):
            for inner, inner_value in value.items():
                flat[f"{key}_{inner}"] = inner_value
        elif isinstance(value, (list, tuple)):
            flat[key] = ",".join(str(item) for item in value)
        else:
            flat[key] = value
    return flat


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    rows = [_flatten(row) for row in rows]
    if not rows:
        path.write_text("")
        return 0
    # Union of keys, first-seen order: a robustness sequence carries condition
    # columns a specificity sequence does not.
    columns: list[str] = []
    for row in rows:
        for key in row:
            if key not in columns:
                columns.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


def _write_traces(path: Path, outputs: list[ValidationOutput]) -> int:
    """Chromatogram points, long-format, to Parquet."""
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        print("pyarrow not available; skipping traces", file=sys.stderr)
        return 0

    injection_ids: list[str] = []
    times: list[float] = []
    responses: list[float] = []
    for output in outputs:
        for injection_id, trace_times, trace_response in output.traces:
            injection_ids.extend([injection_id] * len(trace_times))
            times.extend(round(value, 6) for value in trace_times)
            responses.extend(round(value, 4) for value in trace_response)

    if not injection_ids:
        return 0
    table = pa.table(
        {
            "injection_id": pa.array(injection_ids, pa.string()),
            "time_min": pa.array(times, pa.float32()),
            "response": pa.array(responses, pa.float32()),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")
    return table.num_rows


def _reference_rows(config) -> dict[str, list[dict[str, Any]]]:
    """The declared vocabulary, so the dataset joins without the config."""
    return {
        "substances": [s.model_dump(mode="json") for s in config.substances.substances],
        "excipients": [e.model_dump(mode="json") for e in config.substances.excipients],
        "methods": [
            {
                k: v
                for k, v in m.model_dump(mode="json").items()
                if k != "analytes"
            }
            for m in config.methods.methods
        ],
        "method_analytes": [
            {"method_id": m.method_id, **a.model_dump(mode="json")}
            for m in config.methods.methods
            for a in m.analytes
        ],
        "instruments": [i.model_dump(mode="json") for i in config.instruments.instruments],
        "columns": [c.model_dump(mode="json") for c in config.instruments.columns],
        "analysts": [a.model_dump(mode="json") for a in config.instruments.analysts],
        "reference_standards": [
            s.model_dump(mode="json") for s in config.instruments.reference_standards
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default="config/lab")
    parser.add_argument("--output", default="data/lab")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--no-traces", action="store_true", help="skip the chromatogram points"
    )
    parser.add_argument(
        "--validation", action="append",
        help="run only this validation id; repeatable (default: all)",
    )
    args = parser.parse_args()

    started = time.monotonic()
    config = load_lab_config(args.config)
    wanted = set(args.validation) if args.validation else None
    selected = [
        validation
        for validation in config.validations.validations
        if wanted is None or validation.validation_id in wanted
    ]
    if not selected:
        print(f"no validations matched {sorted(wanted or [])}", file=sys.stderr)
        return 1

    # One id factory across all validations, so ids are unique dataset-wide.
    ids = IdFactory()
    outputs: list[ValidationOutput] = []
    for validation in selected:
        print(f"running {validation.validation_id}  {validation.title}")
        runner = ValidationRunner(
            config,
            validation,
            RngRegistry(args.seed),
            ids,
            keep_traces=not args.no_traces,
        )
        output = runner.run()
        outputs.append(output)
        print(output.summary())
        print()

    root = Path(args.output)
    combined: dict[str, list[dict[str, Any]]] = _reference_rows(config)
    combined["sequences"] = [row for out in outputs for row in out.sequences]
    combined["injections"] = [row for out in outputs for row in out.injections]
    combined["peaks"] = [row for out in outputs for row in out.peaks]
    combined["system_suitability"] = [row for out in outputs for row in out.suitability]
    combined["validation_results"] = [row for out in outputs for row in out.results]
    combined["audit_trail"] = [row for out in outputs for row in out.audit]

    written: dict[str, int] = {}
    for name in _TABLES:
        written[name] = _write_csv(root / f"{name}.csv", combined.get(name, []))

    trace_rows = 0
    if not args.no_traces:
        trace_rows = _write_traces(root / "chromatogram_points.parquet", outputs)

    failures = [row for out in outputs for row in out.failures]
    manifest = {
        "seed": args.seed,
        "config": args.config,
        "validations": [
            {
                "validation_id": out.validation_id,
                "method_id": out.method_id,
                "passed": out.passed,
                "judged_metrics": len(out.judged),
                "failed_metrics": [row["metric"] for row in out.failures],
            }
            for out in outputs
        ],
        "tables": written,
        "chromatogram_points": trace_rows,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"written to {root}")
    for name in _TABLES:
        print(f"  {name + '.csv':<28} {written[name]:>9,} rows")
    if trace_rows:
        print(f"  {'chromatogram_points.parquet':<28} {trace_rows:>9,} rows")
    print(f"\nelapsed {time.monotonic() - started:,.1f} s")
    if failures:
        print(f"\n{len(failures)} validation metric(s) did not meet acceptance criteria:")
        for row in failures:
            print(f"  {row['validation_id']}  {row['metric']}  "
                  f"measured {row['measured']}  criterion {row['criterion']}")
        print("\nThis is a result, not an error: the acceptance criteria were")
        print("evaluated against measured data and these did not meet them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
