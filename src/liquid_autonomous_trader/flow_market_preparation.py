"""Exact-market Flow price/cost preparation; no broker execution authority.

Published fee ceilings are planning reserves, not the account's actual fee tier.
An unpublished Computer minimum is provider-enforced under the September 9 mandate.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from liquid_autonomous_trader.flow_mandate import (
    FLOW_FUNDING_RESERVE_USD,
    FLOW_MAX_ORDER_NOTIONAL_USD,
    FLOW_MAX_PLANNED_LOSS_USD,
)
from liquid_autonomous_trader.frozen.flow_mirror import FLOW_MIRROR_V1_ALLOWED_SYMBOLS
from liquid_autonomous_trader.native_perp_source import NativeQuote


@dataclass(frozen=True)
class ComputerMinimumEvidence:
    minimum_collateral_usd: Decimal
    market_id: str
    observed_at: datetime
    source: str
    evidence_sha256: str


@dataclass(frozen=True)
class FlowMarketPreparation:
    market_id: str
    side: str
    entry_reference: Decimal
    submitted_notional: Decimal
    quantity: Decimal
    quantity_step: Decimal
    leverage: Decimal
    initial_margin: Decimal
    widened_stop: Decimal
    fee_rate_ceiling_each_side: Decimal
    planned_roundtrip_fees: Decimal
    planned_roundtrip_slippage: Decimal
    price_stop_risk: Decimal
    funding_reserve: Decimal
    planned_loss: Decimal
    liquidation_distance: Decimal
    required_liquidation_distance: Decimal
    reasons: tuple[str, ...]
    fee_basis: str = "published_undiscounted_ceiling_not_account_fee_tier"
    execution_ready: bool = False


def prepare_flow_market(quote: NativeQuote, *, side, now, minimum=None):
    if quote.identity.ticker not in FLOW_MIRROR_V1_ALLOWED_SYMBOLS:
        raise ValueError("flow_exact_ticker_required")
    if side not in {"long", "short"}:
        raise ValueError("flow_direction_required")
    if now.tzinfo is None or not 0 <= (now - quote.observed_at).total_seconds() <= 5:
        raise ValueError("flow_native_quote_stale")
    reasons = []
    if quote.maximum_leverage < 10:
        reasons.append("ten_x_not_supported")
    # Use the executable side, not an untimestamped Liquid mark relabeled as a quote.
    entry = quote.ask if side == "long" else quote.bid
    maximum = FLOW_MAX_ORDER_NOTIONAL_USD
    quantity = (maximum / entry / quote.quantity_step).to_integral_value(
        rounding=ROUND_FLOOR
    ) * quote.quantity_step
    notional = quantity * entry
    if quantity <= 0 or notional < 10:
        reasons.append("venue_minimum_notional_not_met")
    if min(quote.bid_notional, quote.ask_notional) < maximum * 5:
        reasons.append("insufficient_market_depth")
    margin = notional / 10
    if minimum is not None:
        import re

        if (
            minimum.market_id != quote.identity.coin
            or minimum.source != "liquid_computer"
            or minimum.observed_at.tzinfo is None
            or not 0 <= (now - minimum.observed_at).total_seconds() <= 300
            or not re.fullmatch(r"[a-f0-9]{64}", minimum.evidence_sha256)
            or not minimum.minimum_collateral_usd.is_finite()
            or minimum.minimum_collateral_usd <= 0
        ):
            raise ValueError("computer_minimum_evidence_invalid")
        if margin < minimum.minimum_collateral_usd:
            reasons.append("computer_minimum_exceeds_approved_order_cap")
    # TP1 rounds down to native units at execution time, or closes fully if
    # either leg is below the venue minimum. Odd entry lots are admissible.
    # Hyperliquid's published highest taker base, HIP-3 scale, and Liquid's
    # additional five bps. Ignore discounts including growth mode: conservative
    # reserved costs, explicitly not fabricated account-specific charged fees.
    scale = quote.deployer_fee_scale
    fee_scale = 1 + scale if scale < 1 else 2 * scale
    fee_rate = Decimal("0.00045") * fee_scale + Decimal("0.0005")
    fees = notional * fee_rate * 2
    slip_price = max(quote.ask - quote.bid, entry * Decimal("0.001"))
    slippage = quantity * slip_price * 2
    funding = FLOW_FUNDING_RESERVE_USD
    # -15% leveraged ROI at 10x, then the mandate's 1.25 initial widening.
    distance = entry * Decimal("0.015") * Decimal("1.25")
    stop = entry - distance if side == "long" else entry + distance
    # Hyperliquid permits integer prices regardless of significant-digit limits.
    # Use the venue decimal-place grid and round outward; validate significant
    # digits too by coarsening to the stricter of five figures or that grid.
    tick = max(Decimal(1).scaleb(-6) / quote.quantity_step, Decimal(1).scaleb(stop.adjusted() - 4))
    stop = (stop / tick).to_integral_value(
        rounding=ROUND_FLOOR if side == "long" else ROUND_CEILING
    ) * tick
    price_risk = quantity * abs(entry - stop)
    costs = fees + slippage + funding
    planned = price_risk + costs
    if price_risk > FLOW_MAX_PLANNED_LOSS_USD:
        reasons.append("flow_planned_loss_exceeds_limit")
    maintenance = 1 / (2 * quote.maximum_leverage)
    liquidation = entry * (Decimal("0.1") - maintenance) / (1 + maintenance)
    required = 3 * abs(entry - stop)
    if liquidation < required:
        reasons.append("liquidation_buffer_insufficient")
    return FlowMarketPreparation(
        quote.identity.coin,
        side,
        entry,
        notional,
        quantity,
        quote.quantity_step,
        Decimal(10),
        margin,
        stop,
        fee_rate,
        fees,
        slippage,
        price_risk,
        funding,
        planned,
        liquidation,
        required,
        tuple(reasons),
    )
