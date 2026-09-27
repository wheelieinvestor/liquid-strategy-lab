from dataclasses import replace
from decimal import Decimal as D

import pytest

from liquid_autonomous_trader.backtesting.admission import account_snapshot, admit, utc
from liquid_autonomous_trader.backtesting.execution import SimulatedExecution
from liquid_autonomous_trader.backtesting.ledger import FeeSchedule, Instrument, Ledger, Tier
from liquid_autonomous_trader.live_policy import EntryRiskRequest, Strategy, assess_entry


def engine(cash="1000", adverse=True):
    spec = Instrument(
        "BTC", D(".01"), D(".01"), D(10), (Tier(D(0), D(40)),), "fixture", "synthetic_assumption"
    )
    fees = FeeSchedule(
        "fixture",
        "research",
        "assumed",
        "none",
        0,
        10**20,
        D(".001"),
        D("-.0001"),
        "synthetic_assumption",
    )
    return SimulatedExecution(Ledger(D(cash), {"BTC": spec}), fees, adverse_intrabar=adverse)


def request():
    return EntryRiskRequest(
        Strategy.BTC,
        "BTC",
        "long",
        D(100),
        D(98),
        D(".01"),
        D(1),
        D(200),
        D(20),
        D(0),
        D(40),
        D(10),
        D(10),
        D(0),
        stop_price_step=D(".01"),
        minimum_notional_usd=D(10),
        require_full_requested_size=True,
    )


def submit(sim, key="one", at=1, **kwargs):
    return sim.submit(key, request(), at, mode="cross", expires_us=1000, **kwargs)


def quote(sim, at, *, bid="99.99", ask="100.01", depth="10"):
    sim.quote("BTC", at, bids=[(bid, depth)], asks=[(ask, depth)])


def test_production_admission_parity_and_shared_reservation_capacity():
    sim = engine()
    expected = assess_entry(request(), account_snapshot(sim.ledger, 1), now=utc(1))
    assert admit(sim.ledger, request(), 1) == expected
    assert submit(sim) == "accepted"
    assert submit(sim, "second") == "market_reserved"
    assert submit(sim) == "duplicate"
    quote(sim, 1)
    assert sim.ledger.positions == {}  # same observation cannot fill its own signal
    quote(sim, 2)
    assert sim.ledger.positions["BTC"].quantity == 2
    assert sim.ledger.positions["BTC"].stop == D("97.50")  # production 1.25 widening
    assert sim.orders["one"].state == "filled"


def test_partial_depth_cancel_latency_and_fee_per_fill():
    sim = engine()
    assert submit(sim) == "accepted"
    quote(sim, 2, depth=".5")
    assert sim.orders["one"].remaining == D("1.5")
    assert sim.ledger.fees == D(".050005")
    sim.cancel("one", 2, latency_us=3)
    quote(sim, 3, depth=".5")  # still executable until cancellation acknowledgment
    quote(sim, 5)
    assert sim.ledger.positions["BTC"].quantity == 1
    assert sim.orders["one"].state == "cancelled"
    assert sim.ledger.reservations == {}
    assert sim.ledger.fees == D(".100010")


def test_unknown_retains_reservation_until_positive_reconciliation():
    sim = engine()
    assert submit(sim, fault="unknown") == "unknown"
    quote(sim, 20)
    sim.cancel("one", 20)
    assert sim.ledger.positions == {} and sim.ledger.reservations
    sim.reconcile_unknown("one", 21, terminal_no_fill=True)
    assert sim.ledger.reservations == {}
    assert submit(sim, fault="unknown") == "duplicate"
    assert any(e["kind"] == "cancel_blocked_unknown" for e in sim.events)


def test_stop_replacement_latency_keeps_old_stop_until_ack_and_survives_restart():
    sim = engine()
    submit(sim)
    quote(sim, 2)
    assert sim.manage("replacement", "one", "stop", 2, stop="99", latency_us=5) == "pending"
    quote(sim, 5, bid="98.49", ask="98.51")
    assert sim.ledger.positions["BTC"].stop == D("97.50")
    restarted = SimulatedExecution.restore(sim.checkpoint())
    quote(restarted, 7)
    assert restarted.ledger.positions["BTC"].stop == 99
    quote(restarted, 8, bid="98.49", ask="98.51")
    assert not restarted.ledger.positions
    assert restarted.management["replacement"]["state"] == "applied"


def test_native_stop_precedes_delayed_replacement_and_does_not_reopen():
    sim = engine()
    submit(sim)
    quote(sim, 2)
    sim.manage("replacement", "one", "stop", 2, stop="99", latency_us=5)
    quote(sim, 7, bid="96.99", ask="97.01")
    assert not sim.ledger.positions
    assert sim.management["replacement"]["state"] == "flat_reconciled"
    assert len([e for e in sim.events if e["kind"] == "exit"]) == 1


def test_software_close_partial_depth_and_unknown_write_recovery():
    sim = engine()
    submit(sim)
    quote(sim, 2)
    assert sim.manage("close", "one", "close", 2, fault="unknown") == "unknown"
    quote(sim, 3)
    assert sim.ledger.positions["BTC"].quantity == 2
    assert submit(sim, "blocked", at=3) == "prior_management_outcome_unresolved"
    with pytest.raises(ValueError, match="not_observed"):
        sim.reconcile_management("close", 3, applied=True)
    sim.reconcile_management("close", 3, known_pending=True)
    quote(sim, 4, depth=".5")
    assert sim.ledger.positions["BTC"].quantity == D("1.5")
    assert sim.management["close"]["state"] == "pending"
    quote(sim, 5, depth="2")
    assert not sim.ledger.positions
    assert sim.management["close"]["state"] == "applied"
    assert sim.ledger.fees == D(".4")  # 2*100.01 entry + 2*99.99 exit, .1% each


