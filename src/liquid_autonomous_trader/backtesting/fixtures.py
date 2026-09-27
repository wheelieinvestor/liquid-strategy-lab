"""Authored, explicitly synthetic source fixtures for reproducible stress cases.

These are not historical alerts, news, books or GEX. Their purpose is exercising
production parsing, decisions and shared admission with a known causal timeline.
"""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal as D

from liquid_autonomous_trader import jev_stop_policy as policy
from liquid_autonomous_trader.backtesting.events import Event, canonical
from liquid_autonomous_trader.backtesting.execution import SimulatedExecution
from liquid_autonomous_trader.backtesting.ledger import FeeSchedule, Instrument, Ledger, Tier
from liquid_autonomous_trader.backtesting.sources import SourceAdapters

NOW = datetime(2026, 9, 22, 15, 1, tzinfo=UTC)
US = int(NOW.timestamp() * 1_000_000)


def event(kind, instrument, payload, *, sequence=1, at=US):
    return Event(
        "authored-fixture",
        instrument,
        "hyperliquid",
        kind,
        at,
        at,
        at,
        at,
        sequence,
        0,
        "native",
        "synthetic_assumption",
        "fixture",
        canonical(payload),
    )


def market_events(ticker="NVDA", coin="xyz:NVDA", maximum=20):
    rows = [
        event("market_identity", ticker, {"ticker": {"coin": coin, "maxLeverage": maximum}}),
        event(
            "native_metadata",
            coin.split(":")[0] if ":" in coin else "native",
            {
                "universe": [
                    {
                        "name": coin,
                        "szDecimals": 2,
                        "maxLeverage": maximum,
                        "deployerFeeScale": "1",
                        "growthMode": "disabled",
                    }
                ]
            },
        ),
        event(
            "native_book",
            coin,
            {
                "coin": coin,
                "time": US // 1000,
                "levels": [
                    [{"px": "100.09", "sz": "1000", "n": 1}],
                    [{"px": "100.11", "sz": "1000", "n": 1}],
                ],
            },
        ),
    ]
    end = int(NOW.replace(minute=0).timestamp() * 1000)
    candles = []
    for i in range(20):
        price = D("100.1") - D(19 - i) * D(".02")
        if coin == "xyz:XYZ100" and i == 18:
            price = D("100")  # actual support touch before the completed-bar bounce
        start = end - (20 - i) * 900000
        candles.append(
            {
                "s": coin,
                "i": "15m",
                "t": start,
                "T": start + 899999,
                "o": str(price),
                "h": str(price + D(".05")),
                "l": str(price - D(".05")),
                "c": str(price),
                "v": "100",
                "n": 10,
            }
        )
    rows.append(event("native_candles15m", coin, candles))
    return rows


def flow_payload():
    return {
        "source": "flow_show_deliveries_v1",
        "source_contract": "confirmed_directional_entry_only",
        "captured_at": NOW.isoformat(),
        "capture_receipt_sha256": "a" * 64,
        "signal": {
            "delivery_event_id": "fixture-delivery",
            "flow_score_id": "fixture-score",
            "flow_alert_id": "fixture-alert",
            "delivered_at": NOW.isoformat(),
            "discord_channel_id": "1503488321236631612",
            "discord_message_id": "fixture",
            "ticker": "NVDA",
            "direction": "long",
            "source_underlying_price": "100",
            "score": "90",
            "scoring_version": "v1.0.14-confirmed-directional",
        },
    }


def cramer_payload(source_id="123", statement=NOW):
    return {
        "model": "original-classifier-fixture",
        "prompt_sha": "c" * 64,
        "body": {
            "post": {
                "source_id": source_id,
                "author_id": "456",
                "handle": "jimcramer",
                "text": "Buy $BTC now",
                "published_at": NOW.isoformat(),
                "discovered_at": NOW.isoformat(),
            },
            "classification": {
                "source_id": source_id,
                "calls": [
                    {
                        "ticker": "BTC",
                        "issuer": "Bitcoin",
                        "stance": "bullish",
                        "confidence": 0.95,
                        "speaker_is_cramer": True,
                        "explicit": True,
                        "conditional": False,
                        "historical": False,
                        "evidence": "Buy $BTC now",
                        "statement_at": statement.isoformat(),
                        "uncertainty": "",
                    }
                ],
            },
        },
    }


def gamma_events():
    return [
        *market_events("NASDAQ100", "xyz:XYZ100", 30),
        event(
            "gamma_matrix",
            "QQQ",
            {
                "symbol": "QQQ",
                "expirations": [
                    {
                        "symbol": "QQQ",
                        "expiration": "2026-09-22",
                        "updatedAt": NOW.isoformat(),
                        "spotPrice": "100.1",
                        "zeroGammaLevel": "100.5",
                        "netGex": {"totalGexDollars": "10"},
                        "strikes": [99, 100, 101, 102],
                        "gexByStrike": {
                            str(k): {"totalGexDollars": "10"} for k in [99, 100, 101, 102]
                        },
                    }
                ],
            },
        ),
        event("gamma_spot", "QQQ", {"price": "100.1", "updatedAt": NOW.isoformat()}),
    ]


