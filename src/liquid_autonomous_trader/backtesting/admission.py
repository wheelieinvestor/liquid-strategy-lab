"""Research account projection into the unmodified production admission policy."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal as D
from types import SimpleNamespace

from liquid_autonomous_trader.backtesting.ledger import ZERO, Ledger
from liquid_autonomous_trader.btc_risk_provider import BtcRiskProvider
from liquid_autonomous_trader.live_policy import (
    AccountRiskSnapshot,
    EntryRiskRequest,
    Strategy,
    assess_entry,
)


def utc(at_us: int):
    return datetime.fromtimestamp(at_us / 1_000_000, UTC)


def account_snapshot(ledger: Ledger, at_us: int) -> AccountRiskSnapshot:
    counts, sleeves = {}, {}
    owned = foreign = ZERO
    for p in ledger.positions.values():
        collateral = ledger.margin(p) if p.mode == "cross" else max(p.isolated_cash, ZERO)
        try:
            strategy = Strategy(p.owner)
        except ValueError:
            foreign += collateral
            continue
        owned += collateral
        counts[strategy] = counts.get(strategy, 0) + 1
        sleeves[strategy] = sleeves.get(strategy, ZERO) + collateral
    pending = unknown = ZERO
    for r in ledger.reservations.values():
        if r["state"] == "unknown":
            unknown += r["collateral"]
        else:
            pending += r["collateral"]
        strategy = Strategy(r["owner"])
        counts[strategy] = counts.get(strategy, 0) + 1
        sleeves[strategy] = sleeves.get(strategy, ZERO) + r["collateral"]
    return AccountRiskSnapshot(
        opening_equity=ledger.initial_cash,
        current_equity=ledger.equity(),
        actual_available_collateral=max(ZERO, ledger.available()),
        owned_open_collateral=owned,
        foreign_open_collateral=foreign,
        pending_collateral=pending,
        unknown_collateral=unknown,
        durable_reservations=ZERO,
        positions_by_strategy=counts,
        collateral_by_strategy=sleeves,
        realized_pnl=ledger.realized,
        fees=max(ZERO, ledger.fees),
        funding=ledger.funding,
        open_stop_risk=sum(
            (
                abs(p.quantity) * abs(p.entry - p.stop)
                for p in ledger.positions.values()
                if p.stop is not None
            ),
            ZERO,
        ),
        pending_unknown_stop_risk=ZERO,
        durable_reserved_stop_risk=ZERO,
        flow_epoch_risk_including_costs=ZERO,
        reconciled=True,
        observed_at=utc(at_us),
    )


def admit(ledger: Ledger, request: EntryRiskRequest, now_us: int):
    if request.symbol in ledger.positions:
        raise ValueError("market_owned")
    if any(r["symbol"] == request.symbol for r in ledger.reservations.values()):
        raise ValueError("market_reserved")
    if any(r["state"] == "unknown" for r in ledger.reservations.values()):
        raise ValueError("prior_write_outcome_unresolved")
    return assess_entry(
        request,
        account_snapshot(ledger, now_us),
        now=utc(now_us),
        max_reconciliation_age_seconds=D(15),
    )


def btc_request(ledger: Ledger, observation, signal, now_us: int):
    """Use the actual provider's sizing/precision/cost planning, with no live reads.

    Metadata is supplied by the run manifest. Its historic validity is a separately
    reported fidelity limitation; this does not pretend it came from a live account.
    """

    def no_network(_):
        raise RuntimeError("research_network_forbidden")

    snapshot = account_snapshot(ledger, now_us)
    projection = SimpleNamespace(flat_risk_snapshot=lambda *_: snapshot)
    provider = BtcRiskProvider(
        SimpleNamespace(accounting_projection=projection),
        fetch=no_network,
        clock=lambda: utc(now_us),
    )
    spec = ledger.instruments["BTC"]
    provider.metadata = (spec.quantity_step, spec.tiers[0].maximum_leverage)
    provider.metadata_at = utc(now_us)
    request, _, _ = provider(
        observation,
        signal,
        SimpleNamespace(route=SimpleNamespace(value="paper")),
        SimpleNamespace(working_snapshot_complete=True),
        None,
    )
    return request
