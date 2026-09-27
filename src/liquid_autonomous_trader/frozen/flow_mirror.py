"""Deterministic Flow Show mirror strategy contracts and state transitions.

This module can produce replay, shadow, and paper-simulation plans. It contains no
broker transport and grants no order authority.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from liquid_autonomous_trader.frozen.flow_mirror_calendar import flow_mirror_risk_epoch
from liquid_autonomous_trader.frozen.models import SignalDirection

FLOW_MIRROR_STRATEGY_ID = "flow_show_mirror"
FLOW_MIRROR_VERSION = "flow-show-mirror-v1"
FLOW_MIRROR_V1_ALLOWED_SYMBOLS = frozenset(
    {
        "AAPL",
        "AMD",
        "AMZN",
        "AVGO",
        "COIN",
        "CRWV",
        "DELL",
        "GOOGL",
        "HOOD",
        "INTC",
        "LITE",
        "META",
        "MRVL",
        "MSFT",
        "MSTR",
        "MU",
        "NBIS",
        "NFLX",
        "NOW",
        "NVDA",
        "ORCL",
        "PLTR",
        "SKHY",
        "SNDK",
        "TSLA",
    }
)


class FlowMirrorDecision(StrEnum):
    ENTER = "enter"
    SKIP = "skip"


class FlowMirrorExitAction(StrEnum):
    HOLD = "hold"
    REDUCE_TP1 = "reduce_tp1"
    REPLACE_STOP = "replace_stop"
    CLOSE_FULL = "close_full"
    HALT = "halt"


class FlowMirrorPositionStage(StrEnum):
    INITIAL = "initial"
    TP1_PENDING = "tp1_pending"
    RUNNER = "runner"
    EXIT_PENDING = "exit_pending"
    HALTED = "halted"
    CLOSED = "closed"


class FlowMirrorConfigV1(BaseModel):
    """The approved baseline configuration; live authority is configured elsewhere."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = FLOW_MIRROR_VERSION
    source_view: str = "flow_show_deliveries_v1"
    discord_channel_id: str = "1503488321236631612"
    scoring_version: str = "v1.0.14-confirmed-directional"
    allowed_symbols: frozenset[str]
    signal_ttl_seconds: int = Field(default=300, gt=0)
    quote_max_age_seconds: int = Field(default=5, gt=0)
    fee_max_age_seconds: int = Field(default=300, gt=0)
    account_state_max_age_seconds: int = Field(default=5, gt=0)
    leverage: Decimal = Field(default=Decimal("10"), gt=0, le=10)
    sleeve_allocation_usd: Decimal = Field(default=Decimal("1000"), gt=0)
    sleeve_margin_cap_fraction: Decimal = Field(default=Decimal("0.90"), gt=0, le=1)
    max_concurrent_positions: int = Field(default=3, gt=0)
    max_planned_loss_per_trade_usd: Decimal = Field(default=Decimal("50"), gt=0)
    daily_loss_cap_usd: Decimal = Field(default=Decimal("150"), gt=0)
    hard_stop_nav_usd: Decimal = Field(default=Decimal("800"), ge=0)
    initial_stop_roi_pct: Decimal = Field(default=Decimal("-15"), lt=0)
    tp1_roi_pct: Decimal = Field(default=Decimal("25"), gt=0)
    tp1_fraction: Decimal = Field(default=Decimal("0.50"), gt=0, lt=1)
    trail_activation_roi_pct: Decimal = Field(default=Decimal("35"), gt=0)
    first_trail_stop_roi_pct: Decimal = Field(default=Decimal("10"), ge=0)
    trail_step_roi_pct: Decimal = Field(default=Decimal("10"), gt=0)
    slippage_reserve_bps_each_side: Decimal = Field(default=Decimal("10"), ge=0)
    funding_reserve_fraction: Decimal = Field(default=Decimal("0.10"), ge=0, le=1)
    maximum_funding_reserve_usd: Decimal = Field(default=Decimal("5"), ge=0)
    once_per_symbol_per_risk_epoch: bool = True

    @field_validator("allowed_symbols")
    @classmethod
    def normalize_symbols(cls, value: frozenset[str]) -> frozenset[str]:
        normalized = frozenset(symbol.strip().upper() for symbol in value if symbol.strip())
        if not normalized:
            raise ValueError("Flow Mirror requires a non-empty exact-symbol allowlist")
        return normalized

    @model_validator(mode="after")
    def validate_strategy_shape(self) -> Self:
        frozen_values: dict[str, object] = {
            "version": FLOW_MIRROR_VERSION,
            "source_view": "flow_show_deliveries_v1",
            "discord_channel_id": "1503488321236631612",
            "scoring_version": "v1.0.14-confirmed-directional",
            "allowed_symbols": FLOW_MIRROR_V1_ALLOWED_SYMBOLS,
            "signal_ttl_seconds": 300,
            "quote_max_age_seconds": 5,
            "fee_max_age_seconds": 300,
            "account_state_max_age_seconds": 5,
            "leverage": Decimal("10"),
            "sleeve_allocation_usd": Decimal("1000"),
            "sleeve_margin_cap_fraction": Decimal("0.90"),
            "max_concurrent_positions": 3,
            "max_planned_loss_per_trade_usd": Decimal("50"),
            "daily_loss_cap_usd": Decimal("150"),
            "hard_stop_nav_usd": Decimal("800"),
            "initial_stop_roi_pct": Decimal("-15"),
            "tp1_roi_pct": Decimal("25"),
            "tp1_fraction": Decimal("0.50"),
            "trail_activation_roi_pct": Decimal("35"),
            "first_trail_stop_roi_pct": Decimal("10"),
            "trail_step_roi_pct": Decimal("10"),
            "slippage_reserve_bps_each_side": Decimal("10"),
            "funding_reserve_fraction": Decimal("0.10"),
            "maximum_funding_reserve_usd": Decimal("5"),
            "once_per_symbol_per_risk_epoch": True,
        }
        changed = [
            field_name
            for field_name, expected in frozen_values.items()
            if getattr(self, field_name) != expected
        ]
        if changed:
            raise ValueError(
                "Flow Mirror V1 parameters are frozen; create a new version to change: "
                + ", ".join(changed)
            )
        return self


