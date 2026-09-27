"""Liquid's September 8 net-position contract; no order or fill synthesis."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from liquid_autonomous_trader.computer_adapter import (
    ComputerContractError,
    ComputerOpenOrders,
    ComputerPortfolio,
    ComputerPosition,
    ComputerRoute,
    require_account_binding,
)

CONTRACT_REVISION = "liquid-team-trigger-confirmation-20260908"


@dataclass(frozen=True)
class PositionEvidence:
    symbol: str
    position: ComputerPosition | None
    stop_order_id: str | None
    protected_quantity: Decimal
    working_order_ids: tuple[str, ...]
    observed_at: datetime
    protection_verified: bool
    reasons: tuple[str, ...]
    quantity_basis: str = "current_net_position_not_cumulative_fills"


def verify_position(
    before: ComputerPortfolio,
    orders: ComputerOpenOrders,
    after: ComputerPortfolio,
    *,
    symbol: str,
    expected_route: ComputerRoute,
    now: datetime,
    expected_stop: Decimal | None = None,
) -> PositionEvidence:
    """Bracket an order read with account-bound, stable position reads.

    Stability is a sampled observation, not a transactional broker guarantee.
    All reads must use the same authenticated client. The order listing is the
    complete working set per Liquid's reply; it is never execution history.
    """
    for portfolio in (before, after):
        require_account_binding(
            portfolio, expected_username="research-account", expected_route=expected_route, now=now
        )
    if not (
        before.received_at <= orders.received_at <= after.received_at <= now
        and 0 <= (now - orders.received_at).total_seconds() <= 5
        and orders.working_snapshot_complete
    ):
        raise ComputerContractError("position_read_sequence_invalid")
    old = tuple(p for p in before.positions if p.broker_symbol == symbol)
    current = tuple(p for p in after.positions if p.broker_symbol == symbol)
    if len(old) > 1 or len(current) > 1:
        raise ComputerContractError("duplicate_net_position")

    # Mark, unrealized PnL and reported margin can move between reads without
    # changing the net position or its stop coverage. Admission independently
    # checks fresh actual margin against owned reservations and account limits.
    def identity(items):
        return tuple((p.side, p.quantity, p.entry_price, p.leverage, p.stop_price) for p in items)

    if identity(old) != identity(current):
        raise ComputerContractError("position_changed_during_reconciliation")
    working = tuple(o for o in orders.orders if o.symbol == symbol)
    ids = tuple(o.order_id for o in working)
    if not current:
        return PositionEvidence(
            symbol,
            None,
            None,
            Decimal(0),
            ids,
            now,
            False,
            ("flat_with_working_orders",) if working else ("flat_observed",),
        )
    p = current[0]
    # User relayed Liquid's confirmation that its generic trigger representation
    # manages SL. Accept only the trigger paired with authoritative portfolio.sl;
    # do not globally relabel generic conditional orders as protective stops.
    stops = tuple(
        o
        for o in working
        if (
            o.kind == "sl"
            and (not o.venue_verified_fixed_stop or o.trigger_or_limit_price == p.stop_price)
        )
        or (
            o.kind == "trigger"
            and p.stop_price is not None
            and o.trigger_or_limit_price == p.stop_price
            and (o.position_level or o.venue_verified_fixed_stop)
            and o.reduce_only
        )
    )
    reasons = []
    stop_id = None
    if len(stops) != 1:
        reasons.append("exactly_one_stop_required")
    else:
        stop = stops[0]
        stop_id = stop.order_id
        if not (stop.position_level or stop.venue_verified_fixed_stop) or not stop.reduce_only:
            reasons.append("stop_flags_invalid")
        if stop.side != ("sell" if p.side == "long" else "buy"):
            reasons.append("stop_closing_side_invalid")
        if p.stop_price != stop.trigger_or_limit_price:
            reasons.append("position_and_order_stop_disagree")
        if expected_stop is not None and stop.trigger_or_limit_price != expected_stop:
            reasons.append("stop_price_not_requested_value")
        if stop.quantity != 0 and stop.quantity < p.quantity:
            reasons.append("stop_quantity_insufficient")
        if (p.side == "long" and stop.trigger_or_limit_price >= p.mark_price) or (
            p.side == "short" and stop.trigger_or_limit_price <= p.mark_price
        ):
            reasons.append("stop_already_crossed")
    if any(
        (o.kind not in {"sl", "tp"} and o not in stops and not o.venue_verified_fixed_stop)
        or not o.reduce_only
        for o in working
    ):
        reasons.append("unresolved_or_exposure_increasing_order")
    return PositionEvidence(
        symbol,
        p,
        stop_id,
        p.quantity if not reasons else Decimal(0),
        ids,
        now,
        not reasons,
        tuple(reasons),
    )
