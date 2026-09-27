"""Immutable, versioned domain models shared across deterministic components."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExecutionMode(StrEnum):
    DISABLED = "disabled"
    REPLAY = "replay"
    SHADOW = "shadow"
    PAPER = "paper"
    LIVE = "live"


class StrategyId(StrEnum):
    INVERSE_CRAMER = "inverse_cramer"
    PERP_ARENA = "perp_arena"
    FLOW_SHOW_MIRROR = "flow_show_mirror"
    BTC_MOMENTUM = "btc_momentum"
    XYZ100_GEX = "xyz100_gex"


class SignalDirection(StrEnum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


class SignalAction(StrEnum):
    ENTER = "enter"
    CLOSE = "close"
    HOLD = "hold"


class CramerStance(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNCLEAR = "unclear"


class BrokerEventType(StrEnum):
    ACKNOWLEDGED = "acknowledged"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderIntent(BaseModel):
    """A proposed order. It carries no authority to execute."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_order_id: str = Field(min_length=1, max_length=128)
    symbol: str = Field(min_length=1, max_length=32)
    side: OrderSide
    notional_usd: Decimal = Field(gt=0)
    leverage: Decimal = Field(gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    market_data_at: datetime
    advisory_text: str | None = None

    @field_validator("market_data_at")
    @classmethod
    def require_aware_market_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("market_data_at must be timezone-aware")
        return value


class AccountState(BaseModel):
    """Minimal fetched-state proof used by the risk layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fetched_at: datetime
    state_fingerprint: str = Field(min_length=1)

    @field_validator("fetched_at")
    @classmethod
    def require_aware_fetch_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fetched_at must be timezone-aware")
        return value


class PortfolioSnapshot(BaseModel):
    """Sanitized state used for reconciliation; never a raw account payload."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    position_keys: frozenset[str] = frozenset()
    open_order_keys: frozenset[str] = frozenset()


class RiskDecision(BaseModel):
    """Deterministic decision with stable machine-readable reasons."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reasons: tuple[str, ...]


class XSourceEventV1(BaseModel):
    """Sanitized public-source event; full article bodies are never retained."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: str = Field(min_length=1, max_length=128)
    source_handle: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=10_000)
    source_url: str = Field(min_length=1, max_length=2_048)
    published_at: datetime
    discovered_at: datetime
    is_official_source: bool
    direct_attribution: bool

    @field_validator("published_at", "discovered_at")
    @classmethod
    def require_aware_source_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("source timestamps must be timezone-aware")
        return value


class CramerClassificationV1(BaseModel):
    """Strict model output. Deterministic code owns every downstream decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="cramer-classification-v1", frozen=True)
    source_id: str = Field(min_length=1, max_length=128)
    speaker_verified: bool
    issuer_text: str = Field(min_length=1, max_length=256)
    ticker_candidate: str | None = Field(default=None, max_length=32)
    stance: CramerStance
    conditional: bool
    horizon: str = Field(min_length=1, max_length=64)
    evidence: str = Field(min_length=1, max_length=500)
    confidence: Decimal = Field(ge=0, le=1)
    calibration_version: str = Field(min_length=1, max_length=64)
    uncertainty_reason: str | None = Field(default=None, max_length=500)


class RawCramerClassificationV1(BaseModel):
    """Uncalibrated model output; never valid strategy input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="raw-cramer-classification-v1", frozen=True)
    source_id: str = Field(min_length=1, max_length=128)
    speaker_verified: bool
    issuer_text: str = Field(min_length=1, max_length=256)
    ticker_candidate: str | None = Field(default=None, max_length=32)
    stance: CramerStance
    conditional: bool
    horizon: str = Field(min_length=1, max_length=64)
    evidence: str = Field(min_length=1, max_length=500)
    raw_confidence: Decimal = Field(ge=0, le=1)
    uncertainty_reason: str | None = Field(default=None, max_length=500)


class MarketContextV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1, max_length=32)
    observed_at: datetime
    price: Decimal = Field(gt=0)
    atr_15m: Decimal = Field(gt=0)
    spread_price: Decimal = Field(ge=0)
    spread_bps: Decimal = Field(ge=0)
    depth_multiple: Decimal = Field(ge=0)

    @field_validator("observed_at")
    @classmethod
    def require_aware_observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class BracketV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stop_price: Decimal = Field(gt=0)
    target_price: Decimal = Field(gt=0)
    initial_risk_distance: Decimal = Field(gt=0)
    trail_activation_r: Decimal = Field(gt=0)
    trail_atr_multiple: Decimal = Field(gt=0)


class StrategySignalV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="strategy-signal-v1", frozen=True)
    signal_id: str = Field(min_length=1, max_length=128)
    strategy_id: StrategyId
    strategy_version: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=32)
    action: SignalAction
    direction: SignalDirection
    leverage: Decimal = Field(ge=0, le=40)
    confidence_or_score: Decimal
    created_at: datetime
    expires_at: datetime
    bracket: BracketV1 | None = None
    reason_codes: tuple[str, ...] = ()

    @field_validator("created_at", "expires_at")
    @classmethod
    def require_aware_signal_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("signal timestamps must be timezone-aware")
        return value