class FlowMirrorSignalV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="flow-mirror-signal-v1", frozen=True)
    delivery_event_id: str = Field(min_length=1, max_length=128)
    flow_score_id: str = Field(min_length=1, max_length=128)
    flow_alert_id: str = Field(min_length=1, max_length=128)
    delivered_at: datetime
    discord_channel_id: str = Field(min_length=1, max_length=64)
    discord_message_id: str = Field(min_length=1, max_length=64)
    ticker: str = Field(min_length=1, max_length=32)
    direction: SignalDirection
    source_underlying_price: Decimal | None = Field(default=None, gt=0)
    score: Decimal
    scoring_version: str = Field(min_length=1, max_length=64)

    @field_validator("delivered_at")
    @classmethod
    def require_aware_delivery(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("delivered_at must be timezone-aware")
        return value

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.strip().upper()


class FlowMirrorCursorV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delivered_at: datetime
    delivery_event_id: str = Field(min_length=1, max_length=128)

    @field_validator("delivered_at")
    @classmethod
    def require_aware_cursor(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cursor timestamp must be timezone-aware")
        return value


class FlowMirrorQuoteV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1, max_length=32)
    observed_at: datetime
    mark_price: Decimal = Field(gt=0)
    bid_price: Decimal = Field(gt=0)
    ask_price: Decimal = Field(gt=0)
    maximum_leverage: Decimal = Field(gt=0)
    minimum_collateral_usd: Decimal | None = Field(gt=0)

    @field_validator("observed_at")
    @classmethod
    def require_aware_quote(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("quote timestamp must be timezone-aware")
        return value

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.strip().upper()

    @model_validator(mode="after")
    def validate_book(self) -> Self:
        if self.bid_price > self.ask_price:
            raise ValueError("bid cannot exceed ask")
        return self


class FlowMirrorFeeScheduleV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: datetime
    market_entry_rate: Decimal = Field(ge=0, lt=1)
    market_exit_rate: Decimal = Field(ge=0, lt=1)

    @field_validator("observed_at")
    @classmethod
    def require_aware_fee_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fee timestamp must be timezone-aware")
        return value


class FlowMirrorSleeveStateV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: datetime
    risk_epoch: date
    sleeve_nav_usd: Decimal = Field(ge=0)
    deployed_margin_usd: Decimal = Field(ge=0)
    daily_negative_realized_usd: Decimal = Field(ge=0)
    open_negative_pnl_usd: Decimal = Field(ge=0)
    active_symbols: frozenset[str] = frozenset()
    traded_symbols_in_epoch: frozenset[str] = frozenset()
    external_collision_symbols: frozenset[str] = frozenset()
    halted_symbols: frozenset[str] = frozenset()
    daily_halted: bool = False
    account_halted: bool = False

    @field_validator("observed_at")
    @classmethod
    def require_aware_state_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("sleeve-state timestamp must be timezone-aware")
        return value

    @field_validator(
        "active_symbols",
        "traded_symbols_in_epoch",
        "external_collision_symbols",
        "halted_symbols",
    )
    @classmethod
    def normalize_state_symbols(cls, value: frozenset[str]) -> frozenset[str]:
        normalized = frozenset(symbol.strip().upper() for symbol in value if symbol.strip())
        if len(normalized) != len(value):
            raise ValueError("sleeve symbol sets cannot contain blank or duplicate values")
        return normalized

    @property
    def daily_loss_usd(self) -> Decimal:
        return self.daily_negative_realized_usd + self.open_negative_pnl_usd


class FlowMirrorSizingV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    risk_budget_usd: Decimal = Field(gt=0)
    margin_usd: Decimal = Field(gt=0)
    notional_usd: Decimal = Field(gt=0)
    price_stop_risk_usd: Decimal = Field(gt=0)
    fee_reserve_usd: Decimal = Field(ge=0)
    slippage_reserve_usd: Decimal = Field(ge=0)
    funding_reserve_usd: Decimal = Field(ge=0)
    planned_loss_usd: Decimal = Field(gt=0)
    capped_by_margin: bool


class FlowMirrorEntryRiskLimitsV1(BaseModel):
    """Operational limits that may tighten, but never loosen, the frozen strategy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_concurrent_positions: int = Field(gt=0)
    max_planned_loss_per_trade_usd: Decimal = Field(gt=0)
    daily_loss_cap_usd: Decimal = Field(gt=0)


class FlowMirrorEntryDecisionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: FlowMirrorDecision
    reasons: tuple[str, ...]
    sizing: FlowMirrorSizingV1 | None = None


class FlowMirrorEntryPlanV1(BaseModel):
    """A market-entry plan. It is not an executable broker request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="flow-mirror-entry-plan-v1", frozen=True)
    client_order_id: str = Field(min_length=1, max_length=128)
    delivery_event_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=32)
    direction: SignalDirection
    order_type: Literal["market"] = "market"
    notional_usd: Decimal = Field(gt=0)
    margin_usd: Decimal = Field(gt=0)
    leverage: Decimal = Field(gt=0, le=10)
    provisional_stop_price: Decimal = Field(gt=0)
    quote_observed_at: datetime
    expires_at: datetime


class FlowMirrorPositionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position_id: str = Field(min_length=1, max_length=128)
    delivery_event_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=32)
    direction: SignalDirection
    opened_at: datetime
    average_entry_price: Decimal = Field(gt=0)
    initial_quantity: Decimal = Field(gt=0)
    remaining_quantity: Decimal = Field(ge=0)
    leverage: Decimal = Field(gt=0, le=10)
    stage: FlowMirrorPositionStage
    current_stop_roi_pct: Decimal
    high_water_roi_pct: Decimal
    tp1_filled_quantity: Decimal = Field(default=Decimal("0"), ge=0)
    adverse_funding_usd: Decimal = Field(default=Decimal("0"), ge=0)
    funding_reserve_usd: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def quantities_are_consistent(self) -> Self:
        if self.remaining_quantity > self.initial_quantity:
            raise ValueError("remaining quantity cannot exceed initial quantity")
        if self.tp1_filled_quantity > self.initial_quantity:
            raise ValueError("TP1 filled quantity cannot exceed initial quantity")
        return self


class FlowMirrorExitDecisionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: FlowMirrorExitAction
    reasons: tuple[str, ...]
    observed_roi_pct: Decimal
    target_quantity: Decimal | None = Field(default=None, gt=0)
    proposed_stop_roi_pct: Decimal | None = None
    proposed_stop_price: Decimal | None = Field(default=None, gt=0)
    updated_position: FlowMirrorPositionV1

    @model_validator(mode="after")
    def validate_action_payload(self) -> Self:
        if self.action is FlowMirrorExitAction.REPLACE_STOP:
            if self.proposed_stop_roi_pct is None or self.proposed_stop_price is None:
                raise ValueError("replace-stop decisions require both proposed stop fields")
            if self.target_quantity is not None:
                raise ValueError("replace-stop decisions cannot contain an exit quantity")
        elif self.action in (FlowMirrorExitAction.REDUCE_TP1, FlowMirrorExitAction.CLOSE_FULL):
            if self.target_quantity is None:
                raise ValueError("quantity-changing exits require a target quantity")
            if self.proposed_stop_roi_pct is not None or self.proposed_stop_price is not None:
                raise ValueError("quantity-changing exits cannot contain replacement-stop fields")
        elif any(
            value is not None
            for value in (
                self.target_quantity,
                self.proposed_stop_roi_pct,
                self.proposed_stop_price,
            )
        ):
            raise ValueError("hold and halt decisions cannot contain executable fields")
        return self