def test_unknown_reduction_requires_observed_quantity_and_cannot_repeat_after_restore():
    sim = engine()
    submit(sim)
    quote(sim, 2)
    sim.manage("reduce", "one", "reduce", 2, quantity=".5", fault="unknown")
    with pytest.raises(ValueError, match="invalid_management_reconciliation"):
        sim.reconcile_management("reduce", 3)
    with pytest.raises(ValueError, match="reduction_application_not_observed"):
        sim.reconcile_management("reduce", 3, applied=True)
    # Independent simulated venue fill is applied before lost-ack reconciliation.
    sim._close("BTC", D("99.99"), 3, "injected-native-reduce", D(".5"))
    with pytest.raises(ValueError, match="unknown_partial_or_intervening_activity"):
        sim.reconcile_management("reduce", 3, known_pending=True)
    sim.reconcile_management("reduce", 3, applied=True)
    assert sim.management["reduce"]["remaining"] == "0"
    restored = SimulatedExecution.restore(sim.checkpoint())
    quote(restored, 4)
    assert restored.ledger.positions["BTC"].quantity == D("1.5")
    assert restored.management["reduce"]["state"] == "applied"


def test_touched_limit_does_not_claim_queue_fill_and_expiry_is_terminal():
    sim = engine()
    submit(sim, kind="limit", limit="100.01")
    quote(sim, 2)
    assert sim.ledger.positions == {}
    quote(sim, 1001)
    assert sim.orders["one"].state == "expired"
    assert sim.ledger.reservations == {}


def test_stop_gap_and_ambiguous_bar_default_adverse_order():
    sim = engine()
    submit(sim, target="105")
    quote(sim, 2)
    sim.bar("BTC", 3, 10, open_price="100", high="106", low="94", close="100")
    assert sim.ledger.positions == {}
    assert sim.ledger.realized == D("-5.02")
    assert any(e["kind"] == "intrabar_ambiguity" for e in sim.events)
    optimistic = engine(adverse=False)
    submit(optimistic, target="105")
    quote(optimistic, 2)
    optimistic.bar("BTC", 3, 10, open_price="100", high="106", low="94", close="100")
    assert optimistic.ledger.realized == D("9.98")
    gap = engine()
    submit(gap)
    quote(gap, 2)
    gap.bar("BTC", 3, 10, open_price="90", high="92", low="89", close="91")
    assert gap.ledger.realized == D("-20.02")  # gap exceeds admitted $5 stop risk


def test_late_stop_cannot_execute_in_earlier_part_of_bar():
    sim = engine()
    submit(sim)
    quote(sim, 5)
    sim.bar("BTC", 2, 10, open_price="100", high="101", low="90", close="100")
    assert "BTC" in sim.ledger.positions


def test_native_stop_survives_halt_and_consumes_depth_across_levels():
    sim = engine()
    submit(sim)
    quote(sim, 2)
    sim.halted = True
    sim.quote("BTC", 3, bids=[("95", ".5"), ("94", ".5")], asks=[("96", "10")])
    assert sim.ledger.positions["BTC"].quantity == 1
    # Already triggered remainder exits even after rebound, without fresh model/entry.
    quote(sim, 4, bid="99", ask="100", depth="1")
    assert sim.ledger.positions == {}
    assert sim.ledger.realized == D("-6.520")  # .5*(-5.01) + .5*(-6.01) + 1*(-1.01)


def test_failed_admission_and_conflicting_duplicate_are_retained():
    sim = engine(cash="160")
    assert submit(sim) == "account_reserve_breached"
    assert sim.decisions[0]["reason"] == "account_reserve_breached"
    sim = engine()
    submit(sim)
    with pytest.raises(ValueError, match="identity_conflict"):
        sim.submit(
            "one", replace(request(), requested_notional=D(300)), 1, mode="cross", expires_us=1000
        )


def test_restart_after_partial_fill_is_idempotent_and_matches_uninterrupted(tmp_path):
    import json

    sim = engine()
    submit(sim)
    quote(sim, 2, depth=".5")
    before = sim.state()
    quote(sim, 2, depth=".5")  # same depth snapshot cannot be consumed twice
    assert sim.state() == before
    checkpoint = tmp_path / "checkpoint.json"
    sim.save(checkpoint)
    restored = SimulatedExecution.restore(json.loads(checkpoint.read_text()))
    quote(restored, 2, depth=".5")
    assert restored.state() == before
    quote(sim, 3, depth="1.5")
    quote(restored, 3, depth="1.5")
    assert restored.state() == sim.state()
    assert restored.ledger.journal == sim.ledger.journal
    assert submit(restored) == "duplicate"
    with pytest.raises(ValueError, match="conflicting_quote"):
        quote(restored, 3, depth="2")


def test_corrupt_checkpoint_rejected_and_unknown_outcome_survives_restore():
    import copy

    sim = engine()
    submit(sim, fault="unknown")
    checkpoint = sim.checkpoint()
    restored = SimulatedExecution.restore(copy.deepcopy(checkpoint))
    quote(restored, 20)
    assert restored.orders["one"].state == "unknown"
    assert restored.ledger.reservations["one"]["state"] == "unknown"
    restored.reconcile_unknown("one", 21, terminal_no_fill=False)
    quote(restored, 22)
    assert restored.orders["one"].state == "filled"
    assert (
        Ledger.replay(
            restored.ledger.initial_cash, restored.ledger.instruments, restored.ledger.journal
        ).state()
        == restored.ledger.state()
    )
    checkpoint["payload"]["initial_cash"] = "2000"
    with pytest.raises(ValueError, match="checksum"):
        SimulatedExecution.restore(checkpoint)
