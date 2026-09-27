"""Exact Liquid/native joins, including the approved oil and U.S. market meanings."""

import re
from datetime import datetime
from decimal import Decimal

from liquid_autonomous_trader.cramer_instruments import (
    MACRO_COINS,
    explicit_ticker_grounded,
    macro_grounded,
    named_issuer_grounded,
)
from liquid_autonomous_trader.desk_store import digest
from liquid_autonomous_trader.native_perp_source import (
    NativeCandle,
    NativeMarketIdentity,
    validate_quote,
)


def valid_coin(coin):
    return isinstance(coin, str) and re.fullmatch(
        r"(?:[a-z][a-z0-9]{0,19}:)?[A-Z][A-Z0-9.]{0,19}", coin
    )


def resolve_cramer_identity(post, call, payload, now):
    data = payload.get("ticker", {})
    coin = data.get("coin")
    if not valid_coin(coin) or coin.split(":")[-1] != call.ticker:
        raise ValueError("cramer_exact_market_mapping_required")
    if call.ticker in MACRO_COINS:
        if coin != MACRO_COINS[call.ticker] or not macro_grounded(post, call):
            raise ValueError("cramer_macro_market_mapping_required")
    maximum = data.get("maxLeverage")
    if type(maximum) is not int or not 10 <= maximum <= 100:
        raise ValueError("cramer_ten_x_unavailable")
    explicit_ticker = explicit_ticker_grounded(post, call)
    named_issuer = named_issuer_grounded(post, call, data)
    if not explicit_ticker and not named_issuer and not macro_grounded(post, call):
        raise ValueError("cramer_instrument_not_grounded")
    return NativeMarketIdentity(call.ticker, coin, Decimal(maximum), now, digest(data))


class CramerMarketData:
    def __init__(self, client, native, *, clock):
        self.client, self.native, self.clock = client, native, clock

    def resolve(self, post, call):
        result = self.client.call_read_tool("analyze_market", {"symbol": call.ticker})
        if result.is_error or not isinstance(result.structured_content, dict):
            raise ValueError("cramer_liquid_market_unavailable")
        return resolve_cramer_identity(post, call, result.structured_content, self.clock())

    def quote(self, identity):
        dex = identity.coin.split(":")[0] if ":" in identity.coin else ""
        metadata = self.native.fetch({"type": "meta", "dex": dex})
        # Costs are informational. These placeholders only satisfy the legacy
        # NativeQuote representation; the Cramer plan explicitly marks fees unknown.
        metadata = {
            **metadata,
            "universe": [
                {
                    **a,
                    "deployerFeeScale": a.get("deployerFeeScale", "0"),
                    "growthMode": a.get("growthMode", "disabled"),
                }
                for a in metadata.get("universe", [])
            ],
        }
        book = self.native.fetch({"type": "l2Book", "coin": identity.coin})
        return validate_quote(identity, metadata, book, now=self.clock())

    def atr(self, coin):
        if not valid_coin(coin):
            raise ValueError("cramer_invalid_native_market")
        started = self.clock()
        end = int(started.timestamp() * 1000)
        raw = self.native.fetch(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": "15m",
                    "startTime": end - 86400000,
                    "endTime": end,
                },
            }
        )
        return closed_atr(raw, coin, started, self.clock())


def closed_atr(raw, coin, started, now):
    end = int(started.timestamp() * 1000)
    if not isinstance(raw, list) or not 15 <= len(raw) <= 100:
        raise ValueError("cramer_atr_history_missing")
    rows = [NativeCandle.model_validate(c) for c in raw]
    if any(
        c.s != coin
        or c.t % 900000
        or c.T != c.t + 899999
        or c.t > end
        or not c.l <= min(c.o, c.c) <= max(c.o, c.c) <= c.h
        for c in rows
    ):
        raise ValueError("cramer_candle_invalid")
    if any(b.t - a.t != 900000 for a, b in zip(rows, rows[1:])):
        raise ValueError("cramer_candle_gap")
    closed = [c for c in rows if c.T <= end - 2000]
    if len(closed) < 15 or closed[-1].T + 1 != int((started.timestamp() - 2) // 900) * 900000:
        raise ValueError("cramer_candle_stale")
    if not 0 <= (now - started).total_seconds() <= 10:
        raise ValueError("cramer_candle_fetch_stale")
    atr = (
        sum(
            max(b.h - b.l, abs(b.h - a.c), abs(b.l - a.c))
            for a, b in zip(closed[-15:], closed[-14:])
        )
        / 14
    )
    if not atr.is_finite() or atr <= 0:
        raise ValueError("cramer_atr_invalid")
    return atr


def restore_markets(executions):
    executions.db.execute(
        "CREATE TABLE IF NOT EXISTS cramer_v2_markets("
        "symbol TEXT PRIMARY KEY,ticker TEXT NOT NULL,evidence TEXT NOT NULL,"
        "resolved_at TEXT NOT NULL)"
    )
    rows = executions.db.execute("SELECT * FROM cramer_v2_markets").fetchall()
    for row in rows:
        if not valid_coin(row["symbol"]) or row["symbol"].split(":")[-1] != row["ticker"]:
            raise ValueError("cramer_saved_market_identity_invalid")
        datetime.fromisoformat(row["resolved_at"])
    return {row["symbol"] for row in rows}