class FlowMirrorRiskLatchV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    flatten_required: bool
    account_halt_required: bool
    reasons: tuple[str, ...]


def leveraged_price_roi_pct(
    *,
    direction: SignalDirection,
    entry_price: Decimal,
    current_price: Decimal,
    leverage: Decimal,
) -> Decimal:
    if direction is SignalDirection.LONG:
        unleveraged = current_price / entry_price - Decimal("1")
    elif direction is SignalDirection.SHORT:
        unleveraged = Decimal("1") - current_price / entry_price
    else:
        raise ValueError("Flow Mirror positions require long or short direction")
    return unleveraged * leverage * Decimal("100")


def price_for_roi_pct(
    *,
    direction: SignalDirection,
    entry_price: Decimal,
    leverage: Decimal,
    roi_pct: Decimal,
) -> Decimal:
    move = roi_pct / (leverage * Decimal("100"))
    if direction is SignalDirection.LONG:
        price = entry_price * (Decimal("1") + move)
    elif direction is SignalDirection.SHORT:
        price = entry_price * (Decimal("1") - move)
    else:
        raise ValueError("Flow Mirror positions require long or short direction")
    if price <= 0:
        raise ValueError("ROI implies a non-positive price")
    return price


def assess_flow_mirror_entry(
    *,
    config: FlowMirrorConfigV1,
    signal: FlowMirrorSignalV1,
    quote: FlowMirrorQuoteV1,
    fees: FlowMirrorFeeScheduleV1,
    sleeve: FlowMirrorSleeveStateV1,
    now: datetime,
    risk_limits: FlowMirrorEntryRiskLimitsV1 | None = None,
    cumulative_loss_limits_enabled: bool = True,
    estimated_cost_controls_enabled: bool = True,
) -> FlowMirrorEntryDecisionV1:
    """Live mandate may disable cumulative loss gates; frozen callers retain them."""
    _require_aware(now, "now")
    effective_limits = _effective_risk_limits(config=config, risk_limits=risk_limits)
    reasons: list[str] = []
    age = now - signal.delivered_at
    quote_age = now - quote.observed_at
    fee_age = now - fees.observed_at
    state_age = now - sleeve.observed_at

    if age < timedelta(seconds=-2):
        reasons.append("delivery_timestamp_in_future")
    elif age > timedelta(seconds=config.signal_ttl_seconds):
        reasons.append("signal_stale")
    if quote_age < timedelta(seconds=-2):
        reasons.append("quote_timestamp_in_future")
    elif quote_age > timedelta(seconds=config.quote_max_age_seconds):
        reasons.append("quote_stale")
    if estimated_cost_controls_enabled and fee_age < timedelta(seconds=-2):
        reasons.append("fee_timestamp_in_future")
    elif estimated_cost_controls_enabled and fee_age > timedelta(
        seconds=config.fee_max_age_seconds
    ):
        reasons.append("fee_schedule_stale")
    if state_age < timedelta(seconds=-2):
        reasons.append("account_state_timestamp_in_future")
    elif state_age > timedelta(seconds=config.account_state_max_age_seconds):
        reasons.append("account_state_stale")
    if sleeve.risk_epoch != flow_mirror_risk_epoch(now):
        reasons.append("risk_epoch_mismatch")
    if signal.discord_channel_id != config.discord_channel_id:
        reasons.append("wrong_discord_channel")
    if signal.scoring_version != config.scoring_version:
        reasons.append("wrong_scoring_version")
    if signal.direction not in (SignalDirection.LONG, SignalDirection.SHORT):
        reasons.append("unsupported_direction")
    if signal.ticker not in config.allowed_symbols:
        reasons.append("unsupported_exact_symbol")
    if quote.symbol != signal.ticker:
        reasons.append("quote_symbol_mismatch")
    if quote.maximum_leverage < config.leverage:
        reasons.append("ten_x_not_supported")
    if signal.ticker in sleeve.external_collision_symbols:
        reasons.append("external_symbol_collision")
    if signal.ticker in sleeve.halted_symbols:
        reasons.append("symbol_halted")
    if signal.ticker in sleeve.active_symbols:
        reasons.append("symbol_already_open")
    if signal.ticker in sleeve.traded_symbols_in_epoch:
        reasons.append("symbol_already_traded_this_epoch")
    if len(sleeve.active_symbols) >= effective_limits.max_concurrent_positions:
        reasons.append("concurrent_position_cap")
    if cumulative_loss_limits_enabled and (
        sleeve.daily_halted or sleeve.daily_loss_usd >= effective_limits.daily_loss_cap_usd
    ):
        reasons.append("daily_loss_halt")
    if (
        sleeve.account_halted
        or sleeve.sleeve_nav_usd <= 0
        or (cumulative_loss_limits_enabled and sleeve.sleeve_nav_usd <= config.hard_stop_nav_usd)
    ):
        reasons.append("sleeve_hard_stop")

    if reasons:
        return FlowMirrorEntryDecisionV1(
            decision=FlowMirrorDecision.SKIP,
            reasons=tuple(reasons),
        )

    sizing = size_flow_mirror_entry(
        config=config,
        fees=fees,
        sleeve=sleeve,
        minimum_collateral_usd=quote.minimum_collateral_usd,
        risk_limits=effective_limits,
        cumulative_loss_limits_enabled=cumulative_loss_limits_enabled,
        estimated_cost_controls_enabled=estimated_cost_controls_enabled,
    )
    if sizing is None:
        return FlowMirrorEntryDecisionV1(
            decision=FlowMirrorDecision.SKIP,
            reasons=("insufficient_risk_or_margin_capacity",),
        )
    return FlowMirrorEntryDecisionV1(
        decision=FlowMirrorDecision.ENTER,
        reasons=("eligible_confirmed_flow_show_delivery",),
        sizing=sizing,
    )