class PerpFeatureRowV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str = Field(min_length=1, max_length=32)
    observed_at: datetime
    seven_day_dollar_volume: Decimal = Field(ge=0)
    history_days: int = Field(ge=0)
    data_complete: bool
    momentum_1h_vol_adjusted: Decimal
    momentum_4h_vol_adjusted: Decimal
    momentum_24h_vol_adjusted: Decimal
    trend_efficiency_4h: Decimal
    volume_acceleration_1h: Decimal
    oi_change_4h_aligned: Decimal
    funding_z: Decimal
    positioning_z: Decimal | None = None
    market: MarketContextV1

    @field_validator("observed_at")
    @classmethod
    def require_aware_feature_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class PerpRankSnapshotV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="perp-rank-v1", frozen=True)
    symbol: str
    observed_at: datetime
    composite_score: Decimal
    rank: int = Field(gt=0)
    eligible: bool
    persistence_bars: int = Field(ge=0)
    leverage: Decimal = Field(ge=0, le=10)
    direction: SignalDirection
    reason_codes: tuple[str, ...] = ()


class AccountRiskSnapshotV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: datetime
    opening_cash_usd: Decimal = Field(ge=0)
    opening_equity_usd: Decimal = Field(ge=0)
    current_equity_usd: Decimal = Field(ge=0)
    cramer_margin_usd: Decimal = Field(ge=0)
    arena_margin_usd: Decimal = Field(ge=0)
    open_stop_risk_usd: Decimal = Field(ge=0)
    realized_pnl_usd: Decimal
    fees_usd: Decimal = Field(ge=0)
    funding_usd: Decimal
    cramer_positions: int = Field(ge=0)
    arena_long_positions: int = Field(ge=0)
    arena_short_positions: int = Field(ge=0)
    arena_long_notional_usd: Decimal = Field(ge=0)
    arena_short_notional_usd: Decimal = Field(ge=0)
    largest_arena_leg_notional_usd: Decimal = Field(ge=0)


class RiskRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy_id: StrategyId
    symbol: str = Field(min_length=1, max_length=32)
    direction: SignalDirection
    requested_leverage: Decimal = Field(gt=0, le=10)
    requested_notional_usd: Decimal = Field(gt=0)
    requested_margin_usd: Decimal = Field(gt=0)
    planned_stop_risk_usd: Decimal = Field(gt=0)
    stressed_cost_usd: Decimal = Field(ge=0)
    liquidation_distance_price: Decimal = Field(gt=0)
    stop_distance_price: Decimal = Field(gt=0)
    stressed_slippage_price: Decimal = Field(ge=0)


class PositionStateV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position_id: str = Field(min_length=1, max_length=128)
    strategy_id: StrategyId
    symbol: str = Field(min_length=1, max_length=32)
    direction: SignalDirection
    opened_at: datetime
    entry_price: Decimal = Field(gt=0)
    current_stop_price: Decimal = Field(gt=0)
    target_price: Decimal = Field(gt=0)
    initial_risk_distance: Decimal = Field(gt=0)
    highest_price: Decimal = Field(gt=0)
    lowest_price: Decimal = Field(gt=0)
    rank_invalid_bars: int = Field(default=0, ge=0)


class ExitAction(StrEnum):
    HOLD = "hold"
    CLOSE = "close"
    UPDATE_STOP = "update_stop"


class ExitDecisionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ExitAction
    reasons: tuple[str, ...]
    new_stop_price: Decimal | None = Field(default=None, gt=0)


class PositionSizeV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    notional_usd: Decimal = Field(gt=0)
    margin_usd: Decimal = Field(gt=0)
    planned_stop_risk_usd: Decimal = Field(gt=0)
    capped_by_margin: bool


class RiskDecisionV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed: bool
    reasons: tuple[str, ...]
    daily_loss_budget_usd: Decimal = Field(ge=0)
    current_drawdown_usd: Decimal = Field(ge=0)
    remaining_daily_risk_usd: Decimal = Field(ge=0)
    projected_margin_usd: Decimal = Field(ge=0)
    flatten_required: bool = False


class OrderIntentV1(BaseModel):
    """Versioned order intent with no embedded execution authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="order-intent-v1", frozen=True)
    client_order_id: str = Field(min_length=1, max_length=128)
    signal_id: str = Field(min_length=1, max_length=128)
    strategy_id: StrategyId
    strategy_version: str = Field(min_length=1, max_length=64)
    symbol: str = Field(min_length=1, max_length=32)
    side: OrderSide
    notional_usd: Decimal = Field(gt=0)
    margin_usd: Decimal = Field(gt=0)
    leverage: Decimal = Field(gt=0, le=10)
    limit_price: Decimal = Field(gt=0)
    bracket: BracketV1
    market_data_at: datetime
    expires_at: datetime


class BrokerEventV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = Field(default="broker-event-v1", frozen=True)
    client_order_id: str
    broker_order_key: str | None = None
    event_type: BrokerEventType
    observed_at: datetime
    filled_notional_usd: Decimal = Field(default=Decimal("0"), ge=0)
    protected_quantity_confirmed: bool = False
    reconciliation_fingerprint: str | None = None
