"""Execution/recovery scenario handlers with independent state assertions."""

from __future__ import annotations

from dataclasses import replace
from decimal import ROUND_CEILING, ROUND_FLOOR
from decimal import Decimal as D

from liquid_autonomous_trader.backtesting.events import digest
from liquid_autonomous_trader.backtesting.execution import SimulatedExecution
from liquid_autonomous_trader.backtesting.fixtures import US, adapters
from liquid_autonomous_trader.backtesting.ledger import Ledger
from liquid_autonomous_trader.live_policy import EntryRiskRequest, Strategy


def environment(case):
    source = adapters()
    sim = source.account.sim
    source.close()
    direction = 1 if case.direction == "long" else -1
    request = EntryRiskRequest(
        Strategy.BTC,
        "BTC",
        case.direction,
        D(100),
        D(100) - direction * D(2),
        D(".01"),
        D(1),
        D(2000),
        D(50),
        D(0),
        D(40),
        D(40),
        D(10),
        D(0),
        stop_price_step=D(".01"),
        minimum_notional_usd=D(10),
        require_full_requested_size=True,
    )
    return sim, request


def invariants(sim):
    sim.ledger.reconcile()
    replay = Ledger.replay(sim.ledger.initial_cash, sim.ledger.instruments, sim.ledger.journal)
    assert replay.state() == sim.ledger.state(), "ledger_journal_replay_mismatch"
    for position in sim.ledger.positions.values():
        if position.owner in {s.value for s in Strategy}:
            assert position.stop is not None, "owned_position_unprotected"
            sign = 1 if position.quantity > 0 else -1
            assert sign * (position.stop - position.original_stop) >= 0, (
                "stop_outside_original_envelope"
            )
    for event in sim.events:
        if event["kind"] == "fill":
            order = sim.orders[event["order"]]
            assert event["at_us"] > order.submitted_us, "same_observation_fill"
            assert event["at_us"] >= max(order.executable_us, order.acknowledged_us), (
                "fill_before_ack"
            )
    restored = SimulatedExecution.restore(sim.checkpoint())
    assert restored.checkpoint() == sim.checkpoint(), "execution_checkpoint_mismatch"
    return [
        "ledger_reconciliation",
        "journal_replay",
        "owned_stop_envelope",
        "causal_acknowledged_fills",
        "exact_execution_restart",
    ]


