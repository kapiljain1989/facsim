#!/usr/bin/env python
"""Walk one batch's full genealogy from the stored dataset.

Reads only what was persisted — no simulator state — so it demonstrates that the
traceability of §20 is a property of the data rather than of the process that
produced it.

    python scripts/trace_batch.py                     # pick a rejected batch
    python scripts/trace_batch.py --batch BATCH-2026-000123
    python scripts/trace_batch.py --failure FAIL-00007   # reverse direction
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pharma_sim.config.loader import load_config
from pharma_sim.storage.factory import build_storage


def _fmt(value) -> str:
    return "-" if value in (None, "") else str(value)


def _print_header(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def trace_batch(store, batch_id: str) -> int:
    batches = store.query("SELECT * FROM batches WHERE batch_id = ?", (batch_id,))
    if not batches:
        print(f"no batch {batch_id!r} in the dataset", file=sys.stderr)
        return 1
    batch = batches[0]

    print("=" * 74)
    print(f"BATCH {batch['batch_id']}   product {batch['product_id']}")
    print("=" * 74)
    print(f"  order            {_fmt(batch['order_id'])}")
    print(f"  planned quantity {_fmt(batch['planned_quantity'])}")
    print(f"  started          {_fmt(batch['started_at'])}")
    print(f"  completed        {_fmt(batch['completed_at'])}")
    print(f"  disposition      {batch['disposition']}")
    print(f"  route            {_fmt(batch['route'])}")

    _print_header("stages")
    stages = store.query(
        "SELECT * FROM batch_stages WHERE batch_id = ? ORDER BY sequence", (batch_id,)
    )
    print(f"  {'#':<3} {'stage':<16} {'machine':<10} {'result':<8} {'health':>7}  parameters")
    for stage in stages:
        parameters = stage["parameters"]
        if isinstance(parameters, str) and parameters:
            try:
                parameters = json.loads(parameters)
            except json.JSONDecodeError:
                parameters = {}
        summary = ", ".join(
            f"{name}={value:g}" for name, value in list((parameters or {}).items())[:3]
        )
        flag = "  <-- deviating: " + stage["deviating_parameters"] if stage[
            "deviating_parameters"
        ] else ""
        print(
            f"  {stage['sequence']:<3} {stage['stage']:<16} {stage['machine_id']:<10} "
            f"{stage['result']:<8} {float(stage['machine_health'] or 0):>7.3f}  {summary}{flag}"
        )

    _print_header("QC results")
    results = store.query(
        "SELECT * FROM qc_results WHERE batch_id = ? ORDER BY timestamp, parameter",
        (batch_id,),
    )
    print(
        f"  {'parameter':<24} {'phase':<11} {'actual':>10} {'target':>10} "
        f"{'limits':<20} result"
    )
    for result in results:
        limits = f"[{_fmt(result['lower_limit'])}, {_fmt(result['upper_limit'])}]"
        marker = "  <-- FAILED" if result["result"] in ("FAIL", "OOS") else ""
        print(
            f"  {result['parameter']:<24} {result['phase']:<11} "
            f"{float(result['actual_value']):>10.3f} {float(result['target']):>10.3f} "
            f"{limits:<20} {result['result']}{marker}"
        )

    if batch["failure_ids"]:
        _print_header("failures that touched this batch")
        for failure_id in batch["failure_ids"].split(","):
            rows = store.query("SELECT * FROM failures WHERE failure_id = ?", (failure_id,))
            for failure in rows:
                print(
                    f"  {failure['failure_id']}  {failure['machine_id']}  "
                    f"{failure['category']}/{failure['severity']}  "
                    f"detected {failure['detected_at']}  "
                    f"downtime {_fmt(failure['downtime_minutes'])} min"
                )
                print(f"      symptom: {failure['symptom']}")

    _print_header("deviations, investigations and actions")
    deviations = store.query(
        "SELECT * FROM deviations WHERE batch_id = ? OR deviation_id IN "
        "(SELECT deviation_id FROM deviations WHERE machine_id IN "
        " (SELECT machine_id FROM batch_stages WHERE batch_id = ?))",
        (batch_id, batch_id),
    )
    if not deviations:
        print("  none")
    for deviation in deviations:
        print(
            f"  {deviation['deviation_id']}  {deviation['severity']:<8} "
            f"{deviation['status']:<14} {deviation['title']}"
        )
        print(f"      {deviation['description']}")
        if deviation["rca_id"]:
            for report in store.query(
                "SELECT * FROM rca WHERE rca_id = ?", (deviation["rca_id"],)
            ):
                print(
                    f"      RCA {report['rca_id']}: {report['root_cause']} "
                    f"(confidence {float(report['confidence']):.2f})"
                )
                print(f"        evidence: {_fmt(report['evidence_summary'])}")
                for index, why in enumerate(
                    (report["five_why"] or "").split(" | "), start=1
                ):
                    if why:
                        print(f"        why {index}: {why}")
        if deviation["capa_id"]:
            for capa in store.query(
                "SELECT * FROM capa WHERE capa_id = ?", (deviation["capa_id"],)
            ):
                print(
                    f"      CAPA {capa['capa_id']} [{capa['status']}] "
                    f"verified {capa['verification_batches_passed']}"
                    f"/{capa['verification_batches_required']} batches"
                )
                print(f"        corrective: {capa['corrective_action']}")
                print(f"        preventive: {capa['preventive_action']}")

    _print_header("machines and operators involved")
    for machine_id in (batch["machines_used"] or "").split(","):
        if not machine_id:
            continue
        for machine in store.query(
            "SELECT m.machine_id, m.equipment_class, m.unit_id, u.name AS unit_name, "
            "(SELECT COUNT(*) FROM sensors s WHERE s.machine_id = m.machine_id) AS sensors "
            "FROM machines m JOIN units u ON u.unit_id = m.unit_id WHERE m.machine_id = ?",
            (machine_id,),
        ):
            print(
                f"  {machine['machine_id']:<10} {machine['equipment_class']:<26} "
                f"{machine['unit_name']:<26} {machine['sensors']} sensors"
            )
    print(f"  operators: {_fmt(batch['operators_involved'])}")
    print(f"  shifts:    {_fmt(batch['shift_instances'])}")
    return 0


def trace_failure(store, failure_id: str) -> int:
    """The reverse direction: failure to affected batches and products."""
    failures = store.query("SELECT * FROM failures WHERE failure_id = ?", (failure_id,))
    if not failures:
        print(f"no failure {failure_id!r} in the dataset", file=sys.stderr)
        return 1
    failure = failures[0]
    print("=" * 74)
    print(f"FAILURE {failure['failure_id']} on {failure['machine_id']}")
    print("=" * 74)
    print(f"  category    {failure['category']} / {failure['severity']}")
    print(f"  symptom     {failure['symptom']}")
    print(f"  detected    {failure['detected_at']}")
    print(f"  resolved    {_fmt(failure['resolved_at'])}")
    print(f"  downtime    {_fmt(failure['downtime_minutes'])} min")
    print(f"  operators   {_fmt(failure['operator_ids'])}")

    _print_header("affected batches")
    affected = [b for b in (failure["affected_batches"] or "").split(",") if b]
    if not affected:
        print("  none recorded")
    for batch_id in affected:
        for batch in store.query(
            "SELECT batch_id, product_id, disposition, qc_failure_count FROM batches "
            "WHERE batch_id = ?",
            (batch_id,),
        ):
            print(
                f"  {batch['batch_id']}  {batch['product_id']:<12} "
                f"{batch['disposition']:<12} {batch['qc_failure_count']} QC failures"
            )

    _print_header("maintenance")
    for record in store.query(
        "SELECT * FROM maintenance WHERE failure_id = ?", (failure_id,)
    ):
        print(
            f"  {record['maintenance_id']}  {record['maintenance_type']:<12} "
            f"{_fmt(record['duration_hours'])} h  cost {_fmt(record['cost'])}  "
            f"parts: {_fmt(record['parts_replaced'])}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config")
    parser.add_argument("--batch", help="batch id to trace")
    parser.add_argument("--failure", help="failure id to trace in reverse")
    args = parser.parse_args()

    config = load_config(args.config)
    storage = build_storage(config.storage, reset=False)
    storage.initialise()
    store = storage.relational
    try:
        if args.failure:
            return trace_failure(store, args.failure)
        batch_id = args.batch
        if not batch_id:
            # Prefer a batch with something interesting in it.
            candidates = store.query(
                "SELECT batch_id FROM batches WHERE disposition != 'RELEASED' "
                "ORDER BY qc_failure_count DESC LIMIT 1"
            ) or store.query("SELECT batch_id FROM batches LIMIT 1")
            if not candidates:
                print("no batches in the dataset; run a simulation first", file=sys.stderr)
                return 1
            batch_id = candidates[0]["batch_id"]
            print(f"(no --batch given; tracing {batch_id})")
        return trace_batch(store, batch_id)
    finally:
        storage.close()


if __name__ == "__main__":
    raise SystemExit(main())
