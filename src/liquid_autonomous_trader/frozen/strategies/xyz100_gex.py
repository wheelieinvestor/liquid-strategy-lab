"""Deterministic, inert XYZ100 GEX level-reaction and breakout strategy."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from liquid_autonomous_trader.frozen.models import (
    BracketV1,
    MarketContextV1,
    SignalAction,
    SignalDirection,
    StrategyId,
    StrategySignalV1,
)


class GexRegimeV1(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    TRANSITION = "transition"


class XYZ100GexConfigV1(BaseModel):
    """Frozen parameters for a shadow-only XYZ100 GEX strategy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = "xyz100-gex-v1"
    symbol: str = "XYZ100"
    max_leverage: Decimal = Field(default=Decimal("30"), gt=0, le=40)
    max_gex_age_seconds: int = Field(default=300, gt=0)
    max_market_age_seconds: int = Field(default=10, gt=0)
    max_spread_bps: Decimal = Field(default=Decimal("15"), ge=0)
    minimum_depth_multiple: Decimal = Field(default=Decimal("5"), gt=0)
    level_tolerance_atr: Decimal = Field(default=Decimal("0.25"), gt=0)
    breakout_buffer_atr: Decimal = Field(default=Decimal("0.15"), gt=0)
    stop_buffer_atr: Decimal = Field(default=Decimal("0.50"), gt=0)
    stop_spread_multiple: Decimal = Field(default=Decimal("3"), gt=0)
    signal_ttl_minutes: int = Field(default=15, gt=0)


class XYZ100GexSnapshotV1(BaseModel):
    """Validated point-in-time GEX geometry, never a directional prediction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_symbol: str = Field(min_length=1, max_length=32)
    source_underlying: str | None = None
    mapping_method: Literal["legacy_identity", "synchronized_ratio_proxy"] = "legacy_identity"
    observed_at: datetime
    provenance_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    complete: bool
    identity_verified: bool
    revision_ambiguous: bool = False
    regime: GexRegimeV1
    support_level: Decimal = Field(gt=0)
    resistance_level: Decimal = Field(gt=0)
    zero_gamma_level: Decimal | None = Field(default=None, gt=0)
    downside_target: Decimal = Field(gt=0)
    upside_target: Decimal = Field(gt=0)

    @field_validator("observed_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if not hasattr(value, "tzinfo") or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class XYZ100GexObservationV1(BaseModel):
    """Two completed 15-minute closes plus the validated GEX map."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    market: MarketContextV1
    prior_close: Decimal = Field(gt=0)
    latest_close: Decimal = Field(gt=0)
    trend_1h: SignalDirection
    gex: XYZ100GexSnapshotV1


