"""Behavioral checks for synthetic strategy evaluation and its time boundary."""

import json
import socket
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from liquid_strategy_lab.cli import main
from liquid_strategy_lab.sandbox import PositionView, run, strategy_factory
from liquid_strategy_lab.settings import SandboxSettings
from liquid_strategy_lab.synthetic import BAR_US, SCENARIOS, Bar, description, generate
from scripts.verify_calculations import verify


def bars():
    return tuple(
        Bar(i, D(o), D(max(o, c)), D(min(o, c)), D(c))
        for i, (o, c) in enumerate([(100, 100), (110, 115), (120, 118), (100, 101)])
    )


def execute(rows, strategy, settings=None):
    return run(
        rows, {"kind": "synthetic", "description": "test"}, settings or SandboxSettings(), strategy
    )


def test_next_open_gap_reversal_flat_and_no_terminal_fill():
    targets = ("long", "short", "flat", "long")
    result = execute(bars(), lambda history, position: targets[len(history) - 1])
    fills = [e for e in result["ledger_journal"] if e["kind"] == "fill"]
    assert [D(f["data"]["price"]) for f in fills] == [110, 120, 120, 100]
    assert [f["data"]["quantity"].startswith("-") for f in fills] == [False, True, True, False]
    assert fills[0]["at_us"] == bars()[1].open_us > result["decisions"][0]["at_us"]
    assert result["decisions"][-1]["execution"] == "no_next_bar"
    assert result["ledger"]["positions"] == {}
    expected = D(1000) + D("1.81818181") * 10 + D("1.66666666") * 20
    assert D(result["ledger"]["equity"]) == expected
    assert all(D(result["ledger"][k]) == 0 for k in ("fees", "funding", "slippage"))
    verify(result)


def test_position_snapshot_and_past_bars_are_immutable_and_endpoint_is_marked():
    seen = []

    def strategy(history, position):
        seen.append((len(history), position))
        assert isinstance(history, tuple)
        assert all(b.index < len(history) for b in history)
        with pytest.raises(FrozenInstanceError):
            history[-1].close = D(999)
        with pytest.raises(FrozenInstanceError):
            position.quantity = D(999)
        return "long"

    result = execute(bars()[:2], strategy)
    assert seen[0][1] == PositionView("flat", D(0), None)
    assert seen[1][1].side == "long"
    assert D(result["ledger"]["equity"]) == 1000 + D("1.81818181") * 5
    assert len(result["ledger"]["positions"]) == 1
    verify(result)


def test_future_mutation_and_truncated_run_cannot_change_earlier_decisions_or_fills():
    rows = generate("choppy", 7, 300)
    settings = SandboxSettings()
    factory, _ = strategy_factory(settings, None)
    first = execute(rows, factory())
    changed = tuple(
        replace(b, open=b.open * 2, high=b.high * 2, low=b.low * 2, close=b.close * 2)
        if b.index >= 150
        else b
        for b in rows
    )
    second = execute(changed, factory())
    truncated = execute(rows[:150], factory())
    cutoff = rows[150].open_us
    for result in (second, truncated):
        assert [e for e in result["ledger_journal"] if e["at_us"] < cutoff] == [
            e for e in first["ledger_journal"] if e["at_us"] < cutoff
        ]
        assert [d["target"] for d in result["decisions"][:150]] == [
            d["target"] for d in first["decisions"][:150]
        ]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_generator_reproducibility_valid_prices_and_changed_seed(scenario):
    rows = generate(scenario, 7, 512)
    assert rows == generate(scenario, 7, 512)
    assert rows != generate(scenario, 8, 512)
    assert rows[:300] == generate(scenario, 7, 300)
    assert all(0 < b.low <= min(b.open, b.close) <= max(b.open, b.close) <= b.high for b in rows)
    assert len({b.open_us for b in rows}) == len(rows)
    if scenario == "sudden-drop":
        assert abs(rows[256].open / rows[255].close - D(".75")) < D(".00000001")
    assert description(scenario, 7, rows)["kind"] == "synthetic"


def test_custom_strategy_loading_and_fresh_state(tmp_path):
    path = tmp_path / "rules.py"
    path.write_text(
        "from __future__ import annotations\nfrom dataclasses import dataclass\n"
        "@dataclass\nclass State:\n    calls: int = 0\n"
        "state = State()\ndef decide(bars, position):\n"
        '    state.calls += 1\n    return "long" if state.calls == 1 else "hold"\n'
    )
    settings = SandboxSettings(strategy="custom", strategy_file="rules.py")
    factory, checksum = strategy_factory(settings, tmp_path)
    assert len(checksum) == 64
    assert execute(bars(), factory(), settings) == execute(bars(), factory(), settings)


@pytest.mark.parametrize("target", ["invalid", "short"])
def test_invalid_or_unsupported_strategy_signal_is_not_silently_substituted(target):
    with pytest.raises(ValueError):
        execute(bars(), lambda history, position: target, SandboxSettings(direction="long_only"))


def test_no_leverage_overspend_and_explicit_insolvency():
    result = execute(
        bars(),
        lambda history, position: "long",
        SandboxSettings(initial_cash="100", position_notional="100"),
    )
    assert abs(D(result["ledger"]["positions"]["SYNTH"]["quantity"])) * 110 <= 100
    failing = (*bars()[:2], Bar(2, D(10000), D(10000), D(10000), D(10000)))
    with pytest.raises(ValueError, match="equity_exhausted"):
        execute(failing, lambda history, position: "short")


@pytest.mark.parametrize(
    "changes",
    [
        {"initial_cash": "NaN"},
        {"fast": 50},
        {"bars": 10},
        {"position_notional": "1001"},
        {"fee_per_side": ".001"},
        {"strategy": "custom"},
        {"strategy_file": "rules.py"},
    ],
)
def test_invalid_settings_and_cost_parameters_rejected(changes):
    with pytest.raises(ValidationError):
        SandboxSettings(**changes)


def test_public_command_offline_zero_costs_report_and_independent_math(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("sandbox_tried_network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    output = tmp_path / "run"
    assert main(["sandbox", "--output", str(output)]) == 0
    comparison = json.loads((output / "comparison.json").read_text(encoding="utf-8"))
    assert set(comparison["metrics"]) == set(SCENARIOS)
    assert comparison["dataset"]["costs"] == "excluded"
    for name, metric in comparison["metrics"].items():
        saved = json.loads((output / f"{name}.json").read_text(encoding="utf-8"))
        checked, _ = verify(saved)
        assert D(checked["net_pnl"]) == D(metric["net_pnl"])
        assert all(
            D(metric[k]) == 0
            for k in ("fees", "funding", "slippage_informational_already_in_prices")
        )
        assert len(saved["decisions"]) == 512
        assert saved["end_us"] - saved["start_us"] == 512 * BAR_US
    report = (output / "report.html").read_text(encoding="utf-8")
    assert "before costs" in report and "breakeven-1R" not in report
    assert "relative to the baseline" not in report
    assert "<script" not in report
    with pytest.raises(SystemExit):
        main(["sandbox", "--output", str(output)])
