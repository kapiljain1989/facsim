"""Command-line interface.

``python -m pharma_sim <command>``. Subcommands are deliberately thin: each one
validates its arguments, builds a :class:`~pharma_sim.simulator.Simulator`, and
prints what happened. Anything interesting lives in the engines.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from pharma_sim.config.errors import ConfigError
from pharma_sim.config.linter import lint_config
from pharma_sim.config.loader import load_config
from pharma_sim.exports.exporter import DatasetExporter
from pharma_sim.logging_config import configure_logging
from pharma_sim.simulator import Simulator

logger = logging.getLogger("pharma_sim")

DEFAULT_CONFIG = "config"


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pharma_sim",
        description="Config-driven pharmaceutical factory simulator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  pharma_sim validate\n"
            "  pharma_sim init\n"
            "  pharma_sim run --days 30\n"
            "  pharma_sim run --hours 24 --speed 10\n"
            "  pharma_sim run --live --speed 1 --sink jsonl\n"
            "  pharma_sim run --days 7 --then-live --sink mqtt\n"
            "  pharma_sim inject-failure --machine TP-006 --failure BEARING_FAILURE\n"
            "  pharma_sim scenario MACHINE_FAILURE\n"
            "  pharma_sim export --output ./data/export\n"
            "  pharma_sim serve --port 8000\n"
            "  pharma_sim serve --live --speed 120\n"
        ),
    )
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG, help="configuration directory (default: config)"
    )
    parser.add_argument("--seed", type=int, help="override the configured random seed")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    parser.add_argument("--json-logs", action="store_true", help="emit logs as JSON lines")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="lint the configuration and report every problem")

    schema = sub.add_parser("schema", help="emit JSON Schema for the config files")
    schema.add_argument("--output", default="schemas", help="output directory")

    init = sub.add_parser("init", help="build the factory and persist its topology")
    init.add_argument(
        "--keep",
        action="store_true",
        help="keep existing data instead of resetting the stores",
    )

    sub.add_parser("status", help="build the factory and print a status snapshot")
    sub.add_parser(
        "verify-integrity", help="run cross-store referential integrity checks"
    )

    run = sub.add_parser("run", help="run the simulation")
    run.add_argument("--days", type=float, default=0.0)
    run.add_argument("--hours", type=float, default=0.0)
    run.add_argument("--minutes", type=float, default=0.0)
    run.add_argument(
        "--speed",
        type=float,
        help="simulated minutes per real second while paced (live mode)",
    )
    run.add_argument("--live", action="store_true", help="stream indefinitely at wall pace")
    run.add_argument(
        "--then-live",
        action="store_true",
        help="fast-forward the requested span, then keep streaming live",
    )
    run.add_argument(
        "--sink",
        default="",
        help="comma-separated sink names from sinks.yaml, e.g. jsonl,mqtt",
    )
    run.add_argument("--keep", action="store_true", help="append to existing data")
    run.add_argument(
        "--max-wall-seconds",
        type=float,
        help="safety stop for live mode, in real seconds",
    )
    run.add_argument("--export", help="export the dataset to this directory afterwards")

    inject = sub.add_parser(
        "inject-failure", help="inject a failure and let it propagate"
    )
    inject.add_argument("--machine", required=True)
    inject.add_argument("--failure", required=True)
    inject.add_argument("--severity", default=None)
    inject.add_argument(
        "--hours",
        type=float,
        default=48.0,
        help="simulated hours to run after injection (default: 48)",
    )
    inject.add_argument("--warmup-hours", type=float, default=6.0)
    inject.add_argument("--export", help="export the dataset afterwards")

    scenario = sub.add_parser("scenario", help="run a predefined scenario")
    scenario.add_argument("name", help="scenario id from scenarios.yaml")
    scenario.add_argument("--export", help="export the dataset afterwards")
    scenario.add_argument("--list", action="store_true", help="list scenarios and exit")

    serve = sub.add_parser("serve", help="serve the read-only API and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--live",
        action="store_true",
        help="host a running plant and stream it to the dashboard",
    )
    serve.add_argument(
        "--speed",
        type=float,
        default=120.0,
        help="simulated minutes per real second in live mode (default: 120)",
    )
    serve.add_argument(
        "--warmup-hours",
        type=float,
        default=6.0,
        help="hours to fast-forward before going live, so the plant is already busy",
    )
    serve.add_argument(
        "--sink",
        default="",
        help="additional sinks to enable alongside the dashboard feed",
    )

    export = sub.add_parser("export", help="export the stored dataset to files")
    export.add_argument("--output", default="exports", help="output directory")
    export.add_argument(
        "--format",
        default="both",
        choices=["csv", "parquet", "both"],
        help="output format for relational tables",
    )
    return parser


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_validate(args) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    issues = lint_config(config)
    if issues:
        print(f"{len(issues)} configuration issue(s) in {args.config}:", file=sys.stderr)
        for issue in issues:
            print(issue.render(), file=sys.stderr)
        return 1

    print(f"configuration in {args.config} is valid and internally consistent")
    print(f"  units             {len(config.units.units)}")
    print(f"  equipment classes {len(config.machines.equipment_classes)}")
    print(
        "  machines          "
        f"{sum(g.count for groups in config.machines.layout.values() for g in groups)}"
    )
    print(f"  sensor profiles   {len(config.sensors.profiles)}")
    print(f"  states            {len(config.states.states)}")
    print(f"  event types       {len(config.event_types.event_types)}")
    print(f"  products          {len(config.products.products)}")
    print(f"  qc parameters     {len(config.qc_rules.parameters)}")
    print(f"  failure modes     {len(config.failures.failure_modes)}")
    print(f"  rca rules         {len(config.rca_rules.rules)}")
    print(f"  scenarios         {len(config.scenarios.scenarios)}")
    return 0


def cmd_schema(args) -> int:
    from pharma_sim.config.models import CONFIG_FILES, FactoryConfig

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    written = 0
    for stem, field_name in CONFIG_FILES.items():
        model = FactoryConfig.model_fields[field_name].annotation
        schema = model.model_json_schema()  # type: ignore[union-attr]
        path = output / f"{stem}.schema.json"
        path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        written += 1
    print(f"wrote {written} JSON Schema file(s) to {output}")
    return 0


def cmd_init(args) -> int:
    sim = _build(args, reset=not args.keep)
    sim.start()
    print(f"factory initialised: run {sim.run_id}")
    _print_status(sim.status())
    sim.finish()
    sim.close()
    return 0


def cmd_status(args) -> int:
    sim = _build(args, reset=False)
    _print_status(sim.status())
    print("\nstorage")
    for name, description in sim.storage.describe().items():
        print(f"  {name:<14} {description}")
    sim.close()
    return 0


def cmd_verify_integrity(args) -> int:
    sim = _build(args, reset=False)
    report = sim.storage.verify_integrity()
    print("cross-store referential integrity")
    print(report.render())
    sim.close()
    if not report.ok:
        print(f"\n{len(report.failures)} check(s) FAILED", file=sys.stderr)
        return 1
    print("\nall checks passed")
    return 0


def cmd_run(args) -> int:
    sinks = tuple(name.strip() for name in args.sink.split(",") if name.strip())
    sim = _build(args, reset=not args.keep, sinks=sinks or None)

    if not (args.days or args.hours or args.minutes or args.live or args.then_live):
        print(
            "nothing to do: pass --days/--hours/--minutes, or --live",
            file=sys.stderr,
        )
        return 2

    summary = sim.run(
        days=args.days,
        hours=args.hours,
        minutes=args.minutes,
        live=args.live,
        then_live=args.then_live,
        speed=args.speed,
        max_wall_seconds=args.max_wall_seconds,
    )
    print(summary.render())
    _print_status(sim.status(), heading="\nfinal state")
    if args.export:
        _export(sim, args.export, "both")
    sim.close()
    return 0


def cmd_inject(args) -> int:
    sim = _build(args, reset=True)
    sim.start()
    if args.warmup_hours > 0:
        sim.run(hours=args.warmup_hours)
    try:
        episode_id = sim.inject_failure(
            args.machine, args.failure, severity=args.severity
        )
    except (KeyError, ValueError) as exc:
        print(f"injection failed: {exc}", file=sys.stderr)
        sim.close()
        return 2

    print(f"injected {args.failure} on {args.machine} (episode {episode_id})")
    summary = sim.run(hours=args.warmup_hours + args.hours)
    print(summary.render())

    truth = next(
        (t for t in sim.ledger.all() if t.episode_id == episode_id), None
    )
    if truth is not None:
        print("\nground truth for the injected episode")
        for key in (
            "failure_mode",
            "root_cause",
            "onset_at",
            "scheduled_fault_at",
            "warned_at",
            "faulted_at",
            "averted_at",
            "outcome",
            "affected_batches",
        ):
            print(f"  {key:<20} {truth.as_row()[key]}")
        rca = [
            report
            for report in sim.rca.reports.values()
            if report.failure_id == truth.failure_id
        ]
        for report in rca:
            verdict = "CORRECT" if report.root_cause == truth.root_cause else "WRONG"
            print(
                f"\nRCA {report.rca_id}: {report.root_cause} "
                f"(confidence {report.confidence:.2f}) — {verdict}"
            )
            for item in report.evidence:
                print(f"    evidence: {item.render()}")
    if args.export:
        _export(sim, args.export, "both")
    sim.close()
    return 0


def cmd_scenario(args) -> int:
    config = load_config(args.config)
    available = {spec.id: spec for spec in config.scenarios.scenarios}
    if args.list:
        print("available scenarios")
        for spec in config.scenarios.scenarios:
            print(f"  {spec.id:<28} {spec.duration_hours:>6.0f}h  {spec.description}")
        return 0
    if args.name not in available:
        print(
            f"unknown scenario {args.name!r}; available: {sorted(available)}",
            file=sys.stderr,
        )
        return 2

    from pharma_sim.engine.scenario_engine import ScenarioEngine

    sim = _build(args, reset=True)
    engine = ScenarioEngine(sim)
    spec = available[args.name]
    print(f"scenario {spec.id}: {spec.description} ({spec.duration_hours:.0f}h)")
    summary = engine.run(spec)
    print(summary.render())
    _print_status(sim.status(), heading="\nfinal state")
    if args.export:
        _export(sim, args.export, "both")
    sim.close()
    return 0


def cmd_serve(args) -> int:
    try:
        from pharma_sim.api.main import AppSettings, run
    except ImportError:
        print(
            "the API needs FastAPI and uvicorn. Install them with:\n"
            '  uv pip install -e ".[api]"',
            file=sys.stderr,
        )
        return 2

    # Fail on a bad configuration before binding a port, rather than serving
    # numbers that came from an inconsistent factory.
    config = load_config(args.config)
    issues = lint_config(config)
    if issues:
        print(f"{len(issues)} configuration issue(s):", file=sys.stderr)
        for issue in issues:
            print(issue.render(), file=sys.stderr)
        return 1

    settings = AppSettings(
        config_dir=args.config,
        live=args.live,
        seed=args.seed,
        speed=args.speed,
        warmup_hours=args.warmup_hours,
        sinks=tuple(name.strip() for name in args.sink.split(",") if name.strip()),
    )
    mode = "live" if args.live else "historical"
    print(f"serving the {mode} dashboard on http://{args.host}:{args.port}")
    if args.live:
        print(
            f"  warming up {args.warmup_hours:g} simulated hours, then running at "
            f"{args.speed:g}x"
        )
    else:
        print("  reading the stored dataset; run a simulation first if it is empty")
    run(settings, host=args.host, port=args.port)
    return 0


def cmd_export(args) -> int:
    sim = _build(args, reset=False)
    _export(sim, args.output, args.format)
    sim.close()
    return 0


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _build(args, *, reset: bool, sinks: tuple[str, ...] | None = None) -> Simulator:
    return Simulator(
        args.config,
        seed=args.seed,
        sinks=sinks,
        reset_storage=reset,
        configure_logs=False,
        log_level=args.log_level,
    )


def _export(sim: Simulator, output: str, fmt: str) -> None:
    exporter = DatasetExporter(sim.storage, output_dir=output, fmt=fmt)
    result = exporter.export()
    print(f"\nexported {result.total_rows:,} rows to {output}")
    for name, count in result.files.items():
        print(f"  {name:<32} {count:>12,}")
    if result.skipped:
        print("  (empty, not written: " + ", ".join(sorted(result.skipped)) + ")")


def _print_status(status: dict, heading: str = "") -> None:
    if heading:
        print(heading)
    topology = status["topology"]
    print(
        f"  topology       {topology['units']} units, {topology['machines']} machines, "
        f"{topology['sensors']} sensors, {topology['employees']} employees "
        f"({topology['workers']} unit workers), {topology['shifts']} shifts, "
        f"{topology['states']} states, {topology['products']} products"
    )
    clock = status["clock"]
    print(
        f"  clock          {clock['now']}  {clock['state']}/{clock['mode']}  "
        f"{clock['simulated_hours']:.2f} simulated hours"
    )
    print(f"  machine states {status['machines_by_state']}")
    print(
        f"  telemetry      {status['telemetry']['readings']:,} readings, "
        f"{status['telemetry']['dropouts']:,} dropouts, "
        f"{status['telemetry']['alarms']:,} alarms"
    )
    batches = status["batches"]
    print(
        f"  batches        {batches['completed']} completed "
        f"({batches['released']} released, {batches['rejected']} rejected, "
        f"{batches['quarantined']} quarantined), {batches['active']} active"
    )
    print(
        f"  quality        {batches['qc_tests']:,} QC tests, "
        f"{batches['qc_failures']:,} failures"
    )
    reliability = status["reliability"]
    print(
        f"  reliability    {reliability['episodes_started']} degradation episodes, "
        f"{reliability['faults']} faults, {reliability['averted']} averted, "
        f"{reliability['maintenance_actions']} maintenance actions "
        f"({reliability['pm_deferred']} PM deferred)"
    )
    qm = status["quality_management"]
    print(
        f"  quality mgmt   {qm['deviations']} deviations, {qm['rca_reports']} RCA, "
        f"{qm['capas']} CAPA ({qm['capas_closed']} closed)"
    )
    print(f"  events         {status['events']:,}   labels {status['labels']:,}")
    sinks = status.get("sinks") or []
    for stats in sinks:
        print(
            f"  sink {stats['sink']:<10} sent={stats['sent']:,} dropped={stats['dropped']:,} "
            f"errors={stats['errors']:,} connected={stats['connected']}"
        )


_COMMANDS = {
    "validate": cmd_validate,
    "schema": cmd_schema,
    "init": cmd_init,
    "status": cmd_status,
    "verify-integrity": cmd_verify_integrity,
    "run": cmd_run,
    "inject-failure": cmd_inject,
    "scenario": cmd_scenario,
    "export": cmd_export,
    "serve": cmd_serve,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(level=args.log_level, json_format=args.json_logs)
    try:
        return _COMMANDS[args.command](args)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