def size_flow_mirror_entry(
    *,
    config: FlowMirrorConfigV1,
    fees: FlowMirrorFeeScheduleV1,
    sleeve: FlowMirrorSleeveStateV1,
    minimum_collateral_usd: Decimal | None,
    risk_limits: FlowMirrorEntryRiskLimitsV1 | None = None,
    cumulative_loss_limits_enabled: bool = True,
    estimated_cost_controls_enabled: bool = True,
) -> FlowMirrorSizingV1 | None:
    effective_limits = _effective_risk_limits(config=config, risk_limits=risk_limits)
    risk_budget = effective_limits.max_planned_loss_per_trade_usd
    if cumulative_loss_limits_enabled:
        remaining_daily = max(
            Decimal("0"), effective_limits.daily_loss_cap_usd - sleeve.daily_loss_usd
        )
        hard_stop_headroom = max(Decimal("0"), sleeve.sleeve_nav_usd - config.hard_stop_nav_usd)
        risk_budget = min(risk_budget, remaining_daily, hard_stop_headroom)
    if risk_budget <= 0:
        return None

    funding_reserve = min(
        config.maximum_funding_reserve_usd,
        risk_budget * config.funding_reserve_fraction,
    )
    stop_fraction = abs(config.initial_stop_roi_pct) / Decimal("100")
    slippage_rate = config.slippage_reserve_bps_each_side * Decimal("2") / Decimal("10000")
    fee_rate = fees.market_entry_rate + fees.market_exit_rate
    cost_fraction_of_margin = config.leverage * (fee_rate + slippage_rate)
    denominator = stop_fraction + (
        cost_fraction_of_margin if estimated_cost_controls_enabled else 0
    )
    available_margin = max(
        Decimal("0"),
        config.sleeve_allocation_usd * config.sleeve_margin_cap_fraction
        - sleeve.deployed_margin_usd,
    )
    uncapped_margin = (
        risk_budget - (funding_reserve if estimated_cost_controls_enabled else 0)
    ) / denominator
    margin = min(uncapped_margin, available_margin).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if minimum_collateral_usd is not None and margin < minimum_collateral_usd:
        return None

    notional = (margin * config.leverage).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    price_stop_risk = margin * stop_fraction
    fee_reserve = notional * fee_rate
    slippage_reserve = notional * slippage_rate
    planned_loss = price_stop_risk + fee_reserve + slippage_reserve + funding_reserve
    if (planned_loss if estimated_cost_controls_enabled else price_stop_risk) > risk_budget:
        return None
    return FlowMirrorSizingV1(
        risk_budget_usd=risk_budget,
        margin_usd=margin,
        notional_usd=notional,
        price_stop_risk_usd=price_stop_risk,
        fee_reserve_usd=fee_reserve,
        slippage_reserve_usd=slippage_reserve,
        funding_reserve_usd=funding_reserve,
        planned_loss_usd=planned_loss,
        capped_by_margin=margin < uncapped_margin,
    )


