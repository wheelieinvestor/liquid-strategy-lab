"""Deterministic BTC closed-15-minute-bar feature semantics, version 2."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from liquid_autonomous_trader.btc_source import BAR_MS, Candle

FEATURE_SCHEMA_V2 = "btc-features-v2"
ATR_PERIOD = 14
EFFICIENCY_PERIOD = 4
VOLUME_BASELINE_PERIOD = 20
MINIMUM_FEATURE_BARS = VOLUME_BASELINE_PERIOD + 1


class BtcFeatureSetV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: str = FEATURE_SCHEMA_V2
    bar_open_ms: int = Field(ge=0)
    bar_end_ms: int = Field(ge=0)
    close: Decimal = Field(gt=0)
    return_1bar: Decimal
    return_4bar: Decimal
    atr14_price: Decimal = Field(gt=0)
    efficiency_4bar: Decimal = Field(ge=0, le=1)
    volume_current_base: Decimal = Field(ge=0)
    volume_baseline_prior20_base_mean: Decimal = Field(gt=0)
    volume_acceleration: Decimal = Field(ge=0)
    persistence_bars: int = Field(ge=0)


def build_features_v2(candles: list[Candle]) -> BtcFeatureSetV2:
    """Build declared v2 features from contiguous, closed candles only."""
    if len(candles) < MINIMUM_FEATURE_BARS:
        raise ValueError("insufficient_feature_history")
    if any(b.t - a.t != BAR_MS for a, b in zip(candles, candles[1:])):
        raise ValueError("gapped_duplicate_or_unsorted_candles")

    latest = candles[-1]
    one_bar = latest.c / candles[-2].c - 1
    four_bar = latest.c / candles[-5].c - 1

    true_ranges: list[Decimal] = []
    for index in range(len(candles) - ATR_PERIOD, len(candles)):
        bar = candles[index]
        previous_close = candles[index - 1].c
        true_ranges.append(
            max(bar.h - bar.l, abs(bar.h - previous_close), abs(bar.l - previous_close))
        )
    atr = sum(true_ranges, Decimal(0)) / ATR_PERIOD
    if atr <= 0:
        raise ValueError("nonpositive_atr")

    path = candles[-5:]
    traveled = sum((abs(b.c - a.c) for a, b in zip(path, path[1:])), Decimal(0))
    efficiency = abs(path[-1].c - path[0].c) / traveled if traveled else Decimal(0)

    prior_volumes = [bar.v for bar in candles[-(VOLUME_BASELINE_PERIOD + 1) : -1]]
    volume_baseline = sum(prior_volumes, Decimal(0)) / VOLUME_BASELINE_PERIOD
    if volume_baseline <= 0:
        raise ValueError("nonpositive_volume_baseline")

    latest_delta = latest.c - candles[-2].c
    persistence = 0
    if latest_delta:
        direction = 1 if latest_delta > 0 else -1
        for earlier, later in reversed(list(zip(candles, candles[1:]))):
            delta = later.c - earlier.c
            if not delta or (1 if delta > 0 else -1) != direction:
                break
            persistence += 1

    return BtcFeatureSetV2(
        bar_open_ms=latest.t,
        bar_end_ms=latest.T,
        close=latest.c,
        return_1bar=one_bar,
        return_4bar=four_bar,
        atr14_price=atr,
        efficiency_4bar=efficiency,
        volume_current_base=latest.v,
        volume_baseline_prior20_base_mean=volume_baseline,
        volume_acceleration=latest.v / volume_baseline,
        persistence_bars=persistence,
    )
