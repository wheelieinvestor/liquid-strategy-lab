from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal as D

from liquid_autonomous_trader.backtesting.execution import Order
from liquid_autonomous_trader.backtesting.historical import MINUTE_US, HistoryAssumptions, run_btc
from liquid_autonomous_trader.backtesting.history import Minute


def test_matched_entry_production_target_and_candidate_wait_for_executable_observation(
    monkeypatch, tmp_path
):
    start = int(datetime(2025, 7, 2, tzinfo=UTC).timestamp() * 1_000_000)
    rows = []
    for i in range(-1440, 30):
        price = D(100) if i < 14 else D(102)
        low = D("99.5") if i == 16 else price - D(".1")
        rows.append(
            Minute(start + i * MINUTE_US, price, price + D(".1"), low, price, D(10), "fixture")
        )
    monkeypatch.setattr(
        "liquid_autonomous_trader.backtesting.historical.minutes", lambda *args: iter(rows)
    )
    monkeypatch.setattr(
        "liquid_autonomous_trader.backtesting.historical.funding_rates", lambda _: {}
    )
    order = Order(
        "cohort",
        "BTC",
        "btc_momentum",
        D(1),
        D(0),
        D(99),
        None,
        D(10),
        "cross",
        start - 2_000_000,
        start - 1_000_000,
        start - 2_000_000,
        start + 900_000_000,
        "market",
        None,
        "filled",
        fills=1,
    )
    cohort = {
        "order": asdict(order),
        "fill": {
            "at_us": start,
            "data": {
                "symbol": "BTC",
                "owner": "btc_momentum",
                "quantity": "1",
                "price": "100",
                "fee": ".1",
                "leverage": "10",
                "mode": "cross",
            },
        },
        "plan": {"target": "101", "tick_size": ".01"},
    }
    assumptions = HistoryAssumptions(
        native_price_step=".01", native_quantity_step=".01", fee_per_side=".001"
    )
    baseline = run_btc(tmp_path, start, start + 30 * MINUTE_US, assumptions, matched_entry=cohort)
    legacy = run_btc(
        tmp_path,
        start,
        start + 30 * MINUTE_US,
        replace(assumptions, policy="legacy-production"),
        matched_entry=cohort,
    )
    candidate = run_btc(
        tmp_path,
        start,
        start + 30 * MINUTE_US,
        replace(assumptions, policy="breakeven-1R"),
        matched_entry=cohort,
    )
    assert D(baseline["ledger"]["equity"]) == D("1001.9")
    assert D(legacy["ledger"]["equity"]) == D("1001.78801")
    assert D(candidate["ledger"]["equity"]) == D("999.8")
    exit_fill = next(
        e for e in legacy["ledger_journal"] if e["kind"] == "fill" and e["data"].get("reduce_only")
    )
    assert exit_fill["at_us"] == start + 16 * MINUTE_US
    assert len([e for e in legacy["ledger_journal"] if e["kind"] == "fill"]) == 2
    assert legacy["ledger"]["positions"] == {}
    assert candidate["ledger"]["positions"] == {}
