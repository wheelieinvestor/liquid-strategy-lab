from dataclasses import replace
from decimal import Decimal as D

import pytest
from test_sources import (
    US,
    adapters,
    cramer_payload,
    event,
    flow_payload,
    gamma_events,
    market_events,
)

from liquid_autonomous_trader.backtesting.events import canonical
from liquid_autonomous_trader.backtesting.portfolio import PortfolioEngine


def fixture_events():
    rows = [
        *market_events(),
        *gamma_events(),
        *market_events("BTC", "BTC", 40),
        event("flow_delivery", "NVDA", flow_payload()),
        event("cramer_classification", "BTC", cramer_payload()),
    ]
    metadata = [e for e in rows if e.kind == "native_metadata" and e.instrument == "xyz"]
    rows = [e for e in rows if not (e.kind == "native_metadata" and e.instrument == "xyz")]
    rows.append(
        replace(
            metadata[0],
            payload_json=canonical(
                {"universe": [asset for e in metadata for asset in e.payload["universe"]]}
            ),
        )
    )
    next_books = []
    for row in rows:
        if row.kind == "native_book":
            p = row.payload
            p["time"] += 2000
            next_books.append(event("native_book", row.instrument, p, at=US + 2000000, sequence=2))
    return rows, next_books


def engine(**kwargs):
    a = adapters()
    sim = a.account.sim
    a.close()
    return PortfolioEngine(sim, **kwargs)


def test_shared_source_order_no_same_observation_fill_and_missing_jev_hold():
    first, second = fixture_events()
    e = engine()
    result = e.run(first)
    assert not e.sim.ledger.positions
    attempts = [r["strategy"] for r in e.decisions if "request" in r]
    assert attempts[0] == "flow_show_mirror" and attempts[1] == "xyz100_gex"
    assert attempts[-1] == "inverse_cramer"
    result = e.run(second)
    assert set(e.sim.ledger.positions) >= {"xyz:NVDA", "BTC"}
    assert e.sim.ledger.positions["BTC"].quantity < 0
    assert e.reviews and all(r["reason"] == "exact_cache_missing" for r in e.reviews)
    assert not e.sim.management
    assert not any(
        r.get("reason") in {"KeyError", "TypeError", "AttributeError"} for r in e.decisions
    )
    assert result["core_sha256"]
    e.close()


def test_portfolio_restart_exact_core_and_future_data_invariance():
    first, second = fixture_events()
    e = engine()
    prefix = e.run(first)
    checkpoint = e.checkpoint()
    restored = PortfolioEngine.restore(checkpoint)
    uninterrupted = e.run(second)
    resumed = restored.run(second)
    assert uninterrupted == resumed
    another = engine()
    assert another.run(first + second, end_us=US) == prefix
    for value in (e, restored, another):
        value.close()


def test_cramer_mandatory_deadline_remains_when_model_unavailable():
    first, second = fixture_events()
    e = engine(enabled=("inverse_cramer",))
    e.run(first + second)
    order = next(iter(e.sim.orders))
    deadline = US + 168 * 3600 * 1000000
    e.run([event("tick", "BTC", {}, at=deadline, sequence=2)])
    assert e.sim.management[order + ":one-week-close"]["state"] == "pending"
    assert e.sim.ledger.positions["BTC"].quantity < 0
    quote = second[-1].payload  # explicit fresh BTC executable observation
    quote["time"] = (deadline + 2000000) // 1000
    e.run([event("native_book", "BTC", quote, at=deadline + 2000000, sequence=3)])
    assert "BTC" not in e.sim.ledger.positions
    e.close()


def test_shared_capacity_rejection_is_retained_and_funding_sign_is_exact():
    first, second = fixture_events()
    e = engine()
    # Explicit foreign exposure consumes shared capital; it is not an owned sleeve.
    e.sim.ledger.apply(
        "foreign",
        US - 1,
        "fill",
        symbol="xyz:XYZ100",
        owner="manual",
        quantity=D(80),
        price=D(100),
        fee=D(0),
        leverage=D(10),
        mode="cross",
    )
    e.run(first + second)
    assert any(
        r.get("reason") not in {"accepted", "hold"} and r.get("request") for r in e.decisions
    )
    before = e.sim.ledger.funding
    stamp = US + 3600000000
    qty = e.sim.ledger.positions["xyz:XYZ100"].quantity
    e.run([event("funding_settlement", "xyz:XYZ100", {"rate": "0.001", "oracle": "100"}, at=stamp)])
    assert e.sim.ledger.funding - before == -qty * D(100) * D(".001")
    e.close()


def test_checkpoint_rejects_tampered_account_state():
    e = engine()
    checkpoint = e.checkpoint()
    checkpoint["payload"]["margin_mode"] = "isolated"
    with pytest.raises(ValueError, match="integrity_failure"):
        PortfolioEngine.restore(checkpoint)
    e.close()