def _effective_risk_limits(
    *,
    config: FlowMirrorConfigV1,
    risk_limits: FlowMirrorEntryRiskLimitsV1 | None,
) -> FlowMirrorEntryRiskLimitsV1:
    baseline = FlowMirrorEntryRiskLimitsV1(
        max_concurrent_positions=config.max_concurrent_positions,
        max_planned_loss_per_trade_usd=config.max_planned_loss_per_trade_usd,
        daily_loss_cap_usd=config.daily_loss_cap_usd,
    )
    if risk_limits is None:
        return baseline
    loosened = []
    if risk_limits.max_concurrent_positions > baseline.max_concurrent_positions:
        loosened.append("max_concurrent_positions")
    if risk_limits.max_planned_loss_per_trade_usd > baseline.max_planned_loss_per_trade_usd:
        loosened.append("max_planned_loss_per_trade_usd")
    if risk_limits.daily_loss_cap_usd > baseline.daily_loss_cap_usd:
        loosened.append("daily_loss_cap_usd")
    if loosened:
        raise ValueError("operational risk limits cannot loosen: " + ", ".join(loosened))
    return risk_limits


def build_flow_mirror_entry_plan(
    *,
    config: FlowMirrorConfigV1,
    signal: FlowMirrorSignalV1,
    quote: FlowMirrorQuoteV1,
    decision: FlowMirrorEntryDecisionV1,
) -> FlowMirrorEntryPlanV1:
    if decision.decision is not FlowMirrorDecision.ENTER or decision.sizing is None:
        raise ValueError("an entry plan requires an allowed decision with sizing")
    digest = hashlib.sha256(signal.delivery_event_id.encode()).hexdigest()[:24]
    return FlowMirrorEntryPlanV1(
        client_order_id=f"fm-{digest}",
        delivery_event_id=signal.delivery_event_id,
        symbol=signal.ticker,
        direction=signal.direction,
        notional_usd=decision.sizing.notional_usd,
        margin_usd=decision.sizing.margin_usd,
        leverage=config.leverage,
        provisional_stop_price=price_for_roi_pct(
            direction=signal.direction,
            entry_price=quote.mark_price,
            leverage=config.leverage,
            roi_pct=config.initial_stop_roi_pct,
        ),
        quote_observed_at=quote.observed_at,
        expires_at=signal.delivered_at + timedelta(seconds=config.signal_ttl_seconds),
    )


