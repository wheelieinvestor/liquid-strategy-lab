"""Bounded public market inputs for exact Liquid-resolved XYZ perpetuals.

No account, broker write, fee-tier invention, or strategy activation is exposed.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from liquid_autonomous_trader.btc_source import BAR_MS, Book, Candle, _public_info
from liquid_autonomous_trader.cash_calendar import xnys_session
from liquid_autonomous_trader.desk_store import digest
from liquid_autonomous_trader.frozen.flow_mirror import FLOW_MIRROR_V1_ALLOWED_SYMBOLS
from liquid_autonomous_trader.xyz_bars import XYZBar, prove_closed_bars


class NativeBook(Book):
    coin: str


class NativeCandle(Candle):
    s: str


@dataclass(frozen=True)
class NativeMarketIdentity:
    ticker: str
    coin: str
    liquid_max_leverage: Decimal
    resolved_at: datetime
    evidence_sha256: str


def resolve_identity(ticker, liquid_payload, *, received_at):
    if ticker not in FLOW_MIRROR_V1_ALLOWED_SYMBOLS | {"NASDAQ100"}:
        raise ValueError("market_outside_approved_strategy_universe")
    if received_at.tzinfo is None:
        raise ValueError("aware_market_resolution_time_required")
    data = liquid_payload.get("ticker", {})
    expected = "xyz:XYZ100" if ticker == "NASDAQ100" else "xyz:" + ticker
    if data.get("coin") != expected:
        raise ValueError("exact_liquid_xyz_mapping_required")
    maximum = data.get("maxLeverage")
    if type(maximum) is not int or not 1 <= maximum <= 40:
        raise ValueError("liquid_market_leverage_invalid")
    return NativeMarketIdentity(
        ticker,
        expected,
        Decimal(maximum),
        received_at,
        digest({"ticker": ticker, "coin": expected, "max_leverage": maximum}),
    )


@dataclass(frozen=True)
class NativeQuote:
    identity: NativeMarketIdentity
    observed_at: datetime
    received_at: datetime
    bid: Decimal
    ask: Decimal
    quantity_step: Decimal
    maximum_leverage: Decimal
    bid_notional: Decimal
    ask_notional: Decimal
    metadata_sha256: str
    book_sha256: str
    deployer_fee_scale: Decimal
    growth_mode: bool

    @property
    def midpoint(self):
        return (self.bid + self.ask) / 2


def validate_quote(identity, metadata, raw_book, *, now):
    if now.tzinfo is None or not 0 <= (now - identity.resolved_at).total_seconds() <= 300:
        raise ValueError("liquid_market_mapping_stale")
    universe = metadata.get("universe")
    if not isinstance(universe, list) or not 1 <= len(universe) <= 1000:
        raise ValueError("native_market_inventory_invalid")
    candidates = [a for a in universe if a.get("name") == identity.coin]
    if len(candidates) != 1 or candidates[0].get("isDelisted"):
        raise ValueError("native_market_missing_duplicate_or_delisted")
    asset = candidates[0]
    decimals, maximum = asset.get("szDecimals"), asset.get("maxLeverage")
    if type(decimals) is not int or not 0 <= decimals <= 6:
        raise ValueError("native_quantity_precision_invalid")
    if type(maximum) is not int or Decimal(maximum) != identity.liquid_max_leverage:
        raise ValueError("liquid_native_leverage_mismatch")
    scale = Decimal(str(asset.get("deployerFeeScale")))
    if not scale.is_finite() or not 0 <= scale <= 3:
        raise ValueError("native_fee_scale_invalid")
    growth = asset.get("growthMode")
    if growth not in {"enabled", "disabled"}:
        raise ValueError("native_growth_mode_unknown")
    book = NativeBook.model_validate(raw_book)
    if book.coin != identity.coin:
        raise ValueError("native_book_market_mismatch")
    observed = datetime.fromtimestamp(book.time / 1000, UTC)
    if not 0 <= (now - observed).total_seconds() <= 5:
        raise ValueError("native_book_stale_or_future")
    bids, asks = book.levels
    if not bids or not asks or max(len(bids), len(asks)) > 20:
        raise ValueError("native_book_depth_invalid")
    if (
        any(a.px <= b.px for a, b in zip(bids, bids[1:]))
        or any(a.px >= b.px for a, b in zip(asks, asks[1:]))
        or bids[0].px >= asks[0].px
    ):
        raise ValueError("native_book_crossed_or_unsorted")
    return NativeQuote(
        identity,
        observed,
        now,
        bids[0].px,
        asks[0].px,
        Decimal(1).scaleb(-decimals),
        Decimal(maximum),
        sum((v.px * v.sz for v in bids), Decimal(0)),
        sum((v.px * v.sz for v in asks), Decimal(0)),
        digest(asset),
        digest(raw_book),
        scale,
        growth == "enabled",
    )


class NativePerpSource:
    def __init__(self, *, fetch=_public_info, clock=lambda: datetime.now(UTC)):
        self.fetch, self.clock = fetch, clock

    def quote(self, identity):
        metadata = self.fetch({"type": "meta", "dex": "xyz"})
        book = self.fetch({"type": "l2Book", "coin": identity.coin})
        return validate_quote(identity, metadata, book, now=self.clock())

    def xyz_bars(self):
        # Closure is evaluated at request start, not after a slow network fetch.
        started = self.clock()
        end = int(started.timestamp() * 1000)
        raw = self.fetch(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": "xyz:XYZ100",
                    "interval": "15m",
                    "startTime": end - 86400000,
                    "endTime": end,
                },
            }
        )
        return validate_xyz_bars(raw, request_started=started, now=self.clock())


def validate_xyz_bars(raw, *, request_started, now):
    if any(t.tzinfo is None for t in (request_started, now)) or request_started > now:
        raise ValueError("native_candle_request_time_invalid")
    if not isinstance(raw, list) or not 5 <= len(raw) <= 100:
        raise ValueError("native_candle_count_invalid")
    parsed = [NativeCandle.model_validate(row) for row in raw]
    for c in parsed:
        if c.s != "xyz:XYZ100" or c.t % BAR_MS or c.T != c.t + BAR_MS - 1:
            raise ValueError("native_candle_identity_or_interval_invalid")
        if not c.l <= min(c.o, c.c) <= max(c.o, c.c) <= c.h:
            raise ValueError("native_candle_geometry_invalid")
        if c.t > request_started.timestamp() * 1000:
            raise ValueError("native_future_candle")
    if any(b.t - a.t != BAR_MS for a, b in zip(parsed, parsed[1:])):
        raise ValueError("native_candle_gap_or_duplicate")
    from zoneinfo import ZoneInfo

    session = xnys_session(now.astimezone(ZoneInfo("America/New_York")).date())
    if session is None or not session.open_at <= now < session.close_at:
        raise ValueError("native_xyz_cash_session_closed")
    closed = [
        c
        for c in parsed
        if c.T <= request_started.timestamp() * 1000 - 2000
        and c.t >= session.open_at.timestamp() * 1000
    ]
    bars = [
        XYZBar(
            "xyz:XYZ100",
            datetime.fromtimestamp(c.t / 1000, UTC),
            datetime.fromtimestamp((c.T + 1) / 1000, UTC),
            c.o,
            c.h,
            c.l,
            c.c,
            c.v,
        )
        for c in closed
    ]
    proof = prove_closed_bars(bars, session, now)
    expected_end = int((now.timestamp() - 2) // 900) * 900
    if proof.latest.end_at.timestamp() != expected_end:
        raise ValueError("native_latest_settled_bar_required")
    return proof
