from decimal import Decimal

from liquid_autonomous_trader.backtesting.btc import BtcAdapter
from liquid_autonomous_trader.backtesting.events import Event, canonical
from liquid_autonomous_trader.btc_inputs import build_shadow_input_v2
from liquid_autonomous_trader.btc_source import BAR_MS, Book, Candle, FundingRate
from liquid_autonomous_trader.frozen.strategies.btc_momentum import (
    BtcMomentumConfigV1,
    BtcMomentumEngineV1,
    BtcMomentumObservationV1,
)


def fixture():
    bars = [
        Candle(
            t=i * BAR_MS,
            T=(i + 1) * BAR_MS - 1,
            s="BTC",
            i="15m",
            o=100 + i,
            h=102 + i,
            l=99 + i,
            c=101 + i,
            v=20 if i == 24 else 10,
            n=1,
        )
        for i in range(25)
    ]
    time = 25 * BAR_MS
    book = Book(
        coin="BTC",
        time=time,
        levels=([{"px": "124.99", "sz": "1000", "n": 1}], [{"px": "125.01", "sz": "1000", "n": 1}]),
    )
    funding = FundingRate(coin="BTC", time=time, fundingRate="0.00001", premium="0")
    events = []
    for kind, stamp, model in [
        *(("candle15m", b.T, b) for b in bars),
        ("book", time, book),
        ("funding", time, funding),
    ]:
        events.append(
            Event(
                source="fixture",
                instrument="BTC",
                venue="hyperliquid",
                kind=kind,
                event_us=stamp * 1000,
                published_us=stamp * 1000,
                available_us=(stamp + 1) * 1000,
                retrieved_us=(stamp + 1) * 1000,
                sequence=0,
                revision=0,
                units="USD/base",
                quality="observed",
                reference="fixture:btc",
                payload_json=canonical(model.model_dump(mode="json")),
            )
        )
    return bars, book, funding, events, (time + 1) * 1000


def test_adapter_matches_actual_production_input_and_signal():
    bars, book, funding, events, now = fixture()
    built = build_shadow_input_v2(bars, book, funding, Decimal("2000"))
    expected = BtcMomentumEngineV1(BtcMomentumConfigV1(funding_filter_enabled=False)).evaluate(
        BtcMomentumObservationV1.model_validate(built.shadow_input.observation)
    )
    result = BtcAdapter().decide(events, now)
    assert result.features == built.features.model_dump(mode="json")
    assert result.signal == expected.model_dump(mode="json")
    assert result.signal["action"] == "enter"


def test_future_book_and_missing_depth_cannot_authorize_a_signal():
    _, _, _, events, now = fixture()
    adapter = BtcAdapter()
    candles_only = [e for e in events if e.kind == "candle15m"]
    assert "missing_book" in adapter.decide(candles_only, now).reasons
    assert "missing_book" in adapter.decide(events, now - 1).reasons
    assert adapter.decide(events, now - 1) == adapter.decide(candles_only, now - 1)


def test_old_book_is_not_reused_and_venues_cannot_be_silently_mixed():
    from dataclasses import replace

    _, _, _, events, now = fixture()
    assert BtcAdapter().decide(events, now + 5_000_001).reasons == ("stale_book",)
    mixed = [replace(e, venue="binance") if e.kind == "candle15m" else e for e in events]
    assert BtcAdapter().decide(mixed, now).reasons == ("mixed_venue_requires_proxy_adapter",)
