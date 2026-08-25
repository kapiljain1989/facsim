"""Dataset export.

Produces the §28 file set. Two rules are enforced here rather than left to
convention:

* **Operational and evaluation data are written to separate directories.** Ground
  truth and prediction labels never appear alongside the operational tables, so a
  training job that globs the operational directory cannot accidentally read the
  answers.
* **Telemetry is referenced, not copied.** The time-series store already holds
  ``sensor_readings`` in its own format; re-serialising tens of millions of rows
  into CSV would be slow and pointless. Its location is reported instead.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pharma_sim.storage.facade import StorageFacade
from pharma_sim.storage.schema import TABLES

__all__ = ["DatasetExporter", "ExportResult"]

logger = logging.getLogger(__name__)

#: Operational tables, in the §28 order, mapped to their output file stem.
_OPERATIONAL_EXPORTS: dict[str, str] = {
    "production_records": "production",
    "machine_events": "machine_events",
    "failures": "machine_failures",
    "employee_events": "employee_events",
    "shift_instances": "shift_data",
    "batches": "batch_data",
    "batch_stages": "batch_stages",
    "qc_results": "qc_results",
    "maintenance": "maintenance",
    "deviations": "deviations",
    "rca": "rca",
    "rca_evidence": "rca_evidence",
    "capa": "capa",
    "machine_state_history": "machine_state_history",
    "oee_snapshots": "oee",
    "events": "events",
}

#: Topology / reference tables, useful for joins.
_REFERENCE_EXPORTS: dict[str, str] = {
    "plants": "plants",
    "units": "units",
    "equipment_classes": "equipment_classes",
    "machines": "machines",
    "sensors": "sensors",
    "plc_tags": "plc_tags",
    "employees": "employees",
    "products": "products",
    "shifts": "shifts",
    "states": "states",
    "event_types": "event_types",
    "runs": "runs",
    "config_versions": "config_versions",
}


@dataclass(slots=True)
class ExportResult:
    files: dict[str, int] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)
    telemetry_location: str = ""
    evaluation_location: str = ""

    @property
    def total_rows(self) -> int:
        return sum(self.files.values())


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, default=str, sort_keys=True)
    return str(value)


class DatasetExporter:
    """Writes the stored dataset out as files."""

    def __init__(
        self,
        storage: StorageFacade,
        *,
        output_dir: str | Path = "exports",
        fmt: str = "both",
        chunk_size: int = 50_000,
    ) -> None:
        self._storage = storage
        self._root = Path(output_dir)
        self._fmt = fmt
        self._chunk = chunk_size

    def export(self) -> ExportResult:
        self._storage.flush()
        result = ExportResult()

        operational = self._root / "operational"
        reference = self._root / "reference"
        operational.mkdir(parents=True, exist_ok=True)
        reference.mkdir(parents=True, exist_ok=True)

        for table, stem in _REFERENCE_EXPORTS.items():
            self._export_table(table, reference / stem, result)
        for table, stem in _OPERATIONAL_EXPORTS.items():
            self._export_table(table, operational / stem, result)

        # Evaluation data goes somewhere a training job will not glob by accident.
        result.evaluation_location = str(
            getattr(self._storage.evaluation, "root", self._storage.evaluation.describe)
        )
        result.telemetry_location = str(
            getattr(self._storage.telemetry, "root", self._storage.telemetry.describe)
        )
        self._write_manifest(result)
        return result

    def _export_table(self, table: str, stem: Path, result: ExportResult) -> None:
        spec = TABLES.get(table)
        if spec is None:
            return
        try:
            rows = self._storage.relational.query(f"SELECT * FROM {table}")
        except Exception as exc:  # pragma: no cover - backend specific
            logger.warning("could not export %s: %s", table, exc)
            return
        if not rows:
            result.skipped.add(table)
            return

        columns = list(spec.column_names)
        if self._fmt in {"csv", "both"}:
            path = stem.with_suffix(".csv")
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(columns)
                for row in rows:
                    writer.writerow([_stringify(row.get(name)) for name in columns])
        if self._fmt in {"parquet", "both"}:
            self._write_parquet(stem.with_suffix(".parquet"), spec, rows)

        result.files[stem.name] = len(rows)

    @staticmethod
    def _write_parquet(path: Path, spec, rows: list[dict[str, Any]]) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        # Everything is exported as strings apart from numerics: values arrive
        # from the relational backend already flattened, and a stable schema is
        # more useful downstream than a guessed one.
        fields = []
        columns: dict[str, list[Any]] = {}
        for column in spec.columns:
            values = [row.get(column.name) for row in rows]
            if column.type in {"REAL"}:
                fields.append(pa.field(column.name, pa.float64()))
                columns[column.name] = [
                    None if v is None else float(v) for v in values
                ]
            elif column.type in {"INTEGER"}:
                fields.append(pa.field(column.name, pa.int64()))
                columns[column.name] = [None if v is None else int(v) for v in values]
            elif column.type == "BOOLEAN":
                fields.append(pa.field(column.name, pa.bool_()))
                columns[column.name] = [None if v is None else bool(v) for v in values]
            else:
                fields.append(pa.field(column.name, pa.string()))
                columns[column.name] = [
                    None if v is None else _stringify(v) for v in values
                ]
        pq.write_table(
            pa.table(columns, schema=pa.schema(fields)), path, compression="zstd"
        )

    def _write_manifest(self, result: ExportResult) -> None:
        """Describe what was written, including where the answers live."""
        manifest = {
            "operational_tables": sorted(result.files),
            "row_counts": result.files,
            "empty_tables": sorted(result.skipped),
            "telemetry": {
                "location": result.telemetry_location,
                "note": (
                    "High-frequency sensor readings live in the time-series store "
                    "in its native format and are not duplicated here."
                ),
            },
            "evaluation": {
                "location": result.evaluation_location,
                "contents": ["ground_truth_events", "prediction_labels"],
                "warning": (
                    "Hidden ground truth and forward-looking labels. Kept apart "
                    "from the operational export on purpose: joining them into "
                    "training features would leak the answer and invalidate any "
                    "evaluation done with this dataset."
                ),
            },
            "storage": self._storage.describe(),
        }
        (self._root / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str), encoding="utf-8"
        )