def execute(case):
    if case.family != "execution":
        raise ValueError("execution_case_required")
    sim, request = environment(case)
    direction = 1 if case.direction == "long" else -1
    name = case.archetype
    elapsed = max(case.latency_us, 1_000_000)
    at = US + elapsed + 1
    checks = []

    def submit(*, fault=None, **changes):
        return sim.submit(
            "entry",
            request,
            US,
            mode="cross",
            expires_us=US + 60_000_000,
            latency_us=case.latency_us,
            fault=fault,
            **changes,
        )

    def quote(when, *, price="100", depth="1000"):
        mid = D(price)
        half = mid * D(case.spread_bps) / 20000
        step = sim.ledger.instruments["BTC"].price_step
        bid = ((mid - half) / step).to_integral_value(rounding=ROUND_FLOOR) * step
        ask = ((mid + half) / step).to_integral_value(rounding=ROUND_CEILING) * step
        sim.quote("BTC", when, bids=[(str(bid), str(depth))], asks=[(str(ask), str(depth))])

    def restart():
        nonlocal sim
        before = sim.checkpoint()
        sim = SimulatedExecution.restore(before)
        assert sim.checkpoint() == before, "restart_changed_obligation"
        checks.append("restart_at_" + name)

    if name in {"reject", "rate_limit"}:
        assert submit(fault=name) == name
        quote(at)
        assert not sim.orders and not sim.ledger.reservations and not sim.ledger.positions
        checks.append("rejection_did_not_create_execution")
    elif name in {"unknown_entry", "crash_unknown"}:
        assert submit(fault="unknown") == "unknown"
        if name == "crash_unknown":
            restart()
        quote(at)
        assert not sim.ledger.positions and sim.ledger.reservations
        other = replace(request, symbol="xyz:NVDA")
        assert (
            sim.submit("other", other, at, mode="cross", expires_us=at + 1_000_000)
            == "prior_write_outcome_unresolved"
        )
        sim.reconcile_unknown("entry", at + 1, terminal_no_fill=True)
        assert not sim.ledger.reservations
        checks.extend(
            [
                "unknown_no_implicit_fill",
                "unknown_blocks_other_market",
                "positive_reconcile_releases",
            ]
        )
    elif name == "ack_delay":
        assert submit(ack_delay_us=elapsed + 5_000_000) == "accepted"
        quote(at)
        assert not sim.ledger.positions
        quote(US + elapsed + 5_000_001)
        assert sim.ledger.positions
        checks.append("ack_delay_enforced")
    elif name in {"limit_touch", "limit_cross"}:
        half = D(100) * D(case.spread_bps) / 20000
        limit = D(100) + direction * (half if name == "limit_touch" else half + D(1))
        assert submit(kind="limit", limit=str(limit)) == "accepted"
        quote(at)
        if name == "limit_touch":
            assert not sim.ledger.positions, "touch_is_not_queue_fill"
            quote(at + 1, price=str(D(100) - direction * D(1)))
        assert sim.ledger.positions
        checks.append("strict_marketable_limit_only")
    elif name in {"no_fill", "disconnected_book", "stale_book"}:
        assert submit() == "accepted"
        if name == "no_fill":
            quote(at, depth="0")
        elif name == "stale_book":
            # The driver rejects stale evidence before it reaches transport.
            from liquid_autonomous_trader.backtesting.fixtures import event
            from liquid_autonomous_trader.backtesting.portfolio import PortfolioEngine

            engine = PortfolioEngine(sim, enabled=())
            payload = {
                "time": (US - 10_000_000) // 1000,
                "levels": [[{"px": "99", "sz": "1000"}], [{"px": "101", "sz": "1000"}]],
            }
            engine.run([event("native_book", "BTC", payload, at=at)])
            assert any(r["reason"] == "noncausal_or_stale_execution_book" for r in engine.decisions)
            engine.close()
        assert not sim.ledger.positions
        quote(US + 61_000_000)
        assert sim.orders["entry"].state == "expired" and not sim.ledger.reservations
        checks.append("no_executable_evidence_no_fill")
    else:
        assert submit() == "accepted"
        if name in {"crash_reserved", "restore_then_later_fill"}:
            restart()
        if name == "duplicate_entry":
            assert submit() == "duplicate"
        if name in {"partial_depth", "crash_partial", "cancel_latency"}:
            partial = max(D(".01"), D(10) * D(case.depth_fraction))
            quote(at, depth=str(partial))
            assert abs(sim.ledger.positions["BTC"].quantity) == partial
            assert sim.ledger.reservations
            if name == "crash_partial":
                restart()
            if name == "cancel_latency":
                sim.cancel("entry", at, latency_us=10)
                quote(at + 1, depth=".5")
                expected = partial + D(".5")
                quote(at + 10)
                assert abs(sim.ledger.positions["BTC"].quantity) == expected
                assert sim.orders["entry"].state == "cancelled"
            else:
                quote(at + 1)
                assert abs(sim.ledger.positions["BTC"].quantity) == 20
            checks.append("partial_depth_and_reservation_preserved")
        else:
            quote(at)
            assert abs(sim.ledger.positions["BTC"].quantity) == 20
            if name == "cancel_after_fill":
                sim.cancel("entry", at)
                quote(at + 1)
                assert abs(sim.ledger.positions["BTC"].quantity) == 20
                checks.append("cancel_does_not_undo_confirmed_fill")
            elif name in {"stop_replace_latency", "lost_stop_ack", "crash_stop_pending"}:
                before = sim.ledger.positions["BTC"].stop
                stop = D(100) - direction * D(1)
                outcome = sim.manage(
                    "stop",
                    "entry",
                    "stop",
                    at,
                    stop=str(stop),
                    latency_us=10_000_000,
                    fault="unknown" if name == "lost_stop_ack" else None,
                )
                assert outcome == ("unknown" if name == "lost_stop_ack" else "pending")
                if name == "crash_stop_pending":
                    restart()
                quote(at + 1)
                assert sim.ledger.positions["BTC"].stop == before
                if name == "lost_stop_ack":
                    # Existing native protection survives a lost replacement reply.
                    quote(at + 2, price=str(D(100) - direction * D(5)))
                    assert not sim.ledger.positions
                    checks.append("native_stop_survives_unknown_replacement")
                else:
                    quote(at + 10_000_000)
                    assert sim.ledger.positions["BTC"].stop == stop
                    checks.append("old_stop_until_acknowledged_replacement")
            elif name in {"crash_software_exit", "duplicate_management"}:
                assert sim.manage("close", "entry", "close", at, latency_us=1) == "pending"
                if name == "duplicate_management":
                    assert sim.manage("close", "entry", "close", at, latency_us=1) == "duplicate"
                else:
                    restart()
                quote(at + 1, depth="1")
                assert abs(sim.ledger.positions["BTC"].quantity) == 19
                quote(at + 2)
                assert not sim.ledger.positions
                checks.append("software_exit_consumes_later_depth_once")
            elif name in {"stop_gap", "partial_stop_rebound"}:
                depth = "1" if name == "partial_stop_rebound" else "1000"
                quote(
                    at + 1, price=str(D(100) - direction * D(10) * D(case.shock_scale)), depth=depth
                )
                if name == "partial_stop_rebound":
                    assert abs(sim.ledger.positions["BTC"].quantity) == 19
                    quote(at + 2, price="100")
                assert not sim.ledger.positions
                checks.append("gap_or_triggered_remainder_execution")
            elif name not in {"crash_reserved", "restore_then_later_fill", "duplicate_entry"}:
                raise ValueError("unimplemented_execution_scenario")
    checks.extend(invariants(sim))
    result = {
        "case_id": case.case_id,
        "seed": case.seed,
        "archetype": name,
        "status": "passed",
        "checks": checks,
        "mode": "synthetic_stress",
        "core_hash": digest(sim.checkpoint()),
        "state": sim.state(),
    }
    return result
