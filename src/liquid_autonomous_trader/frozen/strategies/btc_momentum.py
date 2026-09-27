"""Deterministic, inert BTC momentum strategy."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from liquid_autonomous_trader.frozen.models import (
    BracketV1,
    MarketContextV1,
    SignalAction,
    SignalDirection,
    StrategyId,
    StrategySignalV1,
)


class BtcMomentumConfigV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = "btc-momentum-v1"
    symbol: str = "BTC"
    max_leverage: Decimal = Field(default=Decimal("40"), gt=0, le=40)
    minimum_abs_return_15m: Decimal = Field(default=Decimal("0.0025"), gt=0)
    minimum_abs_return_1h: Decimal = Field(default=Decimal("0.005"), gt=0)
    minimum_trend_efficiency: Decimal = Field(default=Decimal("0.20"), ge=0, le=1)
    minimum_volume_acceleration: Decimal = Field(default=Decimal("1.20"), gt=0)
    funding_filter_enabled: bool = True
    maximum_aligned_funding: Decimal = Field(default=Decimal("0.00030"), ge=0)
    max_spread_bps: Decimal = Field(default=Decimal("10"), ge=0)
    minimum_depth_multiple: Decimal = Field(default=Decimal("5"), gt=0)
    stop_atr_multiple: Decimal = Field(default=Decimal("1.5"), gt=0)
    stop_spread_multiple: Decimal = Field(default=Decimal("3"), gt=0)
    target_r_multiple: Decimal = Field(default=Decimal("2"), gt=0)
    ttl_minutes: int = Field(default=15, gt=0)


class BtcMomentumObservationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    market: MarketContextV1
    return_15m: Decimal
    return_1h: Decimal
    trend_efficiency: Decimal = Field(ge=0, le=1)
    volume_acceleration: Decimal = Field(ge=0)
    funding_rate: Decimal | None
    persistence_bars: int = Field(ge=0)
    data_complete: bool


class BtcMomentumEngineV1:
    """Pure momentum evaluator. It never fetches data or calls a broker."""

    def __init__(self, config: BtcMomentumConfigV1) -> None:
        self._config = config

    def evaluate(self, observation: BtcMomentumObservationV1) -> StrategySignalV1:
        market = observation.market
        reasons: list[str] = []
        if market.symbol.upper() != self._config.symbol:
            reasons.append("unexpected_symbol")
        if not observation.data_complete:
            reasons.append("incomplete_market_data")
        if market.spread_bps > self._config.max_spread_bps:
            reasons.append("spread_too_wide")
        if market.depth_multiple < self._config.minimum_depth_multiple:
            reasons.append("insufficient_depth")
        if observation.persistence_bars < 2:
            reasons.append("persistence_not_met")
        if observation.trend_efficiency < self._config.minimum_trend_efficiency:
            reasons.append("trend_efficiency_too_low")
        if observation.volume_acceleration < self._config.minimum_volume_acceleration:
            reasons.append("volume_acceleration_too_low")
        if reasons:
            return self._hold(observation, tuple(reasons))
        direction = self._direction(observation)
        if direction is SignalDirection.NONE:
            return self._hold(observation, ("momentum_not_aligned",))
        if self._config.funding_filter_enabled and observation.funding_rate is None:
            return self._hold(observation, ("funding_unavailable",))
        if (
            self._config.funding_filter_enabled
            and direction is SignalDirection.LONG
            and observation.funding_rate > self._config.maximum_aligned_funding
        ):
            return self._hold(observation, ("aligned_funding_too_high",))
        if (
            self._config.funding_filter_enabled
            and direction is SignalDirection.SHORT
            and observation.funding_rate < -self._config.maximum_aligned_funding
        ):
            return self._hold(observation, ("aligned_funding_too_high",))
        stop_distance = max(
            market.atr_15m * self._config.stop_atr_multiple,
            market.spread_price * self._config.stop_spread_multiple,
        )
        stop = (
            market.price - stop_distance
            if direction is SignalDirection.LONG
            else market.price + stop_distance
        )
        target = (
            market.price + stop_distance * self._config.target_r_multiple
            if direction is SignalDirection.LONG
            else market.price - stop_distance * self._config.target_r_multiple
        )
        return StrategySignalV1(
            signal_id=self._signal_id(observation, direction, SignalAction.ENTER),
            strategy_id=StrategyId.BTC_MOMENTUM,
            strategy_version=self._config.version,
            symbol=self._config.symbol,
            action=SignalAction.ENTER,
            direction=direction,
            leverage=self._config.max_leverage,
            confidence_or_score=abs(observation.return_15m) + abs(observation.return_1h),
            created_at=market.observed_at,
            expires_at=market.observed_at + timedelta(minutes=self._config.ttl_minutes),
            bracket=BracketV1(
                stop_price=stop,
                target_price=target,
                initial_risk_distance=stop_distance,
                trail_activation_r=Decimal("1"),
                trail_atr_multiple=Decimal("1"),
            ),
        )

    def _direction(self, observation: BtcMomentumObservationV1) -> SignalDirection:
        if (
            observation.return_15m >= self._config.minimum_abs_return_15m
            and observation.return_1h >= self._config.minimum_abs_return_1h
        ):
            return SignalDirection.LONG
        if (
            observation.return_15m <= -self._config.minimum_abs_return_15m
            and observation.return_1h <= -self._config.minimum_abs_return_1h
        ):
            return SignalDirection.SHORT
        return SignalDirection.NONE

    def _hold(
        self, observation: BtcMomentumObservationV1, reasons: tuple[str, ...]
    ) -> StrategySignalV1:
        market = observation.market
        return StrategySignalV1(
            signal_id=self._signal_id(observation, SignalDirection.NONE, SignalAction.HOLD),
            strategy_id=StrategyId.BTC_MOMENTUM,
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
        self,
        observation: BtcMomentumObservationV1,
        direction: SignalDirection,
        action: SignalAction,
    ) -> str:
        material = (
            f"{self._config.version}:{observation.market.observed_at.isoformat()}:"
            f"{direction.value}:{action.value}"
        )
        return hashlib.sha256(material.encode()).hexdigest()[:40]
