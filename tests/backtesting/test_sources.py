from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace as NS

import pytest

from liquid_autonomous_trader.backtesting.admission import admit
from liquid_autonomous_trader.backtesting.events import Event, canonical
from liquid_autonomous_trader.backtesting.execution import SimulatedExecution
from liquid_autonomous_trader.backtesting.ledger import FeeSchedule, Instrument, Ledger, Tier
from liquid_autonomous_trader.backtesting.sources import CapturedInputs, SourceAdapters
from liquid_autonomous_trader.flow_market_preparation import prepare_flow_market
from liquid_autonomous_trader.native_perp_source import resolve_identity

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


def test_flow_real_source_entry_parity_epoch_and_future_exclusion():
    a = adapters()
    a.inputs.advance(US, [*market_events(), event("flow_delivery", "NVDA", flow_payload())])
    bundle = a.flow()
    assert not bundle["reasons"]
    request = bundle["request"]
    identity = resolve_identity("NVDA", market_events()[0].payload, received_at=NOW)
    prepared = prepare_flow_market(a.flow.native.quote(identity), side="long", now=NOW)
    assert request.requested_notional == prepared.submitted_notional
    assert request.selected_leverage == 10
    assert admit(a.account.sim.ledger, request, US).quantity == prepared.quantity
    assert (
        a.account.sim.submit(
            bundle["decision_id"], request, US, mode="cross", expires_us=US + 10000000
        )
        == "accepted"
    )
    a.account.sync()
    assert a.flow()["request"] is None  # conservative epoch claim survives pending execution
    with pytest.raises(ValueError, match="future_source_event"):
        a.inputs.advance(
            US, [replace(market_events()[0], available_us=US + 1, retrieved_us=US + 1)]
        )
    a.close()


def test_stale_flow_consumes_local_cursor_and_preserves_research_state():
    a = adapters()
    a.inputs.advance(US + 301000000, [event("flow_delivery", "NVDA", flow_payload())])
    assert a.flow() is None
    checkpoint = a.source_state()
    b = adapters()
    b.restore_source_state(checkpoint)
    assert b.source_state() == checkpoint
    a.close()
    b.close()


def test_full_gamma_source_uses_production_projection_and_real_calendar():
    a = adapters()
    a.inputs.advance(US, gamma_events())
    bundle = a.gamma()
    assert bundle["strategy"].value == "xyz100_gex"
    assert bundle["plan"]["entry_observation"]["gex"]["source_underlying"] == "QQQ"
    assert bundle["plan"]["provenance"]["expiration"] == "2026-09-22"
    # This path includes expiry selection, synchronized projection, native closed
    # bars and production signal evaluation, even when the signal is a hold.
    assert bundle["request"] is not None
    assert bundle["request"].selected_leverage == 30
    assert admit(a.account.sim.ledger, bundle["request"], US).quantity > 0
    a.inputs.advance(int((NOW + timedelta(days=4)).timestamp() * 1000000))
    with pytest.raises(ValueError, match="cash_session_closed"):
        a.gamma()
    a.close()


def test_cramer_inversion_original_statement_expiry_and_reposts():
    a = adapters()
    a.inputs.advance(
        US,
        [*market_events("BTC", "BTC", 40), event("cramer_classification", "BTC", cramer_payload())],
    )
    bundles = []

    def accept(bundle):
        bundles.append(bundle)
        return {"result": "research_captured"}

    assert a.cramer.next_entry(NS(_entry=accept))["result"] == "research_captured"
    assert bundles[0]["request"].side == "short"
    assert bundles[0]["request"].requested_notional <= 500
    assert admit(a.account.sim.ledger, bundles[0]["request"], US).quantity > 0
    a.inputs.advance(
        US + 60000000,
        [
            event(
                "cramer_classification", "BTC", cramer_payload("124"), sequence=2, at=US + 60000000
            ),
            event(
                "cramer_classification",
                "BTC",
                cramer_payload("125", NOW - timedelta(hours=3)),
                sequence=3,
                at=US + 60000000,
            ),
        ],
    )
    a.cramer.ingest()
    reasons = {r[0] for r in a.account.db.execute("SELECT reason FROM cramer_v2_signals")}
    assert {"duplicate_statement", "source_expired"} <= reasons
    a.close()


def test_source_capabilities_cannot_dispatch_even_with_host_credentials(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network_or_live_transport_reached")

    monkeypatch.setenv("LIQUID_API_KEY", "fixture-not-a-secret")
    monkeypatch.setattr("socket.socket.connect", forbidden)
    a = adapters()
    a.inputs.advance(US, [*market_events(), event("flow_delivery", "NVDA", flow_payload())])
    assert a.flow()["request"] is not None
    with pytest.raises(ValueError, match="capability_unavailable"):
        a.inputs.call_read_tool("execute_order", {"symbol": "NVDA"})
    with pytest.raises(ValueError, match="read_unavailable"):
        a.inputs.fetch({"type": "clearinghouseState"})
    assert not hasattr(a.account, "transport")
    assert not hasattr(a.inputs, "call_write_tool")
    a.close()


def test_revision_dedupe_and_missing_gex_are_visible():
    inputs = CapturedInputs()
    row = gamma_events()[-2]
    inputs.advance(US, [row, row])
    assert len(inputs.rows("gamma_matrix")) == 1
    a = adapters()
    a.inputs.advance(US, market_events("NASDAQ100", "xyz:XYZ100", 30))
    with pytest.raises(ValueError, match="missing_gamma_matrix"):
        a.gamma()
    a.close()