class XYZ100GexEngineV1:
    """Pure engine. It emits inert signals and never contacts a broker."""

    def __init__(self, config: XYZ100GexConfigV1) -> None:
        self._config = config

    def evaluate(self, observation: XYZ100GexObservationV1) -> StrategySignalV1:
        reasons = self._invalid_reasons(observation)
        if reasons:
            return self._hold(observation, tuple(reasons))
        if observation.gex.regime is GexRegimeV1.TRANSITION:
            return self._hold(observation, ("gex_regime_transition",))
        if observation.gex.regime is GexRegimeV1.POSITIVE:
            return self._positive_gamma_signal(observation)
        return self._negative_gamma_signal(observation)

    def _invalid_reasons(self, observation: XYZ100GexObservationV1) -> list[str]:
        market = observation.market
        gex = observation.gex
        reasons: list[str] = []
        if market.symbol.upper() != self._config.symbol:
            reasons.append("unexpected_symbol")
        if gex.source_symbol.upper() != self._config.symbol:
            reasons.append("unverified_gex_symbol_mapping")
        if not gex.complete:
            reasons.append("incomplete_gex_snapshot")
        if not gex.identity_verified:
            reasons.append("unverified_gex_identity")
        if gex.revision_ambiguous:
            reasons.append("ambiguous_gex_revision")
        market_age = (market.observed_at - gex.observed_at).total_seconds()
        if market_age < 0 or market_age > self._config.max_gex_age_seconds:
            reasons.append("stale_or_future_gex_snapshot")
        if market.spread_bps > self._config.max_spread_bps:
            reasons.append("spread_too_wide")
        if market.depth_multiple < self._config.minimum_depth_multiple:
            reasons.append("insufficient_depth")
        if observation.trend_1h is SignalDirection.NONE:
            reasons.append("missing_hourly_trend")
        return reasons

    def _positive_gamma_signal(self, observation: XYZ100GexObservationV1) -> StrategySignalV1:
        market, gex = observation.market, observation.gex
        tolerance = market.atr_15m * self._config.level_tolerance_atr
        if (
            observation.prior_close <= gex.support_level + tolerance
            and observation.latest_close > gex.support_level
            and observation.trend_1h is SignalDirection.LONG
        ):
            return self._enter(
                observation,
                SignalDirection.LONG,
                gex.support_level,
                gex.upside_target,
                "positive_gamma_support_reclaim",
            )
        if (
            observation.prior_close >= gex.resistance_level - tolerance
            and observation.latest_close < gex.resistance_level
            and observation.trend_1h is SignalDirection.SHORT
        ):
            return self._enter(
                observation,
                SignalDirection.SHORT,
                gex.resistance_level,
                gex.downside_target,
                "positive_gamma_resistance_rejection",
            )
        return self._hold(observation, ("no_positive_gamma_level_rejection",))

    def _negative_gamma_signal(self, observation: XYZ100GexObservationV1) -> StrategySignalV1:
        market, gex = observation.market, observation.gex
        buffer = market.atr_15m * self._config.breakout_buffer_atr
        if (
            observation.prior_close <= gex.resistance_level + buffer
            and observation.latest_close > gex.resistance_level + buffer
            and observation.trend_1h is SignalDirection.LONG
        ):
            return self._enter(
                observation,
                SignalDirection.LONG,
                gex.resistance_level,
                gex.upside_target,
                "negative_gamma_resistance_breakout",
            )
        if (
            observation.prior_close >= gex.support_level - buffer
            and observation.latest_close < gex.support_level - buffer
            and observation.trend_1h is SignalDirection.SHORT
        ):
            return self._enter(
                observation,
                SignalDirection.SHORT,
                gex.support_level,
                gex.downside_target,
                "negative_gamma_support_breakdown",
            )
        return self._hold(observation, ("no_negative_gamma_break_and_hold",))

    def _enter(
        self,
        observation: XYZ100GexObservationV1,
        direction: SignalDirection,
        level: Decimal,
        target: Decimal,
        reason: str,
    ) -> StrategySignalV1:
        price, market = observation.latest_close, observation.market
        if (direction is SignalDirection.LONG and target <= price) or (
            direction is SignalDirection.SHORT and target >= price
        ):
            return self._hold(observation, ("invalid_gex_target_geometry",))
        stop_buffer = max(
            market.atr_15m * self._config.stop_buffer_atr,
            market.spread_price * self._config.stop_spread_multiple,
        )
        stop = level - stop_buffer if direction is SignalDirection.LONG else level + stop_buffer
        risk = abs(price - stop)
        if risk <= 0:
            return self._hold(observation, ("invalid_stop_geometry",))
        return StrategySignalV1(
            signal_id=self._signal_id(observation, direction, SignalAction.ENTER),
            strategy_id=StrategyId.XYZ100_GEX,
            strategy_version=self._config.version,
            symbol=self._config.symbol,
            action=SignalAction.ENTER,
            direction=direction,
            leverage=self._config.max_leverage,
            confidence_or_score=Decimal("1"),
            created_at=market.observed_at,
            expires_at=market.observed_at + timedelta(minutes=self._config.signal_ttl_minutes),
            bracket=BracketV1(
                stop_price=stop,
                target_price=target,
                initial_risk_distance=risk,
                trail_activation_r=Decimal("1"),
                trail_atr_multiple=Decimal("1"),
            ),
            reason_codes=(reason,),
        )

    def _hold(
        self, observation: XYZ100GexObservationV1, reasons: tuple[str, ...]
    ) -> StrategySignalV1:
        market = observation.market
        return StrategySignalV1(
            signal_id=self._signal_id(observation, SignalDirection.NONE, SignalAction.HOLD),
            strategy_id=StrategyId.XYZ100_GEX,
            strategy_version=self._config.version,
            symbol=self._config.symbol,
            action=SignalAction.HOLD,
            direction=SignalDirection.NONE,
            leverage=Decimal("0"),
            confidence_or_score=Decimal("0"),
            created_at=market.observed_at,
            expires_at=market.observed_at,
            reason_codes=reasons,
        )

    def _signal_id(
        self, observation: XYZ100GexObservationV1, direction: SignalDirection, action: SignalAction
    ) -> str:
        material = (
            f"{self._config.version}:{observation.market.observed_at.isoformat()}:"
            f"{observation.gex.provenance_sha256}:{direction.value}:{action.value}"
        )
        return hashlib.sha256(material.encode()).hexdigest()[:40]
