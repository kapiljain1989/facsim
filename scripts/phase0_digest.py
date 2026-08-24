#!/usr/bin/env python
"""Content digest of a full simulation run, for refactor acceptance.

Phase 0 of the lifecycle extension (``docs/LIFECYCLE_EXTENSION.md``) is a pure
refactor: namespaced config, modular schema, registry bundles. None of it may
change a single generated row. This script is how that is proven.

    python scripts/phase0_digest.py --days 30 --out /tmp/base.json
    # ... refactor ...
    python scripts/phase0_digest.py --days 30 --out /tmp/after.json
    python scripts/phase0_digest.py --compare /tmp/base.json /tmp/after.json

Storage is redirected to a scratch directory, so a digest run never touches
``data/``.

Three columns are wall-clock and therefore legitimately non-deterministic:
``config_versions.created_at``, ``runs.started_at`` and ``runs.ended_at``. They
are blanked before hashing rather than dropped, so a change in their *presence*
or *position* still fails the digest.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

#: Columns whose value is wall-clock time, keyed by exported file stem.
_VOLATILE: dict[str, frozenset[str]] = {
    "config_versions": frozenset({"created_at"}),
    "runs": frozenset({"started_at", "ended_at"}),
}


def _hash_csv(path: Path) -> tuple[str, int]:
    """SHA-256 of a CSV with volatile columns blanked. Returns (digest, rows)."""
    volatile = _VOLATILE.get(path.stem, frozenset())
    digest = hashlib.sha256()
    rows = 0
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return digest.hexdigest(), 0
        blank = [index for index, name in enumerate(header) if name in volatile]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(header)
        for row in reader:
            for index in blank:
                if index < len(row):
                    row[index] = ""
            writer.writerow(row)
            rows += 1
        digest.update(buffer.getvalue().encode("utf-8"))
    return digest.hexdigest(), rows


def _parquet_rows(root: Path) -> int:
    """Row count across a parquet dataset, without hashing bytes.

    Parquet embeds writer metadata, so its bytes are not a stable digest. The
    row count plus the relational digest is enough: telemetry is derived from
    the same seeded streams as everything else.
    """
    if not root.exists():
        return 0
    try:
        import pyarrow.parquet as pq
    except ImportError:  # pragma: no cover
        return -1
    total = 0
    for file in sorted(root.rglob("*.parquet")):
        total += pq.ParquetFile(file).metadata.num_rows
    return total


def _digest(root: Path) -> dict:
    """Build a manifest over an exported dataset directory."""
    files: dict[str, dict] = {}
    for path in sorted(root.rglob("*.csv")):
        sha, rows = _hash_csv(path)
        files[str(path.relative_to(root))] = {"rows": rows, "sha256": sha}

    roll = hashlib.sha256()
    for name in sorted(files):
        roll.update(name.encode("utf-8"))
        roll.update(files[name]["sha256"].encode("utf-8"))

    return {
        "files": files,
        "file_count": len(files),
        "total_rows": sum(entry["rows"] for entry in files.values()),
        "digest": roll.hexdigest(),
    }


def run(config: str, days: float, seed: int | None, scratch: Path) -> dict:
    scratch.mkdir(parents=True, exist_ok=True)
    os.environ["PHARMA_TRANSACTIONAL_DSN"] = str(scratch / "factory.db")
    os.environ["PHARMA_TIMESERIES_DSN"] = str(scratch / "telemetry")
    os.environ["PHARMA_EVALUATION_DSN"] = str(scratch / "eval")

    from pharma_sim.exports.exporter import DatasetExporter
    from pharma_sim.simulator import Simulator

    sim = Simulator(config, seed=seed, reset_storage=True, configure_logs=False)
    try:
        summary = sim.run(days=days)
        report = sim.storage.verify_integrity()
        export_dir = scratch / "export"
        DatasetExporter(sim.storage, output_dir=str(export_dir), fmt="csv").export()
    finally:
        sim.close()

    manifest = _digest(export_dir)
    manifest["meta"] = {
        "config": config,
        "days": days,
        "seed": sim.config.plant.simulation.seed,
        "config_fingerprint": sim.fingerprint,
        "simulated_hours": round(summary.simulated_hours, 6),
        "events": summary.events,
        "telemetry_rows": _parquet_rows(scratch / "telemetry"),
        "eval_rows": _parquet_rows(scratch / "eval"),
        "integrity_ok": report.ok,
    }
    return manifest


def compare(left: Path, right: Path) -> int:
    a, b = json.loads(left.read_text()), json.loads(right.read_text())
    if a["digest"] == b["digest"]:
        print(f"IDENTICAL  {a['digest'][:16]}  "
              f"{a['file_count']} files, {a['total_rows']:,} rows")
        drift = [
            f"  {key}: {a['meta'].get(key)} -> {b['meta'].get(key)}"
            for key in sorted(set(a["meta"]) | set(b["meta"]))
            if a["meta"].get(key) != b["meta"].get(key)
        ]
        if drift:
            print("metadata differs (does not affect row content):")
            print("\n".join(drift))
        return 0

    print(f"DIFFERENT\n  {left.name}: {a['digest']}\n  {right.name}: {b['digest']}\n")
    names = sorted(set(a["files"]) | set(b["files"]))
    for name in names:
        old, new = a["files"].get(name), b["files"].get(name)
        if old == new:
            continue
        if old is None:
            print(f"  + {name}  ({new['rows']:,} rows)")
        elif new is None:
            print(f"  - {name}  ({old['rows']:,} rows)")
        else:
            note = "content" if old["rows"] == new["rows"] else \
                   f"rows {old['rows']:,} -> {new['rows']:,}"
            print(f"  ~ {name}  ({note})")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config")
    parser.add_argument("--days", type=float, default=30.0)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--out", type=Path, help="write the manifest here")
    parser.add_argument("--scratch", type=Path,
                        help="scratch dir for storage (default: a temp dir, removed after)")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("A", "B"),
                        help="compare two manifests and exit")
    args = parser.parse_args()

    if args.compare:
        return compare(*args.compare)

    keep = args.scratch is not None
    scratch = args.scratch or Path(tempfile.mkdtemp(prefix="pharma-digest-"))
    try:
        manifest = run(args.config, args.days, args.seed, scratch)
    finally:
        if not keep:
            shutil.rmtree(scratch, ignore_errors=True)

    meta = manifest["meta"]
    print(f"digest      {manifest['digest']}")
    print(f"files       {manifest['file_count']}")
    print(f"rows        {manifest['total_rows']:,} relational")
    print(f"telemetry   {meta['telemetry_rows']:,}")
    print(f"eval        {meta['eval_rows']:,}")
    print(f"fingerprint {meta['config_fingerprint'][:16]}  seed {meta['seed']}")
    print(f"integrity   {'ok' if meta['integrity_ok'] else 'FAILED'}")

    if args.out:
        args.out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        print(f"written     {args.out}")

    return 0 if meta["integrity_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