def adapters():
    specs = {
        s: Instrument(
            s, D(".01"), D(".001"), D(10), (Tier(D(0), D(m)),), "fixture", "synthetic_assumption"
        )
        for s, m in [("BTC", 40), ("xyz:NVDA", 20), ("xyz:XYZ100", 30)]
    }
    fees = FeeSchedule(
        "fixture",
        "research",
        "assumed",
        "none",
        0,
        10**20,
        D(".00095"),
        D("0"),
        "synthetic_assumption",
    )
    return SourceAdapters(SimulatedExecution(Ledger(D(1000), specs), fees))


def jev_inputs():
    position = {
        "symbol": "BTC",
        "side": "long",
        "entry": "100",
        "quantity": "1",
        "stop": "90",
        "leverage": "40",
    }
    base = {
        "schema_version": policy.VERSION,
        "model": policy.MODEL,
        "strategy": "btc_momentum",
        "as_of": NOW.isoformat(),
        "position": position,
        "original": {
            "entry": "100",
            "original_stop": "90",
            "risk_price": "10",
            "price_risk_budget": "50",
            "opened_at": "2026-09-22T14:00:00+00:00",
            "entry_plan": {},
        },
        "plan": {"target": "125"},
        "context": {},
        "review_r": "1",
        "mark_price": "110",
        "peak_r": "1",
        "age_seconds": 3660,
        "previous": None,
        "confirmed_reductions": [],
    }
    market = {
        "symbol": "BTC",
        "bid": "110",
        "ask": "110.01",
        "observed_at": NOW.isoformat(),
        "price_step": ".01",
        "quantity_step": ".01",
        "maximum_leverage": "40",
        "atr14_price": "2",
        "support_prior20": "104",
        "resistance_prior20": "112",
        "return_1bar": ".01",
        "return_4bar": ".02",
        "bar_end_ms": int(NOW.replace(minute=0).timestamp() * 1000) - 1,
    }
    return base, market


def jev_response(request, action="HOLD", approve=True):
    def answer(choice, options):
        return {"choice": choice, "probabilities": {k: int(k == choice) for k in options}}

    return {
        "model": policy.MODEL,
        "answers": {
            "action": answer(action, policy.ACTION),
            **{
                "price_" + k: answer("approve" if approve else "reject", policy.PRICE_CRITERIA)
                for k in request.state["stop_candidates"]
            },
        },
    }


def btc_entry_events():
    """Authored narrow-range persistent momentum that passes actual BTC sizing."""
    end = int(NOW.replace(minute=0).timestamp() * 1000)
    rows = []
    for i in range(25):
        close = D("92.9") + D(i) * D(".3")
        start = end - (25 - i) * 900000
        payload = {
            "s": "BTC",
            "i": "15m",
            "t": start,
            "T": start + 899999,
            "o": str(close - D(".3")),
            "h": str(close + D(".02")),
            "l": str(close - D(".32")),
            "c": str(close),
            "v": "200" if i == 24 else "100",
            "n": 10,
        }
        rows.append(
            Event(
                "authored-fixture",
                "BTC",
                "hyperliquid",
                "candle15m",
                (start + 899999) * 1000,
                (start + 899999) * 1000,
                US,
                US,
                i,
                0,
                "native",
                "synthetic_assumption",
                "fixture",
                canonical(payload),
            )
        )
    native = next(e for e in market_events("BTC", "BTC", 40) if e.kind == "native_book")
    rows.append(replace(native, kind="book"))
    rows.append(
        event(
            "funding",
            "BTC",
            {"coin": "BTC", "time": US // 1000, "fundingRate": "0.00001", "premium": "0"},
        )
    )
    return rows


def portfolio_events(*, separate_cramer=True, include_btc=True):
    rows = [
        *market_events(),
        *gamma_events(),
        *market_events("BTC", "BTC", 40),
        event("flow_delivery", "NVDA", flow_payload()),
    ]
    if separate_cramer:
        payload = cramer_payload()
        payload["body"]["post"]["text"] = "Buy $ETH now"
        call = payload["body"]["classification"]["calls"][0]
        call.update(ticker="ETH", issuer="Ethereum", evidence="Buy $ETH now")
        rows.extend(market_events("ETH", "ETH", 40))
        rows.append(event("cramer_classification", "ETH", payload))
    else:
        rows.append(event("cramer_classification", "BTC", cramer_payload()))
    if include_btc:
        rows.extend(btc_entry_events())
    for dex in ("xyz", "native"):
        metadata = [e for e in rows if e.kind == "native_metadata" and e.instrument == dex]
        rows = [e for e in rows if not (e.kind == "native_metadata" and e.instrument == dex)]
        rows.append(
            replace(
                metadata[0],
                payload_json=canonical(
                    {"universe": [asset for e in metadata for asset in e.payload["universe"]]}
                ),
            )
        )
    following = []
    for row in rows:
        if row.kind == "native_book":
            p = row.payload
            p["time"] += 10000
            following.append(event("native_book", row.instrument, p, at=US + 10000000, sequence=2))
    return rows, following
