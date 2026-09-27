"""Build a deterministic BTC shadow input from validated public source values."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from liquid_autonomous_trader.btc_features import BtcFeatureSetV2, build_features_v2
from liquid_autonomous_trader.btc_source import BAR_MS, Book, Candle, FundingRate
from liquid_autonomous_trader.desk_inputs import ShadowInput
from liquid_autonomous_trader.frozen.models import MarketContextV1
from liquid_autonomous_trader.frozen.strategies.btc_momentum import BtcMomentumObservationV1

MAX_FUNDING_AGE_MS = 3_600_000 + 120_000


class BookLiquidityV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: str = "btc-book-liquidity-v2"
    intended_notional_usd: Decimal = Field(gt=0)
    best_bid: Decimal = Field(gt=0)
    best_ask: Decimal = Field(gt=0)
    midpoint: Decimal = Field(gt=0)
    spread_price: Decimal = Field(gt=0)
    spread_bps: Decimal = Field(gt=0)
    bid_notional_usd: Decimal = Field(gt=0)
    ask_notional_usd: Decimal = Field(gt=0)
    depth_multiple: Decimal = Field(gt=0)


class BtcBuiltInputV2(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "btc-built-input-v2"
    features: BtcFeatureSetV2
    liquidity: BookLiquidityV2
    funding: FundingRate
    shadow_input: ShadowInput


def assess_book_v2(book: Book, intended_notional_usd: Decimal) -> BookLiquidityV2:
    if not intended_notional_usd.is_finite() or intended_notional_usd <= 0:
        raise ValueError("positive_finite_intended_notional_required")
    bids, asks = book.levels
    best_bid, best_ask = bids[0].px, asks[0].px
    midpoint = (best_bid + best_ask) / 2
    bid_notional = sum((level.px * level.sz for level in bids), Decimal(0))
    ask_notional = sum((level.px * level.sz for level in asks), Decimal(0))
    if bid_notional < intended_notional_usd or ask_notional < intended_notional_usd:
        raise ValueError("insufficient_book_for_intended_size")
    spread = best_ask - best_bid
    return BookLiquidityV2(
        intended_notional_usd=intended_notional_usd,
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint=midpoint,
        spread_price=spread,
        spread_bps=spread / midpoint * Decimal(10_000),
        bid_notional_usd=bid_notional,
        ask_notional_usd=ask_notional,
        depth_multiple=min(bid_notional, ask_notional) / intended_notional_usd,
    )


def build_shadow_input_v2(
    candles: list[Candle],
    book: Book,
    funding: FundingRate,
    intended_notional_usd: Decimal,
) -> BtcBuiltInputV2:
    features = build_features_v2(candles)
    if book.time <= features.bar_end_ms or book.time > features.bar_end_ms + BAR_MS:
        raise ValueError("book_not_in_decision_window")
    if funding.time > book.time or book.time - funding.time > MAX_FUNDING_AGE_MS:
        raise ValueError("stale_or_future_funding")
    liquidity = assess_book_v2(book, intended_notional_usd)
    observed_at = datetime.fromtimestamp(book.time / 1000, UTC)
    observation = BtcMomentumObservationV1(
        market=MarketContextV1(
            symbol="BTC",
            observed_at=observed_at,
            price=liquidity.midpoint,
            atr_15m=features.atr14_price,
            spread_price=liquidity.spread_price,
            spread_bps=liquidity.spread_bps,
            depth_multiple=liquidity.depth_multiple,
        ),
        return_15m=features.return_1bar,
        return_1h=features.return_4bar,
        trend_efficiency=features.efficiency_4bar,
        volume_acceleration=features.volume_acceleration,
        funding_rate=funding.rate_fraction_per_hour,
        persistence_bars=features.persistence_bars,
        data_complete=True,
    )
    item = ShadowInput(
        schema_version="shadow-input-v1",
        evidence_type="supplied_shadow",
        strategy="btc_momentum",
        source_id=f"btc-v2:{features.bar_end_ms}",
        observed_at=observed_at,
        candle_closed=True,
        observation=observation.model_dump(mode="json"),
    )
    return BtcBuiltInputV2(
        features=features,
        liquidity=liquidity,
        funding=funding,
        shadow_input=item,
    )
