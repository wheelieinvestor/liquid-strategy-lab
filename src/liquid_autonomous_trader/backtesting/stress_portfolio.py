"""Shared-capital/ownership scenarios using the four production adapters."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D

from liquid_autonomous_trader.backtesting.events import canonical, digest
from liquid_autonomous_trader.backtesting.fixtures import (
    NOW,
    US,
    adapters,
    cramer_payload,
    event,
    flow_payload,
    market_events,
    portfolio_events,
)
from liquid_autonomous_trader.backtesting.ledger import Ledger, Tier
from liquid_autonomous_trader.backtesting.portfolio import PortfolioEngine
from liquid_autonomous_trader.backtesting.stress_execution import invariants


def shifted(rows, seconds):
    def transform(value, key=None):
        if isinstance(value, dict):
            return {k: transform(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [transform(v) for v in value]
        if key in {"t", "T", "time"} and type(value) is int:
            return value + seconds * 1000
        if isinstance(value, str) and "T" in value and value.endswith("+00:00"):
            return (datetime.fromisoformat(value) + timedelta(seconds=seconds)).isoformat()
        return value

    return [
        replace(
            e,
            event_us=e.event_us + seconds * 1_000_000,
            published_us=e.published_us + seconds * 1_000_000,
            available_us=e.available_us + seconds * 1_000_000,
            retrieved_us=e.retrieved_us + seconds * 1_000_000,
            payload_json=canonical(transform(e.payload)),
        )
        for e in rows
    ]


def execute(case):
    if case.family != "portfolio":
        raise ValueError("portfolio_case_required")
    a = adapters()
    sim = a.account.sim
    a.close()
    sim.ledger.instruments["ETH"] = replace(sim.ledger.instruments["BTC"], symbol="ETH")
    name, checks = case.archetype, []
    if name == "scarce_collateral":
        sim.ledger = Ledger(D(200) + D(case.variant), sim.ledger.instruments)
    single_flow = name in {
        "full_sleeve",
        "epoch_before_dispatch_reject",
        "epoch_closed_position",
        "day_rollover",
        "capital_released_later_entry",
    }
    single_btc = name in {"margin_tier_boundary", "isolated_funding_drain", "cross_margin_pressure"}
    enabled = (
        ("flow_show_mirror",)
        if single_flow
        else ("btc_momentum",)
        if single_btc
        else ("flow_show_mirror", "xyz100_gex", "btc_momentum", "inverse_cramer")
    )
    mode = "isolated" if name == "isolated_funding_drain" else "cross"
    engine = PortfolioEngine(
        sim, enabled=enabled, mode="synthetic_stress", margin_mode=mode, latency_us=case.latency_us
    )
    first, second = portfolio_events(separate_cramer=name != "cramer_btc_collision")
    try:
        if name == "foreign_exposure":
            sim.ledger.apply(
                "foreign",
                US - 1,
                "fill",
                symbol="ETH",
                owner="manual",
                quantity=D(40),
                price=D(100),
                fee=D(0),
                leverage=D(10),
                mode="cross",
            )
        elif name == "unknown_reservation":
            sim.ledger.apply(
                "unknown",
                US - 1,
                "reserve",
                order_id="external-unknown",
                symbol="BTC",
                owner="btc_momentum",
                collateral=D(50),
                state="unknown",
            )
        elif name == "full_sleeve":
            for i in range(3):
                symbol = f"xyz:FIXTURE{i}"
                sim.ledger.instruments[symbol] = replace(
                    sim.ledger.instruments["xyz:NVDA"], symbol=symbol
                )
                sim.ledger.apply(
                    "sleeve:" + str(i),
                    US - 1,
                    "fill",
                    symbol=symbol,
                    owner="flow_show_mirror",
                    quantity=D(1),
                    price=D(100),
                    fee=D(0),
                    leverage=D(10),
                    mode="cross",
                )
                sim.ledger.apply(
                    "stop:" + str(i),
                    US - 1,
                    "stop",
                    symbol=symbol,
                    owner="flow_show_mirror",
                    price=D(98),
                    original=True,
                )
        elif name == "margin_tier_boundary":
            old = sim.ledger.instruments["BTC"]
            sim.ledger.instruments["BTC"] = replace(
                old, tiers=(Tier(D(0), D(40)), Tier(D(1000), D(20)))
            )
            assert sim.ledger.instruments["BTC"].maintenance(D(2000)) == D("37.5")
            checks.append("hand_calculated_progressive_maintenance")
        elif name == "epoch_before_dispatch_reject":
            db = engine.account.db
            db.execute(
                "INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "previous",
                    "flow_show_mirror",
                    "xyz:NVDA",
                    "long",
                    "rejected",
                    None,
                    "0",
                    "0",
                    "98",
                ),
            )
            for state in ("reserved", "submitting", "rejected"):
                db.execute(
                    "INSERT INTO lifecycle(intent_id,state,detail,at) VALUES(?,?,?,?)",
                    (
                        "previous",
                        state,
                        canonical({"reason": "entry_rejected_before_dispatch"})
                        if state == "rejected"
                        else "{}",
                        NOW.isoformat(),
                    ),
                )
        elif name == "flow_btc_collision":
            # BTC is outside Flow's exact XYZ mapping: an attempted alias collision
            # must be rejected before it can claim the BTC owner's market.
            for i, row in enumerate(first):
                if row.kind == "market_identity" and row.instrument == "NVDA":
                    value = row.payload
                    value["ticker"]["coin"] = "BTC"
                    first[i] = replace(row, payload_json=canonical(value))
        engine.run(first)
        if name == "unknown_reservation":
            assert not sim.orders
            assert any(
                r.get("reason") == "prior_write_outcome_unresolved" for r in engine.decisions
            )
            checks.append("unknown_obligation_blocks_every_sleeve")
        elif name == "full_sleeve":
            assert not sim.orders
            assert any(r.get("reason") == "sleeve_position_limit" for r in engine.decisions)
            checks.append("all_owned_positions_count_toward_sleeve_limit")
        else:
            if name == "partial_fill_reservation":
                changed = []
                for row in second:
                    p = row.payload
                    for side in p["levels"]:
                        for level in side:
                            level["sz"] = ".5"
                    changed.append(replace(row, payload_json=canonical(p)))
                second = changed
            if name == "halt_pending_entries":
                sim.halted = True
            engine.run(second)
            if name == "simultaneous_agents":
                assert {o.owner for o in sim.orders.values() if o.state == "filled"} == set(enabled)
                assert len(sim.ledger.positions) == 4
                checks.append("all_four_eligible_agents_share_one_account")
            elif name == "cramer_btc_collision":
                assert sim.ledger.positions["BTC"].owner == "btc_momentum"
                assert not any(o.owner == "inverse_cramer" for o in sim.orders.values())
                checks.append("btc_priority_preserves_owner_against_cramer_collision")
            elif name == "flow_btc_collision":
                assert not any(o.owner == "flow_show_mirror" for o in sim.orders.values())
                assert sim.ledger.positions["BTC"].owner == "btc_momentum"
                checks.append("invalid_flow_alias_cannot_claim_btc")
            elif name in {"foreign_exposure", "scarce_collateral"}:
                assert len(sim.orders) < 4
                assert any(r.get("request") and r["reason"] != "accepted" for r in engine.decisions)
                checks.append("shared_collateral_denial_is_retained")
            elif name == "margin_tier_boundary":
                assert "BTC" not in sim.ledger.positions
                assert any(e.get("reason") == "tier_leverage_exceeded" for e in sim.events)
                checks.append("upper_tier_maximum_leverage_enforced")
            elif name == "partial_fill_reservation":
                assert sim.ledger.reservations
                assert all(o.state == "partial" for o in sim.orders.values())
                checks.append("partial_fills_retain_shared_obligations")
            elif name == "halt_pending_entries":
                # Already dispatched orders may still fill. A halt cannot rewrite
                # broker history; it suppresses subsequent discretionary entries.
                assert len(sim.orders) == 4 and sim.halted
                checks.append("halt_retains_preexisting_dispatched_obligations")
            elif name in {"correlated_selloff", "native_stop_during_halt"}:
                if name == "native_stop_during_halt":
                    sim.halted = True
                shocks = []
                for row in second:
                    p = row.payload
                    p["time"] = (US + 20_000_000) // 1000
                    p["levels"] = [
                        [{"px": "94.99", "sz": "1000", "n": 1}],
                        [{"px": "95.01", "sz": "1000", "n": 1}],
                    ]
                    shocks.append(
                        event("native_book", row.instrument, p, at=US + 20_000_000, sequence=3)
                    )
                engine.run(shocks)
                assert all(p.quantity < 0 for p in sim.ledger.positions.values())
                checks.append("correlated_native_stops_execute_independent_of_application_halt")
            elif name in {"isolated_funding_drain", "cross_margin_pressure"}:
                if name == "isolated_funding_drain":
                    engine.run(
                        [
                            event(
                                "funding_settlement",
                                "BTC",
                                {"oracle": "100", "rate": ".1"},
                                at=US + 20_000_000,
                            )
                        ]
                    )
                    assert any(b["scope"] == "BTC" for b in sim.ledger.breaches())
                else:
                    sim.ledger.apply(
                        "cross-stress-mark", US + 20_000_000, "mark", symbol="BTC", price=D(1)
                    )
                    engine.run([event("tick", "BTC", {}, at=US + 20_000_000)])
                    assert any(b["scope"] == "cross" for b in sim.ledger.breaches())
                assert (
                    engine.termination_reason
                    == "unsupported_liquidation_execution_after_maintenance_breach"
                )
                checks.append("margin_breach_detected_without_invented_liquidation_fill")
            elif name in {"epoch_closed_position", "day_rollover", "capital_released_later_entry"}:
                owner = next(iter(sim.orders))
                sim.manage("fixture-close", owner, "close", US + 10_000_000, latency_us=1)
                closes = shifted(
                    [e for e in first if e.kind == "native_book" and e.instrument == "xyz:NVDA"], 11
                )
                engine.run(closes)
                assert "xyz:NVDA" not in sim.ledger.positions and not sim.ledger.reservations
                seconds = 20 if name == "epoch_closed_position" else 86400
                payload = flow_payload()
                payload["signal"]["delivery_event_id"] = "next-delivery"
                fresh = shifted(
                    [*market_events(), event("flow_delivery", "NVDA", payload, sequence=2)], seconds
                )
                engine.run(fresh)
                if name == "epoch_closed_position":
                    assert len(sim.orders) == 1
                else:
                    assert len(sim.orders) == 2
                checks.append("flow_epoch_rollover_and_released_capital")
            elif name == "epoch_before_dispatch_reject":
                assert "xyz:NVDA" in sim.ledger.positions
                checks.append("proven_no_dispatch_rejection_releases_epoch_eligibility")
            elif name == "cramer_deadline":
                deadline = US + 168 * 3600 * 1_000_000
                engine.run([event("tick", "ETH", {}, at=deadline)])
                assert any(key.endswith(":one-week-close") for key in sim.management)
                checks.append("cramer_168_hour_exit_overrides_model_unavailability")
            elif name == "cramer_opposing_signal":
                payload = cramer_payload(source_id=str(1000 + case.variant))
                payload["body"]["post"]["text"] = "Sell $ETH now"
                call = payload["body"]["classification"]["calls"][0]
                call.update(
                    ticker="ETH", issuer="Ethereum", stance="bearish", evidence="Sell $ETH now"
                )
                fresh = shifted(
                    [
                        *market_events("ETH", "ETH", 40),
                        event("cramer_classification", "ETH", payload, sequence=2),
                    ],
                    61,
                )
                before = sim.ledger.positions["ETH"].quantity
                engine.run(fresh)
                assert sim.ledger.positions["ETH"].quantity == before
                assert not sim.management
                checks.append("v5_missing_cache_preserves_position_on_opposing_cramer_signal")
            else:
                raise ValueError("portfolio_scenario_not_implemented")
        checks.extend(invariants(sim))
        restored = PortfolioEngine.restore(engine.checkpoint())
        try:
            assert restored.result() == engine.result(), "portfolio_restart_changed_core"
        finally:
            restored.close()
        result = {
            "case_id": case.case_id,
            "seed": case.seed,
            "status": "passed",
            "mode": "synthetic_stress",
            "checks": checks + ["full_portfolio_restart"],
            "core_hash": engine.result()["core_sha256"],
            "ledger": sim.ledger.state(),
            "decisions": engine.decisions,
            "interpretation": "conditional authored market/source states; "
            "no independent performance history",
        }
        return {**result, "scenario_hash": digest(result)}
    finally:
        engine.close()
