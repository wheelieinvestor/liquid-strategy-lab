"""Local research commands, inert unless explicitly invoked."""

from __future__ import annotations

import argparse
import json
import platform
import time
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from liquid_autonomous_trader.backtesting.btc import BtcAdapter
from liquid_autonomous_trader.backtesting.events import Catalog, Event, digest
from liquid_autonomous_trader.backtesting.historical import HistoryAssumptions, run_btc
from liquid_autonomous_trader.backtesting.history import audit_archives, sha256
from liquid_autonomous_trader.backtesting.recorder import capture
from liquid_autonomous_trader.backtesting.report import render
from liquid_strategy_lab.portable import dependency_hash, rss_bytes, source_hash


def output_path(value):
    path = Path(value).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("UTC_or_explicit_timezone_required")
    return int(result.timestamp() * 1_000_000)


def write(path, value):
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    history = commands.add_parser(
        "historical", help="BTC proxy historical run; explicit assumptions"
    )
    history.add_argument("--cache", type=Path, required=True)
    history.add_argument("--start", required=True)
    history.add_argument("--end", required=True)
    history.add_argument("--output", required=True)
    history.add_argument("--assumptions", type=Path)
    report = commands.add_parser("report")
    report.add_argument("run", type=Path)
    report.add_argument("--output", required=True)
    audit = commands.add_parser("audit-archives")
    audit.add_argument("cache", type=Path)
    audit.add_argument("--output", required=True)
    record = commands.add_parser("record", help="bounded foreground capture; Ctrl-C stops")
    record.add_argument("--catalog", required=True)
    record.add_argument("--cycles", type=int, default=1)
    record.add_argument("--interval", type=float, default=1)
    record.add_argument("--source-events", type=Path, action="append", default=[])
    stream = commands.add_parser(
        "record-stream", help="bounded public trades and books; Ctrl-C stops"
    )
    stream.add_argument("--catalog", required=True)
    stream.add_argument("--coin", action="append", dest="coins")
    stream.add_argument("--seconds", type=int, default=10)
    stream.add_argument("--max-messages", type=int, default=1000)
    health = commands.add_parser("health")
    health.add_argument("catalog", type=Path)
    replay = commands.add_parser("replay")
    replay.add_argument("catalog", type=Path)
    replay.add_argument("--at", required=True, help="availability clock in UTC")
    replay.add_argument("--output", required=True)
    export = commands.add_parser("export")
    export.add_argument("catalog", type=Path)
    export.add_argument("--output", required=True)
    ingest = commands.add_parser("import")
    ingest.add_argument("events", type=Path)
    ingest.add_argument("--catalog", required=True)
    portfolio = commands.add_parser("portfolio", help="shared four-agent causal event replay")
    portfolio.add_argument("catalog", type=Path)
    portfolio.add_argument("--manifest", type=Path, required=True)
    portfolio.add_argument("--output", required=True)
    portfolio.add_argument(
        "--until", help="stop at this availability time; save resumable checkpoint"
    )
    portfolio.add_argument("--resume", type=Path)
    batch = commands.add_parser("batch", help="bounded resumable declared scenario campaign")
    batch.add_argument("--directory", required=True)
    batch.add_argument("--family", choices=("market", "execution", "source", "portfolio", "jev"))
    batch.add_argument("--limit", type=int)
    batch.add_argument("--case", action="append", dest="case_ids")
    batch.add_argument("--market-cache", type=Path)
    batch.add_argument("--retry-failed", action="store_true")
    calibration = commands.add_parser("calibrate", help="offline native fee residuals")
    calibration.add_argument("evidence", type=Path)
    calibration.add_argument("--split", required=True)
    calibration.add_argument("--output", required=True)
    normalize = commands.add_parser(
        "normalize-sources", help="existing sanitized source exports to event spool"
    )
    normalize.add_argument("records", type=Path)
    normalize.add_argument("archives", type=Path)
    normalize.add_argument("--output", required=True)
    models = commands.add_parser(
        "cache-models", help="audit and retain original recorded model states"
    )
    models.add_argument("evidence", type=Path)
    models.add_argument("--cache", required=True)
    models.add_argument("--output", required=True)
    comparison = commands.add_parser("compare", help="frozen resumable five-policy study")
    comparison.add_argument("--cache", type=Path, required=True)
    comparison.add_argument("--directory", required=True)
    comparison.add_argument("--limit", type=int)
    comparison.add_argument("--retry-failed", action="store_true")
    return root