def open_flow_mirror_position(
    *,
    config: FlowMirrorConfigV1,
    plan: FlowMirrorEntryPlanV1,
    average_fill_price: Decimal,
    filled_quantity: Decimal,
    opened_at: datetime,
    funding_reserve_usd: Decimal,
) -> FlowMirrorPositionV1:
    _require_aware(opened_at, "opened_at")
    if average_fill_price <= 0 or filled_quantity <= 0:
        raise ValueError("authoritative positive fill price and quantity are required")
    return FlowMirrorPositionV1(
        position_id=plan.client_order_id,
        delivery_event_id=plan.delivery_event_id,
        symbol=plan.symbol,
        direction=plan.direction,
        opened_at=opened_at,
        average_entry_price=average_fill_price,
        initial_quantity=filled_quantity,
        remaining_quantity=filled_quantity,
        leverage=plan.leverage,
        stage=FlowMirrorPositionStage.INITIAL,
        current_stop_roi_pct=config.initial_stop_roi_pct,
        high_water_roi_pct=Decimal("0"),
        funding_reserve_usd=funding_reserve_usd,
    )


def evaluate_flow_mirror_exit(
    *,
    config: FlowMirrorConfigV1,
    position: FlowMirrorPositionV1,
    current_price: Decimal,
    funding_exit_enabled: bool = True,
) -> FlowMirrorExitDecisionV1:
    if position.stage in (FlowMirrorPositionStage.CLOSED, FlowMirrorPositionStage.HALTED):
        raise ValueError("closed or halted positions cannot be evaluated")
    roi = leveraged_price_roi_pct(
        direction=position.direction,
        entry_price=position.average_entry_price,
        current_price=current_price,
        leverage=position.leverage,
    )
    high_water = max(position.high_water_roi_pct, roi)
    updated = position.model_copy(update={"high_water_roi_pct": high_water})

    if (
        funding_exit_enabled
        and position.adverse_funding_usd > 0
        and position.adverse_funding_usd >= position.funding_reserve_usd
    ):
        return FlowMirrorExitDecisionV1(
            action=FlowMirrorExitAction.CLOSE_FULL,
            reasons=("funding_reserve_exhausted",),
            observed_roi_pct=roi,
            target_quantity=position.remaining_quantity,
            updated_position=updated.model_copy(
                update={"stage": FlowMirrorPositionStage.EXIT_PENDING}
            ),
        )
    if roi <= position.current_stop_roi_pct:
        return FlowMirrorExitDecisionV1(
            action=FlowMirrorExitAction.CLOSE_FULL,
            reasons=("price_stop_crossed",),
            observed_roi_pct=roi,
            target_quantity=position.remaining_quantity,
            updated_position=updated.model_copy(
                update={"stage": FlowMirrorPositionStage.EXIT_PENDING}
            ),
        )
    if position.stage is FlowMirrorPositionStage.INITIAL and roi >= config.tp1_roi_pct:
        target = position.initial_quantity * config.tp1_fraction
        return FlowMirrorExitDecisionV1(
            action=FlowMirrorExitAction.REDUCE_TP1,
            reasons=("tp1_threshold_reached",),
            observed_roi_pct=roi,
            target_quantity=target,
            updated_position=updated.model_copy(
                update={"stage": FlowMirrorPositionStage.TP1_PENDING}
            ),
        )
    if position.stage is FlowMirrorPositionStage.RUNNER:
        proposed_stop = _trailing_stop_roi(config=config, high_water_roi_pct=high_water)
        if proposed_stop is not None and proposed_stop > position.current_stop_roi_pct:
            if roi <= proposed_stop:
                return FlowMirrorExitDecisionV1(
                    action=FlowMirrorExitAction.CLOSE_FULL,
                    reasons=("computed_trailing_stop_already_crossed",),
                    observed_roi_pct=roi,
                    target_quantity=position.remaining_quantity,
                    updated_position=updated.model_copy(
                        update={"stage": FlowMirrorPositionStage.EXIT_PENDING}
                    ),
                )
            return FlowMirrorExitDecisionV1(
                action=FlowMirrorExitAction.REPLACE_STOP,
                reasons=("trailing_step_reached",),
                observed_roi_pct=roi,
                proposed_stop_roi_pct=proposed_stop,
                proposed_stop_price=price_for_roi_pct(
                    direction=position.direction,
                    entry_price=position.average_entry_price,
                    leverage=position.leverage,
                    roi_pct=proposed_stop,
                ),
                updated_position=updated,
            )
    return FlowMirrorExitDecisionV1(
        action=FlowMirrorExitAction.HOLD,
        reasons=("no_exit_transition",),
        observed_roi_pct=roi,
        updated_position=updated,
    )


