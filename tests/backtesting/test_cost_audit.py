from dataclasses import replace
from decimal import Decimal as D

import pytest

from liquid_autonomous_trader.backtesting.ledger import HOUR_US
from liquid_autonomous_trader.backtesting.report import trade_cohorts
from tests.backtesting.test_execution import engine, quote, request, submit


def test_roundtrip_execution_friction_includes_both_sides_without_double_charge():
    sim = engine()
    submit(sim)
    quote(sim, 2)
    sim.manage("close", "one", "close", 2)
    quote(sim, 3)
    assert sim.ledger.slippage == D(".04")
    assert sim.ledger.realized == D("-.04")
    assert sim.ledger.fees == D(".4")
    assert sim.ledger.equity() == D("999.56")


@pytest.mark.parametrize(
    "short,gap,expected",
    [
        (False, False, "97.30"),
        (False, True, "89.82"),
        (True, False, "102.71"),
        (True, True, "110.22"),
    ],
)
def test_bar_stop_costs_are_adverse_on_both_sides_and_gaps(short, gap, expected):
    sim = engine()
    req = replace(request(), side="short", proposed_stop=D(102)) if short else request()
    sim.submit("one", req, 1, mode="cross", expires_us=1000)
    quote(sim, 2)
    opening = "110" if short and gap else "90" if gap else "100"
    sim.bar(
        "BTC",
        3,
        10,
        open_price=opening,
        high="112",
        low="88",
        close="100",
        spread_bps="20",
        extra_slippage_bps="10",
    )
    exit_fill = [e["data"] for e in sim.ledger.journal if e["kind"] == "fill"][-1]
    assert D(exit_fill["price"]) == D(expected)
    assert D(exit_fill["fee"]) == D(2) * D(expected) * D(".001")
    assert sim.ledger.slippage > D(".02")


def test_repeated_funding_delivery_is_counted_once_in_trade_report():
    sim = engine()
    submit(sim)
    quote(sim, 2)
    funding = dict(symbol="BTC", oracle_price="105", rate=".001", settlement_us=HOUR_US)
    sim.ledger.apply("fund", HOUR_US, "funding", **funding)
    sim.ledger.apply("retry-with-new-id", HOUR_US, "funding", **funding)
    sim._close("BTC", D("110"), HOUR_US + 1, "audit")
    closed, opened = trade_cohorts(sim.ledger.journal)
    assert not opened
    assert closed[0]["funding"] == sim.ledger.funding == D("-.210")
    assert closed[0]["net_pnl"] == sim.ledger.equity() - sim.ledger.initial_cash


def test_zero_cost_synthetic_quote_does_not_weaken_observed_book_validation():
    sim = engine()
    submit(sim)
    with pytest.raises(ValueError, match="invalid_book"):
        sim.quote("BTC", 2, bids=[("100", "10")], asks=[("100", "10")])
    sim.quote("BTC", 2, bids=[("100", "10")], asks=[("100", "10")], synthetic_locked=True)
    assert sim.ledger.positions["BTC"].entry == 100
    assert sim.ledger.slippage == 0


def test_failed_protection_uses_opposite_depth_and_retains_emergency_exit():
    sim = engine()
    submit(sim)
    sim.quote("BTC", 2, bids=[("94.99", ".25"), ("94.99", ".25")], asks=[("95.01", "10")])
    assert sim.ledger.positions["BTC"].quantity == D("1.5")
    assert "BTC" in sim.triggered_stops
    assert sim.ledger.reservations == {}
    fills = [e["data"] for e in sim.ledger.journal if e["kind"] == "fill"]
    assert D(fills[-1]["price"]) == D("94.99")
    assert D(fills[-1]["quantity"]) == D("-.25")
    restored = type(sim).restore(sim.checkpoint())
    quote(restored, 3, bid="94", ask="95")
    assert not restored.ledger.positions
    assert restored.ledger.realized == D("-1.525")
    assert restored.ledger.fees == D(".378515")


def test_bar_target_also_pays_exit_costs():
    sim = engine()
    submit(sim, target="105")
    quote(sim, 2)
    sim.bar(
        "BTC",
        3,
        10,
        open_price="100",
        high="106",
        low="99",
        close="105",
        spread_bps="20",
        extra_slippage_bps="10",
    )
    assert sim.ledger.realized == D("9.56")
    assert sim.ledger.fees == D(".40960")