def main():
    args = parser().parse_args()
    if args.command == "compare":
        from liquid_autonomous_trader.backtesting.comparison import run

        directory = output_path(str(Path(args.directory) / "destination-check")).parent
        result = run(directory, args.cache, limit=args.limit, retry_failed=args.retry_failed)
        print(
            json.dumps(
                {
                    "core_hash": result["core_hash"],
                    "counts": result["counts"],
                    "stopped": result["stopped"],
                    "invocation": result["invocation"],
                }
            )
        )
    elif args.command == "cache-models":
        from liquid_autonomous_trader.backtesting.jev import ModelCache
        from liquid_autonomous_trader.backtesting.recorded_models import import_recorded_models

        if args.evidence.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("model_export_size_budget")
        cache = ModelCache(output_path(args.cache))
        try:
            result = import_recorded_models(json.loads(args.evidence.read_text()), cache)
            write(output_path(args.output), result)
            print(
                json.dumps(
                    {
                        "counts": result["counts"],
                        "core_hash": result["core_sha256"],
                        "verdict": result["verdict"],
                    }
                )
            )
        finally:
            cache.close()
    elif args.command == "normalize-sources":
        from liquid_autonomous_trader.backtesting.source_import import normalize_sources

        if max(args.records.stat().st_size, args.archives.stat().st_size) > 32 * 1024 * 1024:
            raise ValueError("source_export_size_budget")
        events, receipt = normalize_sources(
            json.loads(args.records.read_text()), json.loads(args.archives.read_text())
        )
        path = output_path(args.output)
        with path.open("x") as stream:
            for event in events:
                stream.write(json.dumps(asdict(event), sort_keys=True) + "\n")
        write(path.with_suffix(".receipt.json"), receipt)
        print(json.dumps(receipt))
    elif args.command == "calibrate":
        from liquid_autonomous_trader.backtesting.calibration import calibrate, render_calibration

        if args.evidence.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("evidence_size_budget")
        result = calibrate(
            json.loads(args.evidence.read_text()), split_ms=timestamp(args.split) // 1000
        )
        path = output_path(args.output)
        write(path, result)
        render_calibration(result, path.with_suffix(".html"))
        print(
            json.dumps(
                {
                    "core_hash": result["core_sha256"],
                    "verdict": result["verdict"],
                    "groups": len(result["groups"]),
                }
            )
        )
    elif args.command == "batch":
        from liquid_autonomous_trader.backtesting.campaign import run

        directory = output_path(str(Path(args.directory) / "destination-check")).parent
        result = run(
            directory,
            family=args.family,
            case_ids=args.case_ids,
            limit=args.limit,
            retry_failed=args.retry_failed,
            market_cache=args.market_cache,
        )
        print(
            json.dumps(
                {
                    "counts": result["counts"],
                    "core_hash": result["core_hash"],
                    "acceptance_campaign_complete": result["acceptance_campaign_complete"],
                    "invocation": result["invocation"],
                }
            )
        )
    elif args.command == "portfolio":
        from liquid_autonomous_trader.backtesting.execution import SimulatedExecution
        from liquid_autonomous_trader.backtesting.ledger import (
            FeeSchedule,
            Instrument,
            Ledger,
            Tier,
        )
        from liquid_autonomous_trader.backtesting.portfolio import PortfolioEngine

        manifest = json.loads(args.manifest.read_text())
        output = output_path(args.output)
        until = timestamp(args.until) if args.until else 2**62
        begin, cpu = time.monotonic(), time.process_time()
        if args.resume:
            saved = json.loads(args.resume.read_text())
            if saved["manifest_sha256"] != digest(manifest):
                raise ValueError("resume_manifest_mismatch")
            engine = PortfolioEngine.restore(saved["checkpoint"])
        else:
            specs = {}
            for row in manifest["instruments"]:
                row = dict(row)
                row["tiers"] = tuple(
                    Tier(Decimal(t["lower"]), Decimal(t["maximum_leverage"])) for t in row["tiers"]
                )
                for key in ("quantity_step", "price_step", "minimum_notional"):
                    row[key] = Decimal(row[key])
                specs[row["symbol"]] = Instrument(**row)
            fees = dict(manifest["fees"])
            for key in ("maker", "taker"):
                fees[key] = Decimal(fees[key])
            sim = SimulatedExecution(
                Ledger(Decimal(manifest["initial_cash"]), specs), FeeSchedule(**fees)
            )
            engine = PortfolioEngine(sim, **manifest["engine"])
        catalog = Catalog(args.catalog, read_only=True)
        try:
            events = [
                e for e in catalog.replay(until_us=until) if e.identity not in engine.applied_events
            ]
            result = engine.run(events)
            result["run_manifest"] = manifest
            result["run_manifest_sha256"] = digest(manifest)
            write(output, result)
            render(result, output.with_suffix(".html"))
            write(
                output.with_suffix(".checkpoint.json"),
                {"manifest_sha256": digest(manifest), "checkpoint": engine.checkpoint()},
            )
            write(
                output.with_suffix(".receipt.json"),
                {
                    "wall_seconds": time.monotonic() - begin,
                    "cpu_seconds": time.process_time() - cpu,
                    "observed_rss_bytes": rss_bytes(),
                    "machine": platform.platform(),
                    "python": platform.python_version(),
                    "source_sha": source_hash(),
                    "source_state": "working_tree",
                    "dependency_lock_sha256": dependency_hash(),
                    "research_package_sha256": digest(
                        {p.name: sha256(p) for p in sorted(Path(__file__).parent.glob("*.py"))}
                    ),
                    "core_hash": result["core_sha256"],
                    "manifest_sha256": digest(manifest),
                    "catalog": catalog.manifest(),
                    "result_bytes": output.stat().st_size,
                    "recorded_at": datetime.now(UTC).isoformat(),
                },
            )
            print(
                json.dumps(
                    {
                        "core_hash": result["core_sha256"],
                        "report": str(output.with_suffix(".html")),
                        "verdict": "unsupported_for_strategy_improvement",
                    }
                )
            )
        finally:
            engine.close()
            catalog.close()
    elif args.command == "historical":
        path = output_path(args.output)
        settings = (
            HistoryAssumptions(**json.loads(args.assumptions.read_text()))
            if args.assumptions
            else HistoryAssumptions()
        )
        begin = time.monotonic()
        cpu = time.process_time()
        start, end = timestamp(args.start), timestamp(args.end)
        catalog = Catalog(path.parent / "exposures.sqlite")
        catalog.expose(
            digest(asdict(settings)), "historical_cli_parameter_and_date_exposure", start, end
        )
        catalog.close()
        result = run_btc(args.cache, start, end, settings)
        write(path, result)
        package = Path(__file__).parent
        code_hash = digest({p.name: sha256(p) for p in sorted(package.glob("*.py"))})
        receipt = {
            "research_package_sha256": code_hash,
            "dependency_lock_sha256": dependency_hash(),
            "source_state": "working_tree; baseline_git_sha_is_not_a_research_release",
            "source_sha": source_hash(),
            "core_hash": result["core_hash"],
            "wall_seconds": time.monotonic() - begin,
            "cpu_seconds": time.process_time() - cpu,
            "observed_rss_bytes": rss_bytes(),
            "machine": platform.platform(),
            "python": platform.python_version(),
            "result_bytes": path.stat().st_size,
            "recorded_at": datetime.now(UTC).isoformat(),
            "command": vars(args)
            | {
                "cache": str(args.cache),
                "assumptions": str(args.assumptions) if args.assumptions else None,
            },
        }
        write(path.with_suffix(".receipt.json"), receipt)
        render(result, path.with_suffix(".html"))
        print(
            json.dumps(
                {
                    "core_hash": result["core_hash"],
                    "report": str(path.with_suffix(".html")),
                    "verdict": result["verdict"],
                }
            )
        )
    elif args.command == "report":
        result = json.loads(args.run.read_text())
        summary = render(result, output_path(args.output))
        print(
            json.dumps({"net_pnl": summary["net_pnl"], "closed_trades": summary["closed_trades"]})
        )
    elif args.command == "audit-archives":
        write(output_path(args.output), audit_archives(args.cache))
    elif args.command == "record-stream":
        from liquid_autonomous_trader.backtesting.stream import capture_stream

        print(
            json.dumps(
                capture_stream(
                    output_path(args.catalog),
                    coins=tuple(args.coins or ["BTC"]),
                    seconds=args.seconds,
                    max_messages=args.max_messages,
                )
            )
        )
    elif args.command == "record":
        print(
            json.dumps(
                capture(
                    output_path(args.catalog),
                    cycles=args.cycles,
                    interval=args.interval,
                    source_spools=args.source_events,
                )
            )
        )
    elif args.command in {"health", "replay", "export"}:
        catalog = Catalog(args.catalog, read_only=True)
        try:
            if args.command == "health":
                from liquid_autonomous_trader.backtesting.catalog_io import health

                print(json.dumps(health(catalog, now_us=time.time_ns() // 1000)))
            elif args.command == "replay":
                at = timestamp(args.at)
                events = list(catalog.replay(until_us=at))
                write(output_path(args.output), asdict(BtcAdapter().decide(events, at)))
            else:
                from liquid_autonomous_trader.backtesting.catalog_io import export_catalog

                print(json.dumps(export_catalog(catalog, output_path(args.output))))
        finally:
            catalog.close()
    elif args.command == "import":
        from liquid_autonomous_trader.backtesting.catalog_io import verify_export

        if args.events.with_suffix(args.events.suffix + ".manifest.json").exists():
            verify_export(args.events)
        catalog = Catalog(output_path(args.catalog))
        inserted = 0
        try:
            with args.events.open() as stream:
                while line := stream.readline(4 * 1024 * 1024 + 1):
                    if len(line) > 4 * 1024 * 1024:
                        raise ValueError("import_event_size_budget")
                    value = json.loads(line)
                    value["lineage"] = tuple(value.get("lineage", []))
                    inserted += catalog.append([Event(**value)])
            print(json.dumps({"inserted": inserted, **catalog.manifest()}))
        finally:
            catalog.close()


if __name__ == "__main__":
    main()
