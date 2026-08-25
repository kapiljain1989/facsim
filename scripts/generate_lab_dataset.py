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
from pharma_sim.lab.stability import StabilityOutput, run_stability
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
    "stability_samples",
    "stability_tests",
    "stability_results",
    "stability_reviews",
    "stability_certificates",
    "stability_oos",
    "stability_trend",
    "stability_shelf_life",
    "stability_injections",
    "stability_peaks",
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


_README = """# Nelvorasib analytical development dataset

Synthetic HPLC data from an ICH Q2(R2) analytical method validation. Generated by
the pharmaceutical factory simulator; **no real data of any kind is in here**.

Nelvorasib is an invented molecule. The name follows the WHO naming convention
for its drug class (`-rasib`, used for KRAS inhibitors) so that it reads
correctly to a chemist, but it does not exist and neither does the study.

## What it is

A laboratory validated a method for measuring a cancer drug in tablets, and this
is everything that came out of the instruments while they did it. {injections}
injections across {sequences} runs, {peaks} peaks, and {results} pass/fail
checks against the protocol's acceptance criteria.

The main method measures {analyte_count} things in one {run_time:g}-minute run:
the drug itself, and {related_count} related substances that either come from the
manufacturing route or form when the drug degrades.

## Files

| File | Rows | What it is |
|---|---|---|
| `injections.csv` | {injections} | One row per sample injected, and the conditions it ran under |
| `peaks.csv` | {peaks} | One row per peak found. The measurements analysts actually use |
| `chromatogram_points.parquet` | {points} | The raw detector signal, ~4,500 points per injection |
| `sequences.csv` | {sequences} | Runs, grouped by which validation experiment they belong to |
| `system_suitability.csv` | {suitability} | The instrument check run before each batch of samples |
| `validation_results.csv` | {results} | Each protocol criterion, what was measured, and whether it passed |
| `audit_trail.csv` | {audit} | Who did what and when, in the shape a regulator expects |
| `substances.csv`, `methods.csv`, `method_analytes.csv`, `instruments.csv`, `columns.csv`, `analysts.csv`, `reference_standards.csv`, `excipients.csv` | small | Reference tables, so the data joins without needing the config |

## The columns that matter

In `peaks.csv`:

| Column | Meaning |
|---|---|
| `retention_time_min` | When the compound came off the column. Identifies *what* it is |
| `area` | How much of it there was. Identifies *how much* |
| `analyte_id`, `peak_name` | Which compound this peak was matched to. Blank means an unknown peak |
| `tailing_usp` | Peak symmetry. 1.0 is perfect, above 2.0 fails |
| `plate_count_usp` | Column sharpness. Higher is better; declines as the column wears out |
| `resolution_previous` | Separation from the peak before it. Below 2.0 fails |
| `signal_to_noise` | Peak height against background noise. Below ~10 is too small to trust |

Join `peaks.csv` to `injections.csv` on `injection_id`, and `injections.csv` to
`sequences.csv` on `sequence_id`.

## Things worth knowing before you use it

- **The numbers are measured, not made up.** A detector signal is simulated from
  the underlying chemistry, then a peak-detection algorithm reads it back with no
  knowledge of what went in. So areas, symmetry and separation all carry
  realistic run-to-run scatter, and they agree with each other.
- **It contains failures on purpose.** Two of the protocol's criteria fail. Both
  are the same finding: raising the solvent strength by 2%, or the column
  temperature by 5 C, makes two peaks merge so they can no longer be told apart.
  A real validation report would conclude that these two settings must be
  controlled tightly. Look in `validation_results.csv` where `verdict = FAIL`.
- **Small peaks are less accurate, as they should be.** A compound present at
  0.05% recovers about 94% of its true amount; at 1% it recovers 99%. Accuracy
  degrades at low levels and does so downwards, never upwards.
- **Reproducible.** Same seed, same numbers, byte for byte. This run used seed
  {seed}.
- **Not a regulatory record.** Realistic in shape and behaviour, but it is a
  simulation. Do not use it to support a filing.

## Loading it

```python
import pandas as pd

peaks = pd.read_csv("peaks.csv")
injections = pd.read_csv("injections.csv")
df = peaks.merge(injections, on="injection_id", suffixes=("", "_inj"))

# Impurity levels as a percentage of the main peak, per injection.
# Blanks, placebos and low-level solutions have no main peak, so this is NaN
# for those -- {main_peak_injections} of the {injections} injections have one.
main = df[df.analyte_id == "{assay_id}"].set_index("injection_id").area
df["percent_of_main"] = df.area / df.injection_id.map(main) * 100

# The raw signal for one injection. {example_injection} is a tablet sample
# showing every analyte the method looks for.
trace = pd.read_parquet("chromatogram_points.parquet")
one = trace[trace.injection_id == "{example_injection}"]
one.plot(x="time_min", y="response")
```

Full technical documentation: `docs/ANALYTICAL_DEVELOPMENT.md` in the simulator
repository.
"""


