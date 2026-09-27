"""Deterministic live admission policy, independent of any broker transport.

All monetary values are USD.  Collateral is deliberately distinct from notional.
The caller must supply a fresh, authoritative all-account reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

from liquid_autonomous_trader.btc_mandate import BTC_MAX_PLANNED_LOSS_USD
from liquid_autonomous_trader.flow_mandate import FLOW_MAX_PLANNED_LOSS_USD
from liquid_autonomous_trader.xyz_mandate import XYZ_MAX_PRICE_STOP_LOSS_USD

ZERO = Decimal("0")
ACCOUNT_COLLATERAL_CAP = Decimal("500")
ACCOUNT_RESERVE = Decimal("150")
INITIAL_STOP_MULTIPLIER = Decimal("1.25")


class Strategy(StrEnum):
    BTC = "btc_momentum"
    XYZ = "xyz100_gex"
    FLOW = "flow_show_mirror"
    CRAMER = "inverse_cramer"


@dataclass(frozen=True)
class SleeveLimit:
    collateral: Decimal
    positions: int


LIMITS = {
    Strategy.BTC: SleeveLimit(Decimal("250"), 1),
    Strategy.XYZ: SleeveLimit(Decimal("250"), 1),
    Strategy.FLOW: SleeveLimit(Decimal("350"), 3),
    Strategy.CRAMER: SleeveLimit(Decimal("150"), 3),
}


# User mandate, 2026-09-11: independent planned loss per trade; no cumulative cap.
PRICE_STOP_LOSS_CAPS = {
    Strategy.BTC: BTC_MAX_PLANNED_LOSS_USD,
    Strategy.XYZ: XYZ_MAX_PRICE_STOP_LOSS_USD,
    Strategy.FLOW: FLOW_MAX_PLANNED_LOSS_USD,
    Strategy.CRAMER: Decimal("15"),
}


class PolicyViolation(ValueError):
    """A stable reason code for a rejected admission."""


@dataclass(frozen=True)
class AccountRiskSnapshot:
    """Fresh reconciled exposure. Every collateral component must be all-account."""

    opening_equity: Decimal
    current_equity: Decimal
    actual_available_collateral: Decimal
    owned_open_collateral: Decimal
    foreign_open_collateral: Decimal
    pending_collateral: Decimal
    unknown_collateral: Decimal
    durable_reservations: Decimal
    positions_by_strategy: dict[Strategy, int]
    collateral_by_strategy: dict[Strategy, Decimal]
    realized_pnl: Decimal | None
    fees: Decimal | None
    funding: Decimal | None
    open_stop_risk: Decimal
    pending_unknown_stop_risk: Decimal
    durable_reserved_stop_risk: Decimal
    flow_epoch_risk_including_costs: Decimal
    reconciled: bool
    observed_at: datetime
    accounted_loss_usd: Decimal | None = None
    accounting_basis: str = "provider_components"

    @property
    def obligated_collateral(self) -> Decimal:
        return (
            self.owned_open_collateral
            + self.foreign_open_collateral
            + self.pending_collateral
            + self.unknown_collateral
            + self.durable_reservations
        )


@dataclass(frozen=True)
class EntryRiskRequest:
    strategy: Strategy
    symbol: str
    side: str
    entry: Decimal
    proposed_stop: Decimal
    quantity_step: Decimal
    contract_multiplier: Decimal
    requested_notional: Decimal
    requested_collateral: Decimal
    stressed_cost: Decimal
    venue_max_leverage: Decimal
    selected_leverage: Decimal
    liquidation_distance_price: Decimal
    stressed_slippage_price: Decimal
    existing_position_stop: Decimal | None = None
    stop_price_step: Decimal | None = None
    minimum_notional_usd: Decimal = ZERO
    require_full_requested_size: bool = False


@dataclass(frozen=True)
class Admission:
    strategy: Strategy
    widened_initial_stop: Decimal
    quantity: Decimal
    admitted_notional: Decimal
    planned_loss_including_costs: Decimal
    reserved_collateral: Decimal
    stressed_cost: Decimal = ZERO
    policy_version: str = "live-collateral-500-price-risk-only-v5"


def _finite_nonnegative(name: str, value: Decimal) -> None:
    if not value.is_finite() or value < ZERO:
        raise PolicyViolation(f"invalid_{name}")


def widened_initial_stop(entry: Decimal, stop: Decimal, side: str) -> Decimal:
    for name, value in (("entry", entry), ("stop", stop)):
        _finite_nonnegative(name, value)
        if value == ZERO:
            raise PolicyViolation(f"invalid_{name}")
    if side == "long" and stop < entry:
        result = entry - (entry - stop) * INITIAL_STOP_MULTIPLIER
    elif side == "short" and stop > entry:
        result = entry + (stop - entry) * INITIAL_STOP_MULTIPLIER
    else:
        raise PolicyViolation("invalid_stop_geometry")
    if result <= ZERO:
        raise PolicyViolation("nonpositive_widened_stop")
    return result


def assess_entry(
    request: EntryRiskRequest,
    account: AccountRiskSnapshot,
    *,
    now: datetime,
    max_reconciliation_age_seconds: Decimal = Decimal("5"),
) -> Admission:
    """Size after widening, then enforce loss, collateral, venue and sleeve constraints."""

    if now.tzinfo is None or account.observed_at.tzinfo is None:
        raise PolicyViolation("aware_reconciliation_time_required")
    age_seconds = Decimal(
        str((now.astimezone(UTC) - account.observed_at.astimezone(UTC)).total_seconds())
    )
    if not account.reconciled or age_seconds < ZERO or age_seconds > max_reconciliation_age_seconds:
        raise PolicyViolation("fresh_reconciliation_required")
    numeric = {
        "opening_equity": account.opening_equity,
        "current_equity": account.current_equity,
        "available_collateral": account.actual_available_collateral,
        "owned_collateral": account.owned_open_collateral,
        "foreign_collateral": account.foreign_open_collateral,
        "pending_collateral": account.pending_collateral,
        "unknown_collateral": account.unknown_collateral,
        "reservations": account.durable_reservations,
        "open_stop_risk": account.open_stop_risk,
        "pending_unknown_stop_risk": account.pending_unknown_stop_risk,
        "reserved_stop_risk": account.durable_reserved_stop_risk,
        "flow_epoch_risk": account.flow_epoch_risk_including_costs,
    }
    numeric.update({f"sleeve_collateral_{k}": v for k, v in account.collateral_by_strategy.items()})
    for name, value in numeric.items():
        _finite_nonnegative(name, value)
    if account.accounting_basis == "local_conservative_cash_debits":
        if any(v is not None for v in (account.realized_pnl, account.fees, account.funding)):
            raise PolicyViolation("mixed_accounting_bases")
        if account.accounted_loss_usd is None:
            raise PolicyViolation("local_accounting_loss_required")
        _finite_nonnegative("accounted_loss", account.accounted_loss_usd)
    elif account.accounting_basis == "provider_components":
        if account.accounted_loss_usd is not None:
            raise PolicyViolation("mixed_accounting_bases")
        if any(
            v is None or not v.is_finite()
            for v in (account.realized_pnl, account.fees, account.funding)
        ):
            raise PolicyViolation("invalid_account_pnl")
        _finite_nonnegative("fees", account.fees)
    else:
        raise PolicyViolation("unsupported_accounting_basis")
    if account.opening_equity <= ZERO or account.current_equity <= ZERO:
        raise PolicyViolation("positive_equity_required")
    if any(count < 0 for count in account.positions_by_strategy.values()):
        raise PolicyViolation("invalid_position_count")

    values = (
        request.entry,
        request.proposed_stop,
        request.quantity_step,
        request.contract_multiplier,
        request.requested_notional,
        request.requested_collateral,
        request.venue_max_leverage,
        request.selected_leverage,
        request.liquidation_distance_price,
    )
    if not request.symbol.strip():
        raise PolicyViolation("symbol_required")
    if any(not value.is_finite() or value <= ZERO for value in values):
        raise PolicyViolation("invalid_request_value")
    _finite_nonnegative("stressed_cost", request.stressed_cost)
    _finite_nonnegative("stressed_slippage_price", request.stressed_slippage_price)
    if request.strategy is Strategy.CRAMER and (
        request.selected_leverage != Decimal(10)
        or request.requested_notional > Decimal(500)
        or not request.require_full_requested_size
    ):
        raise PolicyViolation("cramer_fixed_size_mandate_required")
    if request.selected_leverage > request.venue_max_leverage:
        raise PolicyViolation("venue_leverage_exceeded")
    if request.existing_position_stop is not None:
        if (
            (
                request.side == "long"
                and not (
                    request.proposed_stop < request.entry
                    and request.existing_position_stop < request.entry
                )
            )
            or (
                request.side == "short"
                and not (
                    request.proposed_stop > request.entry
                    and request.existing_position_stop > request.entry
                )
            )
            or request.side not in {"long", "short"}
        ):
            raise PolicyViolation("invalid_stop_geometry")
        old_distance = abs(request.entry - request.existing_position_stop)
        if abs(request.entry - request.proposed_stop) > old_distance:
            raise PolicyViolation("existing_stop_widening_forbidden")
        stop = request.proposed_stop
    else:
        stop = widened_initial_stop(request.entry, request.proposed_stop, request.side)
    if request.stop_price_step is not None:
        if not request.stop_price_step.is_finite() or request.stop_price_step <= ZERO:
            raise PolicyViolation("invalid_stop_price_step")
        stop = (stop / request.stop_price_step).to_integral_value(
            rounding=ROUND_FLOOR if request.side == "long" else ROUND_CEILING
        ) * request.stop_price_step
        if stop <= ZERO:
            raise PolicyViolation("nonpositive_widened_stop")
        if request.existing_position_stop is not None and (
            (request.side == "long" and stop < request.existing_position_stop)
            or (request.side == "short" and stop > request.existing_position_stop)
        ):
            raise PolicyViolation("existing_stop_widening_forbidden")
    unit_notional = request.entry * request.contract_multiplier
    unit_price_risk = abs(request.entry - stop) * request.contract_multiplier
    stop_distance = abs(request.entry - stop)
    minimum_liquidation_distance = stop_distance * Decimal("3")
    if (
        not (request.strategy is Strategy.BTC and request.symbol == "BTC")
        and request.liquidation_distance_price < minimum_liquidation_distance
    ):
        raise PolicyViolation("liquidation_buffer_insufficient")
    limit = LIMITS[request.strategy]
    price_capacity = PRICE_STOP_LOSS_CAPS[request.strategy]

    quantity = min(
        request.requested_notional / unit_notional,
        price_capacity / unit_price_risk,
    )
    quantity = (quantity / request.quantity_step).to_integral_value(
        rounding=ROUND_FLOOR
    ) * request.quantity_step
    if quantity <= ZERO:
        raise PolicyViolation("sized_quantity_zero")
    requested_quantity = (
        request.requested_notional / unit_notional / request.quantity_step
    ).to_integral_value(rounding=ROUND_FLOOR) * request.quantity_step
    if request.require_full_requested_size and quantity < requested_quantity:
        raise PolicyViolation("requested_size_exceeds_loss_budget")
    admitted_notional = quantity * unit_notional
    _finite_nonnegative("minimum_notional", request.minimum_notional_usd)
    if admitted_notional < request.minimum_notional_usd:
        raise PolicyViolation("venue_minimum_notional_not_met")
    planned_loss = quantity * unit_price_risk + request.stressed_cost
    if planned_loss - request.stressed_cost > price_capacity:
        raise PolicyViolation("per_trade_risk_exceeded")

    minimum_requested_collateral = request.requested_notional / request.selected_leverage
    if request.requested_collateral < minimum_requested_collateral:
        raise PolicyViolation("requested_collateral_below_leverage_minimum")
    proportional_collateral = (
        request.requested_collateral * admitted_notional / request.requested_notional
    )
    minimum_collateral = admitted_notional / request.selected_leverage
    collateral = max(proportional_collateral, minimum_collateral)
    collateral = collateral.quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    free_by_cap = ACCOUNT_COLLATERAL_CAP - account.obligated_collateral
    available = min(
        free_by_cap,
        account.actual_available_collateral - account.durable_reservations,
    )
    sleeve_used = account.collateral_by_strategy.get(request.strategy, ZERO)
    if collateral > available:
        raise PolicyViolation("aggregate_or_available_collateral_exceeded")
    if sleeve_used + collateral > limit.collateral:
        raise PolicyViolation("sleeve_collateral_exceeded")
    if account.positions_by_strategy.get(request.strategy, 0) >= limit.positions:
        raise PolicyViolation("sleeve_position_limit")
    if account.current_equity - account.obligated_collateral - collateral < ACCOUNT_RESERVE:
        raise PolicyViolation("account_reserve_breached")
    return Admission(
        strategy=request.strategy,
        widened_initial_stop=stop,
        quantity=quantity,
        admitted_notional=admitted_notional,
        planned_loss_including_costs=planned_loss,
        reserved_collateral=collateral,
        stressed_cost=request.stressed_cost,
    )


def reserved_price_loss_limit(
    strategy: Strategy, planned_loss: Decimal, stressed_cost: Decimal
) -> Decimal:
    """Validate durable risk before dispatch; keep estimated costs outside each price cap."""
    _finite_nonnegative("planned_loss", planned_loss)
    _finite_nonnegative("stressed_cost", stressed_cost)
    if stressed_cost > planned_loss:
        raise PolicyViolation("invalid_reserved_cost")
    price_loss = planned_loss - stressed_cost
    if price_loss > PRICE_STOP_LOSS_CAPS[strategy]:
        raise PolicyViolation("per_trade_risk_exceeded")
    return PRICE_STOP_LOSS_CAPS[strategy]
