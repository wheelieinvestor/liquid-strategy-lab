"""Verify the actual beginner workflows, data failures, and offline boundary."""

import json
import socket
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from liquid_autonomous_trader.backtesting.portfolio import PortfolioEngine
from liquid_strategy_lab.cli import bundled, main, run_settings
from liquid_strategy_lab.datasets import load_csv
from liquid_strategy_lab.settings import BtcSettings


def forbid_network(*args, **kwargs):
    raise AssertionError("simulation_attempted_network")


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    monkeypatch.setattr(socket, "create_connection", forbid_network)
    for name in ("LIQUID_LIVE_TRADING", "LIQUID_BROKER_WRITES"):
        monkeypatch.setenv(name, "true")
    for name in ("TYPESAFE_API_KEY", "DISCORD_BOT_TOKEN", "DATABASE_URL"):
        monkeypatch.setenv(name, "inert-test-sentinel")
    real_connect = sqlite3.connect

    def memory_only(database, *args, **kwargs):
        assert database == ":memory:", "beginner_simulation_opened_external_database"
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", memory_only)


def test_demo_offline_accounting_reproducibility_and_report(tmp_path, offline):
    a, b = tmp_path / "first", tmp_path / "second"
    assert main(["demo", "--output", str(a)]) == 0
    assert main(["demo", "--output", str(b)]) == 0
    first, second = [json.loads((p / "comparison.json").read_text()) for p in (a, b)]
    assert first["run_hashes"] == second["run_hashes"]
    assert first["dataset"]["kind"] == "synthetic"
    assert first["dataset"]["funding"] == "zero_assumed_not_observed"
    assert first["strategy_improvement"] == "not_established"
    for name, metric in first["metrics"].items():
        assert metric["closed_trades"] > 0
        assert Decimal(metric["fees"]) > 0
        assert Decimal(metric["net_pnl"]) == (
            Decimal(metric["price_pnl_at_execution_prices"])
            - Decimal(metric["fees"])
            + Decimal(metric["funding"])
        )
        result = json.loads((a / (name + ".json")).read_text())
        assert result["mode"] == "synthetic_stress"
    assert first["run_hashes"]["preserve-stop"] != first["run_hashes"]["breakeven-1R"]
    report = (a / "report.html").read_text()
    assert "SYNTHETIC DATA" in report
    assert "Largest account decline" in report
    assert "https://" not in report and "<script" not in report
    assert 'href="comparison.json"' in report
    with pytest.raises(SystemExit) as exc:
        main(["demo", "--output", str(a)])
    assert exc.value.code == 2
    assert json.loads((a / "comparison.json").read_text())["run_hashes"] == first["run_hashes"]


def test_one_setting_change_and_escaped_title(tmp_path, offline):
    settings = BtcSettings(title='<script>alert("x")</script>', policies=["breakeven-1.5R"])
    run_settings(settings, tmp_path, tmp_path / "changed")
    report = (tmp_path / "changed/report.html").read_text()
    assert "<script>alert" not in report
    assert "&lt;script&gt;" in report
    result = json.loads((tmp_path / "changed/comparison.json").read_text())
    assert list(result["metrics"]) == ["breakeven-1.5R"]


def test_portfolio_uses_all_four_real_strategy_adapters(tmp_path, offline):
    assert main(["portfolio", "--output", str(tmp_path / "portfolio")]) == 0
    result = json.loads((tmp_path / "portfolio/portfolio.json").read_text())
    positions = result["simulation"]["ledger"]["positions"]
    assert {p["owner"] for p in positions.values()} == {
        "btc_momentum",
        "flow_show_mirror",
        "xyz100_gex",
        "inverse_cramer",
    }
    assert "SYNTHETIC DATA" in (tmp_path / "portfolio/report.html").read_text()


@pytest.mark.parametrize(
    "policy",
    ["current-v5-exact", "legacy-production", "breakeven-1R", "breakeven-1.25R", "breakeven-1.5R"],
)
def test_all_management_variants_are_offline(policy, offline):
    from dataclasses import replace

    from liquid_autonomous_trader.backtesting.fixtures import adapters, portfolio_events

    sources = adapters()
    sim = sources.account.sim
    sources.close()
    sim.ledger.instruments["ETH"] = replace(sim.ledger.instruments["BTC"], symbol="ETH")
    engine = PortfolioEngine(sim, policy=policy, mode="synthetic_stress")
    try:
        before, after = portfolio_events()
        engine.run([*before, *after])
        sim.ledger.reconcile()
    finally:
        engine.close()


@pytest.mark.parametrize(
    "updates",
    [
        {"fee_per_side": "NaN"},
        {"initial_cash": "-1"},
        {"spread_bps": -1},
        {"policies": ["unknown"]},
        {"api_key": "unexpected"},
        {"data_kind": "native"},
    ],
)
def test_invalid_settings_fail(updates):
    with pytest.raises(ValidationError):
        BtcSettings(**updates)


def test_bundled_data_cannot_be_mislabeled_or_overwritten(tmp_path):
    with pytest.raises(ValueError, match="labeled_synthetic"):
        run_settings(BtcSettings(data_kind="proxy"), tmp_path, tmp_path / "out")
    with pytest.raises(ValueError, match="only_once"):
        run_settings(
            BtcSettings(policies=["preserve-stop", "preserve-stop"]), tmp_path, tmp_path / "out"
        )


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("gap", "contiguous"),
        ("timezone", "UTC"),
        ("ohlc", "OHLC"),
        ("nan", "invalid_decimal"),
    ],
)
def test_corrupt_candles_fail_before_simulation(tmp_path, mutation, expected):
    lines = bundled("btc-demo.csv").read_text().splitlines()
    if mutation == "gap":
        lines.pop(20)
    elif mutation == "timezone":
        lines[1] = lines[1].replace("+00:00", "")
    elif mutation == "ohlc":
        fields = lines[1].split(",")
        fields[2] = "1"
        lines[1] = ",".join(fields)
    else:
        fields = lines[1].split(",")
        fields[1] = "NaN"
        lines[1] = ",".join(fields)
    p = tmp_path / "bad.csv"
    p.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match=expected):
        load_csv(p, kind="proxy", description="Invalid test data")


def test_no_account_transport_is_distributed():
    from liquid_autonomous_trader import computer_mcp, controller, jev_exit_manager

    assert not hasattr(computer_mcp, "ComputerPostOnce")
    assert not hasattr(computer_mcp, "ReadOnlyComputerClient")
    assert not hasattr(controller, "TradingController")
    with pytest.raises(RuntimeError, match="live_inference_not_available"):
        jev_exit_manager.ask_jev({}, "inert-sentinel")
    assert not Path(computer_mcp.__file__).with_name("computer_oauth.py").exists()
