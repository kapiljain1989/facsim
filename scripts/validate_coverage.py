"""Validate that every table holds data across the whole simulated window.

Three classes of table need three different questions asked of them, and
conflating them produces a report that is either falsely reassuring or falsely
alarming:

``spanning``
    Fact tables.  Something happens, it gets a timestamp.  These must cover the
    window end to end, so an empty calendar month is a defect.
``derived``
    Facts with no timestamp of their own -- they inherit one through a foreign
    key (a production record is dated by its shift).  Same expectation, reached
    through a join.
``dimension`` / ``pre_run``
    Reference data (units, products, states) and rows that are deliberately
    *older* than the window (a machine commissioned in 2019).  Asking these to
    span six months is a category error: they are reported, never failed.

The window itself is read from ``runs``, so this makes no assumption about which
dates were requested.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pharma_sim.storage.schema import EVAL_TABLES, TABLES, TELEMETRY_TABLE  # noqa: E402

# Rows here predate the window by design; spanning it would be wrong.
PRE_RUN = {"machines": "commissioned_on", "employees": "hired_on"}
# One row per run / per config load -- no series to speak of.
SINGLETON = {"runs", "config_versions"}
# No timestamp column; dated through this foreign key instead.
DERIVED = {
    "production_records": "shift_instance_id",
    "oee_snapshots": "shift_instance_id",
    "rca_evidence": "rca_id",
}
# Preference order when a table carries several timestamps: we want the moment
# the row came into existence, not when it was closed out (which is often null).
ANCHOR_PRIORITY = (
    "timestamp", "detected_at", "entered_at", "opened_at", "started_at",
    "created_at", "scheduled_time", "business_date", "onset_at", "start_time",
)


def anchor_for(table) -> str | None:
    stamps = [c.name for c in table.columns if c.type in ("TIMESTAMP", "DATE")]
    for preferred in ANCHOR_PRIORITY:
        if preferred in stamps:
            return preferred
    return stamps[0] if stamps else None


def months_between(start: datetime, end: datetime) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def parse(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class SkipClickHouse(Exception):
    """Raised to bypass the ClickHouse section without reporting a failure."""


class Report:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, **kw) -> None:
        self.rows.append(kw)

    def assess(self, name, kind, stamps, expected_months, window):
        """Turn a list of timestamps into a verdict."""
        n = len(stamps)
        if n == 0:
            self.add(table=name, kind=kind, rows=0, verdict="FAIL",
                     note="no rows at all")
            return
        stamps = sorted(s for s in stamps if s is not None)
        if not stamps:
            self.add(table=name, kind=kind, rows=n, verdict="FAIL",
                     note="every timestamp is null")
            return

        hist = Counter(f"{s.year:04d}-{s.month:02d}" for s in stamps)
        present = set(hist)
        missing = [m for m in expected_months if m not in present]
        days = {s.date() for s in stamps}
        span_days = ((window[1] - window[0]).days or 1) + 1  # inclusive of both ends
        gap = max((b - a).days for a, b in zip(stamps, stamps[1:])) if n > 1 else span_days
        lead = (stamps[0] - window[0]).days
        trail = (window[1] - stamps[-1]).days
        mean_gap = span_days / n

        if kind in ("dimension", "pre_run", "singleton"):
            # Deliberately outside or independent of the window: report the
            # extent, but suppress coverage figures that would be nonsense.
            self.add(table=name, kind=kind, rows=n, first=stamps[0],
                     last=stamps[-1], verdict="INFO",
                     note=f"spans {stamps[0].date()}..{stamps[-1].date()}; "
                          "not expected to track the window")
            return
        elif missing:
            verdict = "FAIL"
            note = f"empty month(s): {', '.join(missing)}"
        elif mean_gap > 15:
            verdict = "SPARSE"
            note = f"mean gap {mean_gap:.1f} d - low-rate table, all months present"
        else:
            verdict, note = "PASS", ""

        self.add(table=name, kind=kind, rows=n, first=stamps[0], last=stamps[-1],
                 hist=hist, months=f"{len(present)}/{len(expected_months)}",
                 days=len(days), day_cover=100.0 * len(days) / span_days,
                 max_gap=gap, lead=lead, trail=trail,
                 verdict=verdict, note=note)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/plant/factory.db")
    ap.add_argument("--clickhouse", default="localhost")
    ap.add_argument("--ts-database", default="pharma_ts")
    ap.add_argument("--no-clickhouse", action="store_true",
                    help="skip telemetry/eval checks (parquet-backed datasets)")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    start, end = None, None
    for row in con.execute("SELECT sim_start, sim_end FROM runs"):
        start, end = parse(row["sim_start"]), parse(row["sim_end"])
    if start is None or end is None:
        print("cannot read the window from runs; aborting", file=sys.stderr)
        return 2
    expected = months_between(start, end)
    print(f"window from runs : {start}  ->  {end}   ({(end - start).days} days)")
    print(f"calendar months  : {', '.join(expected)}\n")

    rep = Report()
    existing = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    for name, table in TABLES.items():
        if name not in existing:
            rep.add(table=name, kind="missing", rows=0, verdict="FAIL",
                    note="table absent from the database")
            continue
        if name in DERIVED:
            fk = DERIVED[name]
            column = table.column(fk)
            if column is not None and column.references:
                parent, parent_key = column.references.split(".")
                undeclared = ""
            else:
                # No fk= in the schema; join on the identically named key.
                parent, parent_key = next(
                    (t.name, fk) for t in TABLES.values()
                    if fk in t.key_columns and t.name != name)
                undeclared = " (fk undeclared)"
            pcol = anchor_for(TABLES[parent])
            stamps = [parse(r[0]) for r in con.execute(
                f"SELECT p.{pcol} FROM {name} c "
                f"JOIN {parent} p ON c.{fk} = p.{parent_key}")]
            rep.assess(f"{name} -> {parent}.{pcol}",
                       f"derived via {parent}{undeclared}",
                       stamps, expected, (start, end))
            continue
        anchor = anchor_for(table)
        if anchor is None:
            n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            rep.add(table=name, kind="dimension", rows=n,
                    verdict="INFO" if n else "FAIL",
                    note="static reference data" if n else "no rows at all")
            continue
        kind = ("pre_run" if name in PRE_RUN else
                "singleton" if name in SINGLETON else "spanning")
        stamps = [parse(r[0]) for r in con.execute(f"SELECT {anchor} FROM {name}")]
        rep.assess(f"{name}.{anchor}", kind, stamps, expected, (start, end))

    # ---- ClickHouse: telemetry and evaluation ---------------------------- #
    try:
        if args.no_clickhouse:
            raise SkipClickHouse
        from clickhouse_driver import Client
        ch = Client(host=args.clickhouse)
        dbs = {r[0] for r in ch.execute("SELECT name FROM system.databases")}
        tel = TELEMETRY_TABLE.name
        by_month = dict(ch.execute(
            f"SELECT formatDateTime(ts, '%Y-%m') AS m, count() "
            f"FROM {args.ts_database}.{tel} GROUP BY m"))
        lo, hi, ndays = ch.execute(
            f"SELECT min(ts), max(ts), uniqExact(toDate(ts)) "
            f"FROM {args.ts_database}.{tel}")[0]
        missing = [m for m in expected if m not in by_month]
        total = sum(by_month.values())
        rep.add(table=f"{args.ts_database}.{tel}", kind="telemetry (clickhouse)",
                rows=total, first=lo, last=hi,
                months=f"{len(by_month)}/{len(expected)}", days=ndays,
                day_cover=100.0 * ndays / ((end - start).days or 1),
                max_gap="-", lead=(parse(lo) - start).days,
                trail=(end - parse(hi)).days,
                verdict="FAIL" if missing or not total else "PASS",
                note=f"empty month(s): {', '.join(missing)}" if missing else "")

        evdb = next((d for d in dbs if d not in
                     {"system", "default", "INFORMATION_SCHEMA",
                      "information_schema", args.ts_database}), None)
        if evdb:
            for name, table in EVAL_TABLES.items():
                anchor = anchor_for(table)
                stamps = [parse(r[0]) for r in ch.execute(
                    f"SELECT {anchor} FROM {evdb}.{name}")]
                rep.assess(f"{evdb}.{name}.{anchor}", "evaluation (clickhouse)",
                           stamps, expected, (start, end))
    except SkipClickHouse:
        pass
    except Exception as exc:  # noqa: BLE001 - report, don't mask
        rep.add(table="clickhouse", kind="telemetry", rows=0, verdict="FAIL",
                note=f"{type(exc).__name__}: {exc}")

    # ---- print ----------------------------------------------------------- #
    hdr = (f"{'table':38s} {'kind':22s} {'rows':>12s} {'months':>8s} "
           f"{'days':>6s} {'cover':>7s} {'gap':>5s} {'lead':>5s} {'trail':>6s} verdict")
    print(hdr)
    print("-" * len(hdr))
    for r in rep.rows:
        cover = f"{r['day_cover']:.0f}%" if "day_cover" in r else "-"
        print(f"{r['table']:38s} {r['kind']:22s} {r['rows']:>12,} "
              f"{str(r.get('months', '-')):>8s} {str(r.get('days', '-')):>6s} "
              f"{cover:>7s} {str(r.get('max_gap', '-')):>5s} "
              f"{str(r.get('lead', '-')):>5s} {str(r.get('trail', '-')):>6s} "
              f"{r['verdict']}"
              + (f"  <- {r['note']}" if r.get("note") else ""))

    # ---- per-month matrix: the direct answer to "is every month covered" -- #
    matrix = [r for r in rep.rows if r.get("hist")]
    if matrix:
        print(f"\nrows per calendar month\n{'table':38s}" +
              "".join(f"{m[-2:]+'/'+m[2:4]:>11s}" for m in expected))
        print("-" * (38 + 11 * len(expected)))
        for r in matrix:
            cells = "".join(
                f"{r['hist'].get(m, 0):>11,}" if r["hist"].get(m) else f"{'--':>11s}"
                for m in expected)
            print(f"{r['table'][:37]:38s}{cells}")

    tally = Counter(r["verdict"] for r in rep.rows)
    print("\n" + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    print(f"total tables checked: {len(rep.rows)}")
    return 1 if tally["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
