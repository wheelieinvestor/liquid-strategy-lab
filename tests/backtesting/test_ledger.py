from decimal import Decimal as D

import pytest

from liquid_autonomous_trader.backtesting.ledger import (
    HOUR_US,
    FeeSchedule,
    Instrument,
    Ledger,
    Tier,
    native_total_fee,
)


def instrument(symbol="BTC"):
    return Instrument(
        symbol,
        D("0.01"),
        D("0.01"),
        D(10),
        (Tier(D(0), D(40)), Tier(D(10000), D(20))),
        "fixture-v1",
        "synthetic_assumption",
    )


def ledger(cash="1000"):
    return Ledger(D(cash), {s: instrument(s) for s in ("BTC", "xyz:TSLA")})


def fill(book, key, at=1, **changes):
    data = dict(
        symbol="BTC",
        owner="btc_momentum",
        quantity="2",
        price="100",
        fee="0.2",
        leverage="10",
        mode="cross",
    )
    data.update(changes)
    return book.apply(key, at, "fill", **data)


def test_hand_calculated_partial_exit_funding_and_fee_rebate():
    book = ledger()
    fill(book, "entry")  # $200 notional, $20 margin; cash 999.8
    book.apply("mark", 2, "mark", symbol="BTC", price="110")
    assert book.equity() == D("1019.8")
    assert book.margin(book.positions["BTC"]) == 22
    book.apply(
        "fund",
        HOUR_US,
        "funding",
        symbol="BTC",
        oracle_price="105",
        rate="0.001",
        settlement_us=HOUR_US,
    )  # long debit .21, NOT .22 mark notional
    fill(book, "partial", HOUR_US + 1, quantity="-1", price="110", fee="-0.01", reduce_only=True)
    assert book.cash() == D("1009.60")
    assert book.realized == 10
    assert book.funding == D("-0.210")
    fill(book, "exit", HOUR_US + 2, quantity="-1", price="90", fee="0.09", reduce_only=True)
    assert book.cash() == book.equity() == D("999.51")
    assert book.realized == 0 and book.fees == D("0.28")
    assert book.turnover == 400
    assert book.positions == {}
    assert book.reconcile()


def test_short_funding_credit_duplicate_and_original_currency():
    book = ledger()
    fill(book, "entry", quantity="-2")
    event = dict(symbol="BTC", oracle_price="100", rate="0.001", settlement_us=HOUR_US)
    book.apply("fund", HOUR_US, "funding", **event)
    book.apply("same-economic-settlement", HOUR_US, "funding", **event)
    assert book.cash() == 1000
    with pytest.raises(ValueError, match="conflicting_funding"):
        book.apply("bad-revision", HOUR_US, "funding", **{**event, "rate": "0.002"})
    with pytest.raises(ValueError, match="boundary"):
        book.apply("late", HOUR_US + 1, "funding", **event)
    assert native_total_fee({"fee": "1.364", "builderFee": "0.5", "feeToken": "USDC"}) == D("1.364")
    with pytest.raises(ValueError, match="currency"):
        native_total_fee({"fee": "1", "feeToken": "OTHER"})


def test_cross_account_equity_not_one_over_leverage_and_isolated_firewall():
    cross = ledger("100")
    isolated = ledger("100")
    fill(cross, "entry", quantity="10", leverage="40", fee="0")
    fill(isolated, "entry", quantity="10", leverage="40", fee="0", mode="isolated")
    for book in (cross, isolated):
        book.apply("adverse", 2, "mark", symbol="BTC", price="97")
        assert book.equity() == 70  # loss exceeds the initial $25 collateral
    assert cross.breaches() == []  # $70 cross equity > $12.125 maintenance
    assert isolated.breaches()[0]["equity"] == "-5"
    assert isolated.cross_cash == 75
    fill(
        cross,
        "other",
        3,
        symbol="xyz:TSLA",
        owner="flow_show_mirror",
        quantity="1",
        price="100",
        leverage="10",
        fee="0",
    )
    cross.apply("correlated-crash", 4, "mark", symbol="xyz:TSLA", price="40")
    assert cross.breaches()[0]["scope"] == "cross"
    assert cross.breaches()[0]["equity"] == "10"


def test_maintenance_tier_deduction_is_continuous():
    spec = instrument()
    assert spec.maintenance(D(10000)) == D(125)
    assert spec.maintenance(D(12000)) == D(175)  # 10k*.0125 + 2k*.025
    assert spec.maintenance(D(9999)) == D("124.9875")


def test_ownership_reserves_precision_and_reduce_only_fail_atomically():
    book = ledger()
    book.apply(
        "reserve",
        0,
        "reserve",
        order_id="a",
        symbol="BTC",
        owner="btc_momentum",
        collateral="20",
        state="unknown",
    )
    with pytest.raises(ValueError, match="cannot_be_released"):
        book.apply("unsafe", 1, "release", order_id="a", outcome_known=False)
    fill(book, "entry", order_id="a")
    assert book.reservations["a"]["collateral"] == 0
    before = book.state()
    for changes in (
        {"owner": "flow_show_mirror"},
        {"quantity": "-3", "reduce_only": True},
        {"quantity": "0.001"},
        {"price": "100.001"},
    ):
        with pytest.raises(ValueError):
            fill(book, "invalid", 2, **changes)
        assert book.state() == before
    with pytest.raises(ValueError, match="conflicting_ledger_retry"):
        fill(book, "entry", order_id="a", fee="1")
    assert not fill(book, "entry", order_id="a")


def test_stop_ownership_original_risk_and_time_survive_replay():
    book = ledger()
    fill(book, "entry")
    book.apply("original", 2, "stop", symbol="BTC", owner="btc_momentum", price="95", original=True)
    book.apply("tighten", 3, "stop", symbol="BTC", owner="btc_momentum", price="98")
    for key, changes in [
        ("foreign", {"owner": "flow_show_mirror"}),
        ("loose", {"price": "94", "allow_loosen": True}),
        ("crossed", {"price": "101"}),
        ("no-evidence", {"price": "96"}),
    ]:
        with pytest.raises(ValueError):
            book.apply(
                key,
                4,
                "stop",
                **{"symbol": "BTC", "owner": "btc_momentum", "price": "99", **changes},
            )
    book.apply(
        "bounded-loosen",
        5,
        "stop",
        symbol="BTC",
        owner="btc_momentum",
        price="96",
        allow_loosen=True,
    )
    restored = Ledger.replay(book.initial_cash, book.instruments, book.journal)
    assert restored.state() == book.state()
    assert restored.positions["BTC"].stop_active_us == 5
    assert not fill(restored, "entry")


def test_fee_versions_and_slippage_are_not_double_charged():
    schedule = FeeSchedule(
        "test-v1",
        "liquid-native",
        "fixture",
        "none",
        0,
        100,
        D(".001"),
        D("-.0001"),
        "synthetic_assumption",
    )
    assert schedule.charge(D(200), 1, False) == D(".2")
    assert schedule.charge(D(200), 1, True) == D("-.02")
    with pytest.raises(ValueError, match="effective_window"):
        schedule.charge(D(200), 100, False)
    book = ledger()
    fill(book, "entry", reference_price="99")
    assert book.cash() == D("999.8")  # slippage is already included in execution price
    assert book.slippage == 2