def apply_tp1_fill(
    *,
    config: FlowMirrorConfigV1,
    position: FlowMirrorPositionV1,
    filled_reduction_quantity: Decimal,
) -> FlowMirrorPositionV1:
    if position.stage is not FlowMirrorPositionStage.TP1_PENDING:
        raise ValueError("TP1 fills require a pending TP1 transition")
    if filled_reduction_quantity <= 0 or filled_reduction_quantity > position.remaining_quantity:
        raise ValueError("TP1 fill must be positive and no larger than remaining quantity")
    cumulative = position.tp1_filled_quantity + filled_reduction_quantity
    remaining = position.remaining_quantity - filled_reduction_quantity
    target = position.initial_quantity * config.tp1_fraction
    if cumulative > target:
        raise ValueError("TP1 fill exceeds the approved reduction target")
    if cumulative >= target:
        return position.model_copy(
            update={
                "tp1_filled_quantity": cumulative,
                "remaining_quantity": remaining,
                "stage": FlowMirrorPositionStage.RUNNER,
                "current_stop_roi_pct": Decimal("0"),
            }
        )
    return position.model_copy(
        update={
            "tp1_filled_quantity": cumulative,
            "remaining_quantity": remaining,
        }
    )


def confirm_stop_replacement(
    *, position: FlowMirrorPositionV1, stop_roi_pct: Decimal
) -> FlowMirrorPositionV1:
    if position.stage is not FlowMirrorPositionStage.RUNNER:
        raise ValueError("only runner positions can replace trailing stops")
    if stop_roi_pct < position.current_stop_roi_pct:
        raise ValueError("Flow Mirror stops are monotonic and cannot be loosened")
    return position.model_copy(update={"current_stop_roi_pct": stop_roi_pct})


def apply_adverse_funding(
    *, position: FlowMirrorPositionV1, adverse_funding_usd: Decimal
) -> FlowMirrorPositionV1:
    if adverse_funding_usd < 0:
        raise ValueError("adverse funding increment cannot be negative")
    return position.model_copy(
        update={"adverse_funding_usd": position.adverse_funding_usd + adverse_funding_usd}
    )


def assess_flow_mirror_risk_latches(
    *,
    config: FlowMirrorConfigV1,
    sleeve: FlowMirrorSleeveStateV1,
    risk_limits: FlowMirrorEntryRiskLimitsV1 | None = None,
) -> FlowMirrorRiskLatchV1:
    effective_limits = _effective_risk_limits(config=config, risk_limits=risk_limits)
    reasons: list[str] = []
    account_halt = sleeve.account_halted or sleeve.sleeve_nav_usd <= config.hard_stop_nav_usd
    daily_halt = sleeve.daily_halted or sleeve.daily_loss_usd >= effective_limits.daily_loss_cap_usd
    if daily_halt:
        reasons.append("daily_loss_cap_reached")
    if account_halt:
        reasons.append("sleeve_nav_hard_stop_reached")
    return FlowMirrorRiskLatchV1(
        flatten_required=daily_halt or account_halt,
        account_halt_required=account_halt,
        reasons=tuple(reasons),
    )


def _trailing_stop_roi(
    *, config: FlowMirrorConfigV1, high_water_roi_pct: Decimal
) -> Decimal | None:
    if high_water_roi_pct < config.trail_activation_roi_pct:
        return None
    completed_steps = (
        (high_water_roi_pct - config.trail_activation_roi_pct) / config.trail_step_roi_pct
    ).to_integral_value(rounding=ROUND_DOWN)
    return config.first_trail_stop_roi_pct + completed_steps * config.trail_step_roi_pct


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
