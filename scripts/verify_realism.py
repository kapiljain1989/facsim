#!/usr/bin/env python
"""Check generated data against the envelopes in ``config/realism.yaml``.

The second credibility gate. ``verify-integrity`` and ``verify-spine`` say every
row resolves; this says the numbers are the right numbers.

    python scripts/verify_realism.py --plant data/plant --lab data/lab \
        --clinical data/clinical

It reads the exported files rather than re-running the simulation, so what is
checked is what was shipped. Every envelope in the config carries a reason, and
where one exists to catch a specific regression the failure message says which --
because an envelope nobody can justify is one somebody widens the first time it
fails.

Exit code 1 if any metric is outside its envelope, or if a metric the config asks
for could not be computed at all. A missing metric is a failure rather than a
skip: a check that silently stops running is worse than one that fails.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics as stats
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable

import yaml


def _read(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _number(row: dict[str, str], key: str) -> float | None:
    value = row.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _median_survival(times: list[float], events: list[bool]) -> float | None:
    """Kaplan-Meier median, in the units of ``times``."""
    if not times:
        return None
    survival = 1.0
    for time in sorted({t for t, e in zip(times, events) if e}):
        at_risk = sum(1 for t in times if t >= time)
        failures = sum(1 for t, e in zip(times, events) if e and t == time)
        if not at_risk:
            continue
        survival *= 1.0 - failures / at_risk
        if survival <= 0.5:
            return time
    return None


def _spearman(pairs: list[tuple[float, float]]) -> float | None:
    count = len(pairs)
    if count < 8:
        return None
    by_x = sorted(range(count), key=lambda i: pairs[i][0])
    by_y = sorted(range(count), key=lambda i: pairs[i][1])
    rank_x = {value: position for position, value in enumerate(by_x)}
    rank_y = {value: position for position, value in enumerate(by_y)}
    squared = sum((rank_x[i] - rank_y[i]) ** 2 for i in range(count))
    return 1.0 - 6.0 * squared / (count * (count * count - 1))


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def manufacturing_metrics(root: Path) -> dict[str, float]:
    batches = _read(root / "operational" / "batch_data.csv") or _read(
        root / "batch_data.csv"
    )
    qc = _read(root / "operational" / "qc_results.csv") or _read(root / "qc_results.csv")
    out: dict[str, float] = {}
    if not batches or not qc:
        return out

    nvr = [row for row in batches if row["product_id"].startswith("PRD-NVR")]
    if nvr:
        out["nvr_batches"] = float(len(nvr))
        settled = [row for row in nvr if row["disposition"] in {"RELEASED", "REJECTED"}]
        if settled:
            out["nvr_released_fraction"] = sum(
                1 for row in settled if row["disposition"] == "RELEASED"
            ) / len(settled)

    assay = [row for row in qc if row["parameter"] == "nvr_assay"]
    values = [v for row in assay if (v := _number(row, "actual_value")) is not None]
    if len(values) > 2:
        out["nvr_assay_mean_percent"] = stats.fmean(values)
        observed = stats.stdev(values) / stats.fmean(values)
        out["nvr_assay_rsd_percent"] = 100.0 * observed
        analytical = _number(assay[0], "analytical_rsd")
        size = _number(assay[0], "sample_size") or 1.0
        if analytical:
            floor = analytical / math.sqrt(size)
            out["nvr_assay_rsd_over_method_floor"] = observed / floor
    return out


def laboratory_metrics(root: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    results = _read(root / "validation_results.csv")
    if results:
        out["validation_failed_criteria"] = float(
            sum(1 for row in results if row["verdict"] == "FAIL")
        )
        loq = [
            v
            for row in results
            if row["metric"] == "LOQ_PERCENT_OF_STANDARD"
            and (v := _number(row, "measured")) is not None
        ]
        if loq:
            out["loq_percent_of_standard"] = loq[0]

    suitability = _read(root / "system_suitability.csv")
    passing = [row for row in suitability if row["verdict"] == "PASS"]
    rsd = [
        v for row in passing if (v := _number(row, "measured_area_rsd_percent")) is not None
    ]
    if rsd:
        out["sst_area_rsd_percent"] = stats.fmean(rsd)
    resolution = [
        v for row in passing if (v := _number(row, "measured_resolution")) is not None
    ]
    if resolution:
        out["critical_pair_resolution"] = stats.fmean(resolution)

    shelf = _read(root / "stability_shelf_life.csv")
    limiting = [row for row in shelf if row["limiting"].strip().lower() in {"true", "1"}]
    if limiting:
        months = _number(limiting[0], "shelf_life_months")
        if months is not None:
            out["stability_shelf_life_months"] = months
    impurity = [row for row in shelf if row["attribute"] == "total_impurities"]
    if impurity:
        slope = _number(impurity[0], "slope_per_month")
        declared = _declared_degradation_rate()
        if slope and declared:
            out["stability_impurity_slope_ratio"] = slope / declared
    return out


def _declared_degradation_rate() -> float | None:
    path = Path("config/lab/stability.yaml")
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw["kinetics"]["reference_rate_percent_per_month"]


def clinical_metrics(root: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    subjects = _read(root / "subjects.csv")
    rs = _read(root / "rs.csv")
    adtte = _read(root / "adtte.csv")
    exposure = _read(root / "exposure.csv")
    ae = _read(root / "ae.csv")
    tu = _read(root / "tu.csv")
    tmf = _read(root / "tmf_documents.csv")
    forms = _read(root / "forms.csv")
    queries = _read(root / "queries.csv")
    sites = _read(root / "sites.csv")
    if not subjects:
        return out

    arms = {row["USUBJID"]: row["ARM"] for row in subjects}
    weeks_per_month = 52.1775 / 12.0

    # Response
    best = [row for row in rs if row["RSTESTCD"] == "BESTRESP" and row["RSEVALID"] == "BICR"]
    for arm, key in (("ARM-A", "orr_arm_a"), ("ARM-B", "orr_arm_b")):
        rows = [row for row in best if arms.get(row["USUBJID"]) == arm]
        if rows:
            out[key] = sum(1 for row in rows if row["RSSTRESC"] in {"CR", "PR"}) / len(rows)
    if "orr_arm_a" in out and "orr_arm_b" in out:
        out["orr_difference"] = out["orr_arm_a"] - out["orr_arm_b"]

    # Survival
    for arm, key in (
        ("ARM-A", "median_pfs_months_arm_a"),
        ("ARM-B", "median_pfs_months_arm_b"),
    ):
        rows = [
            row
            for row in adtte
            if row["PARAMCD"] == "PFS" and row["EVAL"] == "BICR" and arms.get(row["USUBJID"]) == arm
        ]
        times = [(_number(row, "AVAL") or 0.0) / 7.0 for row in rows]
        events = [row["CNSR"] == "0" for row in rows]
        median = _median_survival(times, events)
        if median is not None:
            out[key] = median / weeks_per_month

    # Reader disagreement, and the mechanism behind it
    paired: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    for row in rs:
        if row["RSTESTCD"] == "OVRLRESP":
            paired[(row["USUBJID"], row["VISITNUM"])][row["RSEVALID"]] = row["RSSTRESC"]
    both = [values for values in paired.values() if len(values) == 2]
    if both:
        out["reader_discordance"] = sum(
            1 for values in both if len(set(values.values())) > 1
        ) / len(both)

    targets: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in tu:
        if row["TUORRES"] == "TARGET":
            targets[row["USUBJID"]][row["TUEVALID"]].add(row["TULNKID"])
    complete = {s: v for s, v in targets.items() if len(v) == 2}
    if complete:
        out["different_target_selection"] = sum(
            1 for v in complete.values() if len(set(map(frozenset, v.values()))) > 1
        ) / len(complete)

    # Exposure, and whether it predicts outcome
    intensity = {
        row["subject_id"]: _number(row, "relative_dose_intensity") or 0.0
        for row in exposure
    }
    by_arm: dict[str, list[float]] = defaultdict(list)
    for row in exposure:
        by_arm[row["arm"]].append(intensity[row["subject_id"]])
    if by_arm.get("ARM-A"):
        out["relative_dose_intensity_arm_a"] = stats.fmean(by_arm["ARM-A"])
    if by_arm.get("ARM-A") and by_arm.get("ARM-B"):
        out["rdi_arm_a_below_arm_b"] = float(
            stats.fmean(by_arm["ARM-A"]) < stats.fmean(by_arm["ARM-B"])
        )

    # Exposure against DEPTH OF RESPONSE rather than against survival time.
    #
    # The obvious statistic -- rank correlation of dose intensity with
    # progression-free survival -- is computable only on subjects who had an
    # event, which is about forty per arm. At that size the standard error on a
    # rank correlation is around 0.16, so a real relationship of 0.28 reads
    # anywhere from -0.05 to 0.6 depending on the seed. As a gate it fires at
    # random.
    #
    # Depth of response is available for every subject, censored or not, and it is
    # what the exposure acts on directly. The best percentage change from
    # baseline is the deepest the tumour got, and more drug should make it more
    # negative -- so the correlation is expected to be NEGATIVE, and its
    # magnitude is what the envelope bounds.
    deepest: dict[str, float] = {}
    for row in rs:
        if row["RSTESTCD"] != "OVRLRESP" or row["RSEVALID"] != "BICR":
            continue
        change = _number(row, "PCHGBASE")
        if change is None:
            continue
        subject = row["USUBJID"]
        deepest[subject] = min(deepest.get(subject, 0.0), change)

    for arm, key in (
        ("ARM-A", "exposure_response_rho_arm_a"),
        ("ARM-B", "exposure_response_rho_arm_b"),
    ):
        pairs = [
            (intensity[subject], change)
            for subject, change in deepest.items()
            if arms.get(subject) == arm and subject in intensity
        ]
        rho = _spearman(pairs)
        if rho is not None:
            out[key] = rho

    # Adverse events
    if ae:
        out["serious_ae_fraction"] = sum(1 for row in ae if row["AESER"] == "Y") / len(ae)
        grades = [_number(row, "AETOXGR") or 0 for row in ae]
        out["grade_3_plus_fraction"] = sum(1 for g in grades if g >= 3) / len(grades)
        counts: dict[str, Counter] = {"ARM-A": Counter(), "ARM-B": Counter()}
        totals = Counter(arms.values())
        for row in ae:
            arm = arms.get(row["USUBJID"])
            if arm in counts:
                counts[arm][row["AEDECOD"]] += 1

        def ratio(terms: list[str]) -> float | None:
            a = sum(counts["ARM-A"][t] for t in terms) / max(totals["ARM-A"], 1)
            b = sum(counts["ARM-B"][t] for t in terms) / max(totals["ARM-B"], 1)
            return a / b if b > 0 else None

        backbone = ratio(["Anaemia", "Neutrophil count decreased", "Platelet count decreased"])
        if backbone is not None:
            out["backbone_ae_arm_ratio"] = backbone
        product = ratio(["Diarrhoea", "Alanine aminotransferase increased"])
        if product is not None:
            out["product_ae_arm_ratio"] = product

    # Trial master file and data quality
    if tmf:
        out["tmf_completeness"] = sum(1 for row in tmf if row["status"] == "FILED") / len(tmf)
    if forms and queries:
        out["query_rate_per_form"] = len(queries) / len(forms)
        per_site_forms = Counter(row["site_id"] for row in forms)
        per_site_queries = Counter(row["site_id"] for row in queries)
        mean = len(queries) / len(forms)
        rates = [
            (per_site_queries[site] / count) / mean
            for site, count in per_site_forms.items()
            if count
        ]
        if rates:
            out["worst_site_query_multiple"] = max(rates)

    below = [
        row
        for row in rs
        if row["RSTESTCD"] == "OVRLRESP"
        and row["RSSTRESC"] == "PD"
        and (value := _number(row, "PCHGBASE")) is not None
        and value < -30.0
    ]
    out["progression_below_baseline"] = float(len(below))
    del sites
    return out


def spine_metrics(root: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    dosing = _read(root / "dosing.csv")
    kits = {row["kit_number"]: row for row in _read(root / "imp_kits.csv")}
    lots = {row["lot_id"] for row in _read(root / "imp_lots.csv")}
    if dosing:
        resolved = sum(
            1
            for row in dosing
            if row["kit_number"] in kits and kits[row["kit_number"]]["lot_id"] in lots
        )
        out["doses_traced_to_batches"] = resolved / len(dosing)
    manifest = root / "manifest.json"
    if manifest.exists():
        payload = json.loads(manifest.read_text())
        checks = payload.get("spine_checks")
        if isinstance(checks, dict) and checks.get("total"):
            out["spine_checks_passed"] = checks["passed"] / checks["total"]
    return out


def process_development_metrics(root: Path) -> dict[str, float]:
    """Does the plant run the settings the development work chose?

    Read from the laboratory export's selected optimum and the plant's declared
    process parameters. If these drift apart the design of experiments is
    decoration and manufacturing is running numbers nobody selected -- which is
    exactly the state this whole link was built to end.
    """
    out: dict[str, float] = {}
    optimum = _read(root / "doe_optimum.csv")
    if not optimum:
        return out

    links_path = Path("config/lifecycle/links.yaml")
    products_path = Path("config/products.yaml")
    doe_path = Path("config/lab/doe.yaml")
    if not (links_path.exists() and products_path.exists() and doe_path.exists()):
        return out

    links = yaml.safe_load(links_path.read_text(encoding="utf-8"))
    products = yaml.safe_load(products_path.read_text(encoding="utf-8"))
    tolerances = yaml.safe_load(doe_path.read_text(encoding="utf-8"))["optimisation"][
        "setpoint_tolerance"
    ]
    development = links.get("process_development")
    if not development:
        return out

    row = next(
        (r for r in optimum if r["study_id"] == development["doe_study"]), optimum[0]
    )
    product = next(
        (
            p
            for p in products["products"]
            if p["product_id"] == development["product_id"]
        ),
        None,
    )
    if product is None:
        return out

    declared: dict[str, float] = {}
    for parameters in product["process_parameters"].values():
        for name, window in parameters.items():
            declared[name] = float(window["target"])

    agreements: list[float] = []
    for setpoint in development["setpoints"]:
        chosen = _number(row, f"optimum_{setpoint['doe_factor']}")
        running = declared.get(setpoint["process_parameter"])
        if chosen is None or running is None:
            continue
        tolerance = float(tolerances.get(setpoint["doe_factor"], 0.0))
        agreements.append(1.0 if abs(chosen - running) <= tolerance else 0.0)
    if agreements:
        out["doe_setpoint_agreement"] = stats.fmean(agreements)

    selected = row.get("selected_formulation")
    if selected:
        out["doe_selected_declared_formulation"] = float(
            selected == development["formulation"]
        )
    return out


_SECTIONS: dict[str, Callable[[Path], dict[str, float]]] = {
    "manufacturing": manufacturing_metrics,
    "laboratory": laboratory_metrics,
    "clinical": clinical_metrics,
    "spine": spine_metrics,
    "process_development": process_development_metrics,
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default="config/realism.yaml")
    parser.add_argument("--plant", help="manufacturing export directory")
    parser.add_argument("--lab", help="laboratory export directory")
    parser.add_argument("--clinical", help="clinical export directory")
    args = parser.parse_args()

    envelopes: dict[str, list[dict[str, Any]]] = yaml.safe_load(
        Path(args.config).read_text(encoding="utf-8")
    )
    supplied = {
        "manufacturing": args.plant,
        "laboratory": args.lab,
        "clinical": args.clinical,
        "spine": args.clinical,
        "process_development": args.lab,
    }

    failures: list[str] = []
    missing: list[str] = []
    checked = 0

    for section, expectations in envelopes.items():
        directory = supplied.get(section)
        if not directory:
            print(f"{section}: skipped, no export supplied")
            continue
        measured = _SECTIONS[section](Path(directory))
        print(f"\n{section}")
        for expectation in expectations:
            name = expectation["metric"]
            low, high = float(expectation["low"]), float(expectation["high"])
            value = measured.get(name)
            if value is None:
                # Not a skip. A check that stopped running is worse than one that
                # fails, because nothing draws attention to it.
                print(f"  MISSING  {name:<36} could not be computed")
                missing.append(f"{section}.{name}")
                continue
            checked += 1
            ok = low <= value <= high
            mark = "ok     " if ok else "OUTSIDE"
            print(f"  {mark}  {name:<36} {value:>10.4f}   expected {low:g} to {high:g}")
            if not ok:
                failures.append(f"{section}.{name} = {value:.4f}, expected {low:g}-{high:g}")
                reason = " ".join(str(expectation.get("why", "")).split())
                if reason:
                    print(f"           why: {reason}")

    print(f"\n{checked} metrics checked")
    if missing:
        print(f"{len(missing)} could not be computed:")
        for name in missing:
            print(f"  {name}")
    if failures:
        print(f"\n{len(failures)} outside envelope:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    if missing:
        return 1
    print("every metric is inside its declared envelope")
    return 0


if __name__ == "__main__":
    sys.exit(main())