def _write_readme(
    path: Path,
    written: dict[str, int],
    trace_rows: int,
    seed: int,
    config,
    combined: dict[str, list[dict[str, Any]]],
) -> None:
    """Emit a dataset card beside the data.

    Every figure in it is derived from this run rather than written by hand --
    including the analyte count and the injection the example plots. A dataset
    description that has drifted from its dataset is worse than none, and the
    first draft of this one had two wrong numbers in it for exactly that reason.
    """
    method = config.methods.methods[0]
    assay_id = method.assay_analyte.analyte_id

    peaks = combined.get("peaks", [])
    per_injection: dict[str, int] = {}
    for row in peaks:
        per_injection[row["injection_id"]] = per_injection.get(row["injection_id"], 0) + 1

    # The example should plot something worth looking at: the sample injection
    # with the most peaks in it.
    sample_ids = {
        row["injection_id"]
        for row in combined.get("injections", [])
        if row.get("purpose") == "SAMPLE"
    }
    candidates = {i: n for i, n in per_injection.items() if i in sample_ids} or per_injection
    example = max(candidates, key=lambda key: candidates[key]) if candidates else "INJ-000001"

    with_main = len({row["injection_id"] for row in peaks if row["analyte_id"] == assay_id})

    path.write_text(
        _README.format(
            injections=f"{written.get('injections', 0):,}",
            peaks=f"{written.get('peaks', 0):,}",
            sequences=f"{written.get('sequences', 0):,}",
            suitability=f"{written.get('system_suitability', 0):,}",
            results=f"{written.get('validation_results', 0):,}",
            audit=f"{written.get('audit_trail', 0):,}",
            points=f"{trace_rows:,}" if trace_rows else "not generated",
            seed=seed,
            analyte_count=len(method.analytes),
            related_count=len(method.analytes) - 1,
            run_time=method.run_time_min,
            assay_id=assay_id,
            example_injection=example,
            main_peak_injections=f"{with_main:,}",
        )
    )


def _stability_batches(config, export: str | None):
    """Which batches go on stability.

    Real released batches of the product when a plant export is supplied. ICH
    requires three primary batches and they should be product that exists;
    placeholders are labelled so nothing reads them as real.
    """
    from datetime import date

    product_ids = {p.product_id for p in config.stability.protocols}
    if export:
        from pharma_sim.lifecycle.spine import load_released_batches
        from pharma_sim.lifecycle.config import load_lifecycle_config

        lifecycle = load_lifecycle_config("config/lifecycle")
        batches = [
            (batch.batch_id, batch.released_on)
            for batch in load_released_batches(export, lifecycle)
            if batch.product_id in product_ids
        ]
        if batches:
            return batches
        print(f"no batches of {sorted(product_ids)} in {export}; using placeholders",
              file=sys.stderr)
    return [(f"STUB-BATCH-{index:04d}", date(2026, 1, 15)) for index in range(1, 4)]


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
        "--manufacturing-export",
        help="a plant export. Stability runs on real released batches when given; "
             "without one the batch ids are placeholders labelled as such",
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

    # ---------------------------------------------------------------- stability
    stability_batches = _stability_batches(config, args.manufacturing_export)
    stability_outputs: list[StabilityOutput] = []
    for protocol in config.stability.protocols:
        print(f"running {protocol.protocol_id}  {protocol.title}")
        result = run_stability(
            config, protocol, stability_batches, RngRegistry(args.seed), ids,
            keep_traces=False,
        )
        stability_outputs.append(result)
        print(result.summary())
        print()

    root = Path(args.output)
    combined: dict[str, list[dict[str, Any]]] = _reference_rows(config)
    combined["sequences"] = [row for out in outputs for row in out.sequences]
    combined["injections"] = [row for out in outputs for row in out.injections]
    combined["peaks"] = [row for out in outputs for row in out.peaks]
    combined["system_suitability"] = [row for out in outputs for row in out.suitability]
    combined["validation_results"] = [row for out in outputs for row in out.results]
    combined["audit_trail"] = [row for out in outputs for row in out.audit]
    combined["stability_samples"] = [r for o in stability_outputs for r in o.samples]
    combined["stability_tests"] = [r for o in stability_outputs for r in o.tests]
    combined["stability_results"] = [r for o in stability_outputs for r in o.results]
    combined["stability_reviews"] = [r for o in stability_outputs for r in o.reviews]
    combined["stability_certificates"] = [r for o in stability_outputs for r in o.certificates]
    combined["stability_oos"] = [r for o in stability_outputs for r in o.out_of_specification]
    combined["stability_trend"] = [r for o in stability_outputs for r in o.trend]
    combined["stability_injections"] = [r for o in stability_outputs for r in o.injections]
    combined["stability_peaks"] = [r for o in stability_outputs for r in o.peaks]
    # The fitted shelf life, which is what dates a clinical lot.
    combined["stability_shelf_life"] = [
        {
            "protocol_id": o.protocol_id,
            "attribute": life.attribute,
            "shelf_life_months": life.months,
            "limit": life.limit,
            "intersection_months": round(life.intersection_months, 2),
            "slope_per_month": round(life.slope_per_month, 6),
            "residual_sd": round(life.residual_sd, 5),
            "points": life.points,
            "limiting": life.attribute == o.limiting_attribute,
            "limited_by_study_length": life.limited_by_study_length,
        }
        for o in stability_outputs
        for life in o.shelf_lives
    ]

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
    _write_readme(root / "README.md", written, trace_rows, args.seed, config, combined)

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
