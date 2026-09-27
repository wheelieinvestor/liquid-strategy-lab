"""As-of adapter around the production BTC feature and signal evaluators."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from liquid_autonomous_trader.backtesting.events import Event
from liquid_autonomous_trader.btc_inputs import build_shadow_input_v2
from liquid_autonomous_trader.btc_source import Book, Candle, FundingRate
from liquid_autonomous_trader.frozen.strategies.btc_momentum import (
    BtcMomentumConfigV1,
    BtcMomentumEngineV1,
    BtcMomentumObservationV1,
)


@dataclass(frozen=True)
class BtcDecision:
    status: str
    reasons: tuple[str, ...]
    available_us: int
    evidence: tuple[str, ...]
    features: dict | None = None
    signal: dict | None = None
    observation: dict | None = None


class BtcAdapter:
    """No IO. Returns all rejected/unsupported decisions, including warmup gaps.

    No assumed order book is inserted for candle-only history. A separate explicitly
    synthetic market-quality event may be supplied for a conditional experiment.
    The active override disables funding admission; production input construction
    still needs a funding observation, whose absence remains visible here.
    """

    def __init__(self, *, notional: Decimal = Decimal("2000")):
        self.notional = notional
        self.engine = BtcMomentumEngineV1(BtcMomentumConfigV1(funding_filter_enabled=False))

    def decide(self, events: list[Event], now_us: int) -> BtcDecision:
        visible = sorted((e for e in events if e.available_us <= now_us), key=lambda e: e.order)
        # Resolve revisions only among data actually available at this decision.
        latest = {}
        for item in visible:
            if item.instrument != "BTC" or item.kind not in {
                "candle15m",
                "book",
                "native_book",
                "funding",
            }:
                continue
            kind = "book" if item.kind == "native_book" else item.kind
            key = (item.source, item.venue, kind, item.event_us, item.sequence)
            old = latest.get(key)
            if old is None or item.revision > old.revision:
                latest[key] = item
        values = sorted(latest.values(), key=lambda e: e.order)
        candles = [e for e in values if e.kind == "candle15m"]
        books = [e for e in values if e.kind in {"book", "native_book"}]
        funding = [e for e in values if e.kind == "funding"]
        evidence = tuple(e.content_hash for e in values)
        missing = tuple(
            name
            for name, rows in (("candles", candles), ("book", books), ("funding", funding))
            if not rows
        )
        if missing:
            return BtcDecision(
                "unsupported", tuple(f"missing_{x}" for x in missing), now_us, evidence
            )
        if now_us - books[-1].event_us > 5_000_000:
            return BtcDecision("unsupported", ("stale_book",), now_us, evidence)
        venues = {e.venue for e in candles + [books[-1]]}
        if len(venues) != 1:
            return BtcDecision(
                "unsupported", ("mixed_venue_requires_proxy_adapter",), now_us, evidence
            )
        try:
            built = build_shadow_input_v2(
                [
                    Candle.model_validate(e.payload)
                    for e in sorted(candles, key=lambda e: e.event_us)
                ],
                Book.model_validate(books[-1].payload),
                FundingRate.model_validate(funding[-1].payload),
                self.notional,
            )
            if built.features.bar_end_ms * 1000 >= now_us:
                raise ValueError("bar_not_completed")
            observation = BtcMomentumObservationV1.model_validate(built.shadow_input.observation)
            signal = self.engine.evaluate(observation)
        except ValueError as error:
            # Pydantic errors can include raw values; report type, never arbitrary payloads.
            reason = str(error) if type(error) is ValueError else "invalid_market_evidence"
            return BtcDecision("unsupported", (reason,), now_us, evidence)
        quality = "observed" if all(e.quality == "observed" for e in values) else "partial"
        return BtcDecision(
            quality,
            tuple(signal.reason_codes),
            now_us,
            evidence,
            built.features.model_dump(mode="json"),
            signal.model_dump(mode="json"),
            observation.model_dump(mode="json"),
        )
