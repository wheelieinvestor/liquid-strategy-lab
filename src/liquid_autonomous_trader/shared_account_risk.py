"""Fresh all-owner admission projection over the single durable execution store.

Open positions remain fully reserved in ExecutionStore. Do not count them twice,
or turn an unknown/foreign position into owned exposure based on its ticker.
"""

from decimal import Decimal

from liquid_autonomous_trader.liquid_position_contract import verify_position
from liquid_autonomous_trader.live_policy import AccountRiskSnapshot
from liquid_autonomous_trader.local_accounting import AccountingError


def shared_risk_snapshot(projection, before, account, orders, *, journal, executions, now):
    if projection is None or projection.blockers:
        raise AccountingError("shared_accounting_unavailable")
    owners = {
        row["symbol"]: row["owner_intent"]
        for row in journal.db.execute(
            "SELECT symbol,owner_intent FROM liquid_market_owners WHERE account='research-account'"
        )
    }
    obligations = {row["intent_id"]: row for row in executions.unresolved()}
    if any(o["state"] != "acknowledged" or o["kind"] != "entry" for o in journal.unresolved()):
        raise AccountingError("shared_account_write_unresolved")
    position_symbols = {p.broker_symbol for p in account.positions}
    if len(position_symbols) != len(account.positions) or position_symbols != set(owners):
        raise AccountingError("shared_account_ownership_mismatch")
    if any(o.symbol not in owners for o in orders.orders):
        raise AccountingError("shared_account_unowned_working_order")
    actual_margin = Decimal(0)
    for p in account.positions:
        record = obligations.get(owners[p.broker_symbol])
        if (
            record is None
            or record["state"] != "open"
            or record["symbol"] != p.broker_symbol
            or record["side"] != p.side
            or Decimal(record["leverage"]) != p.leverage
            or p.margin_used_usd > Decimal(record["collateral"])
        ):
            raise AccountingError("shared_position_exceeds_owned_reservation")
        evidence = verify_position(
            before, orders, account, symbol=p.broker_symbol, expected_route=journal.route, now=now
        )
        if not evidence.protection_verified:
            raise AccountingError("shared_position_protection_unverified")
        actual_margin += p.margin_used_usd
    if actual_margin != account.margin_used_usd:
        raise AccountingError("shared_account_margin_unattributed")
    zero = Decimal(0)
    # Existing open risk, collateral and position counts enter atomically through
    # ExecutionStore.reserve(), which includes all open reservations. Actual
    # available collateral stays the provider value (conservative double reserve).
    return projection.apply(
        AccountRiskSnapshot(
            opening_equity=projection.opening_equity,
            current_equity=account.equity_usd,
            actual_available_collateral=account.available_collateral_usd,
            owned_open_collateral=zero,
            foreign_open_collateral=zero,
            pending_collateral=zero,
            unknown_collateral=zero,
            durable_reservations=zero,
            positions_by_strategy={},
            collateral_by_strategy={},
            realized_pnl=None,
            fees=None,
            funding=None,
            open_stop_risk=zero,
            pending_unknown_stop_risk=zero,
            durable_reserved_stop_risk=zero,
            flow_epoch_risk_including_costs=flow_session_risk(executions, now),
            reconciled=True,
            observed_at=account.received_at,
        )
    )


def flow_session_risk(executions, now):
    """Committed Flow entry risk never refunds within its NY cash-session day."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    day = now.astimezone(ZoneInfo("America/New_York")).date()
    total = Decimal(0)
    # Lifecycle's earliest reserved record survives later state updates.
    for row in executions.db.execute(
        "SELECT e.planned_loss,MIN(l.at) AS admitted_at FROM executions e "
        "JOIN lifecycle l ON l.intent_id=e.intent_id "
        "WHERE e.strategy='flow_show_mirror' AND l.state='reserved' GROUP BY e.intent_id"
    ):
        admitted = datetime.fromisoformat(row["admitted_at"])
        if admitted.astimezone(ZoneInfo("America/New_York")).date() == day:
            total += Decimal(row["planned_loss"])
    return total
