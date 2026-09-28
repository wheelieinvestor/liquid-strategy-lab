"""Beginner commands around the existing causal simulation engine."""

import argparse
import json
import sys
import time
import webbrowser
from dataclasses import asdict, replace
from importlib.resources import files
from pathlib import Path

from pydantic import ValidationError

from liquid_autonomous_trader.backtesting.events import digest
from liquid_autonomous_trader.backtesting.historical import HistoryAssumptions, run_btc
from liquid_autonomous_trader.backtesting.ledger import serial
from liquid_autonomous_trader.backtesting.report import render as detailed_report
from liquid_strategy_lab import __version__
from liquid_strategy_lab.datasets import MINUTE, load_csv
from liquid_strategy_lab.portable import dependency_hash, rss_bytes, source_hash
from liquid_strategy_lab.report import render
from liquid_strategy_lab.settings import (
    POLICY_NAMES,
    BtcSettings,
    PortfolioSettings,
    SandboxSettings,
)


def bundled(name):
    return Path(str(files("liquid_strategy_lab").joinpath("data", name)))


def write_json(path, value):
    path.write_text(json.dumps(serial(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def new_output(path):
    path = Path(path).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    if any(path.iterdir()):
        raise ValueError("output_folder_not_empty_choose_a_new_folder_to_preserve_results")
    return path


def run_settings(settings, config_directory, output):
    if len(settings.policies) != len(set(settings.policies)):
        raise ValueError("choose_each_policy_only_once")
    if settings.dataset == "bundled:btc-demo":
        if settings.data_kind != "synthetic":
            raise ValueError("bundled_demo_must_be_labeled_synthetic")
        path = bundled("btc-demo.csv")
        description = "Authored synthetic BTC candles; not historical prices."
    else:
        path = (config_directory / settings.dataset).resolve()
        description = settings.data_description
    dataset = load_csv(path, kind=settings.data_kind, description=description)
    start = dataset.rows[0].open_us + 1440 * MINUTE
    end = dataset.rows[-1].open_us + MINUTE
    output = new_output(output)
    results = {}
    began = time.monotonic()

    def guard():
        if time.monotonic() - began > 600 or rss_bytes() > 2 * 1024**3:
            raise ValueError("local_run_budget_exceeded_10_minutes_or_2_GiB")

    for name in settings.policies:
        assumptions = HistoryAssumptions(
            initial_cash=str(settings.initial_cash),
            fee_per_side=str(settings.fee_per_side),
            spread_bps=str(settings.spread_bps),
            extra_slippage_bps=str(settings.slippage_bps),
            policy=POLICY_NAMES[name],
        )
        result = run_btc(
            path.parent,
            start,
            end,
            assumptions,
            minute_rows=iter(dataset.rows),
            funding={},
            dataset=dataset.description,
            resource_guard=guard,
        )
        result["title"] = settings.title + " — " + name
        result["core_hash"] = digest({k: v for k, v in result.items() if k != "core_hash"})
        results[name] = result
    finish(
        settings.title,
        settings.model_dump(mode="json"),
        dataset.description,
        results,
        output,
        began,
    )
    return output


def finish(title, settings, dataset, results, output, began):
    for name, result in results.items():
        write_json(output / (name + ".json"), result)
        detailed_report(result, output / (name + ".html"))
    values = render(title, results, output / "report.html", settings=settings, dataset=dataset)
    write_json(
        output / "comparison.json",
        {
            "schema": "liquid-strategy-lab-comparison-v1",
            "version": __version__,
            "settings": settings,
            "dataset": dataset,
            "metrics": values,
            "source_sha256": source_hash(),
            "dependencies_sha256": dependency_hash(),
            "run_hashes": {
                name: r.get("core_hash", r.get("core_sha256")) for name, r in results.items()
            },
            "wall_seconds": round(time.monotonic() - began, 3),
            "observed_rss_bytes": rss_bytes(),
            "strategy_improvement": "not_established",
        },
    )


def sandbox(settings, config_directory, output):
    from liquid_strategy_lab.sandbox import run, strategy_factory
    from liquid_strategy_lab.synthetic import SCENARIOS, description, generate

    output = new_output(output)
    began = time.monotonic()
    factory, strategy_hash = strategy_factory(settings, config_directory)
    results = {}
    for name in SCENARIOS:
        rows = generate(name, settings.seed, settings.bars)
        results[name] = run(rows, description(name, settings.seed, rows), settings, factory())
        results[name]["strategy_sha256"] = strategy_hash
        results[name]["core_hash"] = digest(
            {k: v for k, v in results[name].items() if k != "core_hash"}
        )
    dataset = {
        "kind": "synthetic",
        "funding": "excluded",
        "costs": "excluded",
        "description": "Five fictional markets. Fees, spread, slippage and funding excluded.",
        "sha256": digest({name: r["dataset"]["sha256"] for name, r in results.items()}),
    }
    finish(settings.title, settings.model_dump(mode="json"), dataset, results, output, began)
    return output


def portfolio(settings, output):
    from liquid_autonomous_trader.backtesting.fixtures import adapters, portfolio_events
    from liquid_autonomous_trader.backtesting.portfolio import PortfolioEngine

    output = new_output(output)
    began = time.monotonic()
    sources = adapters()
    sim = sources.account.sim
    sources.close()
    # Construct a fresh ledger instead of editing a ledger after transitions.
    from liquid_autonomous_trader.backtesting.execution import SimulatedExecution
    from liquid_autonomous_trader.backtesting.ledger import Ledger

    instruments = dict(sim.ledger.instruments)
    instruments["ETH"] = replace(instruments["BTC"], symbol="ETH")
    sim = SimulatedExecution(Ledger(settings.initial_cash, instruments), sim.fees)
    engine = PortfolioEngine(
        sim, policy=settings.policy, enabled=settings.enabled, mode="synthetic_stress"
    )
    try:
        before, after = portfolio_events()
        result = engine.run([*before, *after])
    finally:
        engine.close()
    result["title"] = settings.title
    dataset = {
        "kind": "synthetic",
        "description": (
            "Authored market, Flow, GEX and Cramer events; not historical alerts or news."
        ),
        "sha256": digest([asdict(e) for e in [*before, *after]]),
    }
    finish(
        settings.title,
        settings.model_dump(mode="json"),
        dataset,
        {"portfolio": result},
        output,
        began,
    )
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Liquid strategy tests on your computer. No account required."
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    simple = commands.add_parser(
        "sandbox", help="Test strategy rules on five synthetic scenarios, without costs"
    )
    simple.add_argument("--config", type=Path)
    simple.add_argument("--output", type=Path, default=Path("outputs/sandbox"))
    simple.add_argument("--open", action="store_true")
    demo = commands.add_parser("demo", help="Run the included BTC comparison")
    demo.add_argument("--output", type=Path, default=Path("outputs/demo"))
    demo.add_argument(
        "--open", action="store_true", help="Open the completed report in your browser"
    )
    run = commands.add_parser("run", help="Run a BTC settings file")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--open", action="store_true")
    shared = commands.add_parser("portfolio", help="Run the four-agent synthetic account example")
    shared.add_argument("--config", type=Path, default=None)
    shared.add_argument("--output", type=Path, default=Path("outputs/portfolio"))
    shared.add_argument("--open", action="store_true")
    stress = commands.add_parser("stress", help="Run credential-free behavior checks")
    stress.add_argument(
        "--family", choices=["execution", "source", "portfolio", "jev"], default="execution"
    )
    stress.add_argument("--output", type=Path, default=Path("outputs/stress"))
    commands.add_parser("templates", help="Describe the included Liquid strategy templates")
    args = parser.parse_args(argv)
    try:
        if args.command == "templates":
            print(
                "Synthetic sandbox: SMA or custom rules in five fictional markets, zero costs.\n"
                "BTC Momentum: candle simulation and stop comparisons (synthetic or proxy CSV).\n"
                "Flow Show Mirror, XYZ100 GEX, Inverse Cramer: captured-event replay "
                "and synthetic portfolio examples.\n"
                "Exact historical AI management requires original timestamped model records."
            )
            return 0
        if args.command == "stress":
            from liquid_autonomous_trader.backtesting.campaign import run as campaign

            result = campaign(args.output.expanduser().resolve(), family=args.family)
            print(
                f"Scenarios: {result['counts']}\n"
                f"Report: {args.output / (args.family + '-summary.html')}"
            )
            return 0 if result["selected_cases_passed"] else 1
        config = args.config if args.command != "demo" else None
        if args.command == "sandbox":
            settings = (
                SandboxSettings.model_validate_json(config.read_text(encoding="utf-8"))
                if config
                else SandboxSettings()
            )
            output = sandbox(
                settings, config.resolve().parent if config else Path.cwd(), args.output
            )
        elif args.command == "portfolio":
            config = config or bundled("portfolio.json")
            settings = PortfolioSettings.model_validate_json(config.read_text(encoding="utf-8"))
            output = portfolio(settings, args.output)
        else:
            config = config or bundled("default.json")
            settings = BtcSettings.model_validate_json(config.read_text(encoding="utf-8"))
            output = run_settings(settings, config.resolve().parent, args.output)
        print(
            f"Complete. Open {output / 'report.html'}\n"
            "Simulated results only. Full run data is in the same folder."
        )
        if args.open:
            webbrowser.open((output / "report.html").as_uri())
        return 0
    except (ValueError, ValidationError, OSError) as error:
        parser.exit(
            2,
            f"Could not run: {error}\nSee docs/TROUBLESHOOTING.md or choose a new output folder.\n",
        )


if __name__ == "__main__":
    sys.exit(main())
