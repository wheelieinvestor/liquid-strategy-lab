"""Authenticated archive plus native prices, with strict historical quote matching."""

from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from liquid_autonomous_trader.cash_calendar import xnys_session
from liquid_autonomous_trader.desk_store import digest
from liquid_autonomous_trader.frozen.strategies.xyz100_gex import (
    XYZ100GexConfigV1,
    XYZ100GexEngineV1,
    XYZ100GexObservationV1,
)
from liquid_autonomous_trader.gamma_projection import GammaProjection
from liquid_autonomous_trader.native_perp_source import (
    NativeCandle,
    resolve_identity,
    validate_quote,
    validate_xyz_bars,
)
from liquid_autonomous_trader.xyz_mandate import XYZ_MAX_ORDER_NOTIONAL_USD
from liquid_autonomous_trader.xyz_proxy import prepare_shadow


class GammaLiveSource:
    def __init__(self, db, read, native, archive, *, clock=lambda: datetime.now(UTC), sampler=None):
        self.db, self.read, self.native, self.archive, self.clock = db, read, native, archive, clock
        self.projection = GammaProjection(db)
        self.engine = XYZ100GexEngineV1(XYZ100GexConfigV1())
        self.identity = self.metadata = self.bars = None
        self.sampler = sampler
        self.current_quotes = deque(maxlen=600)
        self.consumed = lambda key: False
        self.latest_exit_context = None
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS gamma_native_quotes(observed_at TEXT PRIMARY KEY,"
            "price TEXT NOT NULL,body_hash TEXT NOT NULL,received_at TEXT NOT NULL)"
        )

    def sample_quote(self):
        now = self.clock()
        if self.identity is None or (now - self.identity.resolved_at).total_seconds() > 240:
            payload = self.read.call_read_tool("analyze_market", {"symbol": "NASDAQ100"})
            if payload.is_error or not isinstance(payload.structured_content, dict):
                raise ValueError("gamma_liquid_market_unavailable")
            self.identity = resolve_identity(
                "NASDAQ100", payload.structured_content, received_at=self.clock()
            )
            self.metadata = self.native.fetch({"type": "meta", "dex": "xyz"})
        if self.sampler is not None:
            self.sampler.configure(self.identity, self.metadata)
        raw = self.native.fetch({"type": "l2Book", "coin": "xyz:XYZ100"})
        quote = validate_quote(self.identity, self.metadata, raw, now=self.clock())
        self._record_quote(quote)
        self._collect_samples()
        return quote

    def _collect_samples(self):
        if self.sampler is not None:
            for quote in self.sampler.drain():
                self._record_quote(quote)

    def _record_quote(self, quote):
        stamp = quote.observed_at.isoformat()
        existing = self.db.execute(
            "SELECT price,body_hash FROM gamma_native_quotes WHERE observed_at=?", (stamp,)
        ).fetchone()
        if existing and (
            Decimal(existing[0]) != quote.midpoint or existing[1] != quote.book_sha256
        ):
            raise ValueError("gamma_native_quote_revision_conflict")
        self.db.execute(
            "INSERT OR IGNORE INTO gamma_native_quotes VALUES(?,?,?,?)",
            (stamp, str(quote.midpoint), quote.book_sha256, quote.received_at.isoformat()),
        )
        self.current_quotes.append(quote)

    def __call__(self):
        now = self.clock()
        session = xnys_session(now.astimezone(ZoneInfo("America/New_York")).date())
        if session is None or not session.open_at <= now < session.close_at:
            raise ValueError("gamma_cash_session_closed")
        quote = self.sample_quote()
        bar_end = datetime.fromtimestamp(int((now.timestamp() - 2) // 900) * 900, UTC)
        if self.consumed("xyz-v1:" + bar_end.isoformat()):
            return None
        # Capture native quotes even when the slower QQQ collector is between updates.
        values = self.archive.read(self.clock())
        self._collect_samples()
        now = self.clock()
        bar_end = datetime.fromtimestamp(int((now.timestamp() - 2) // 900) * 900, UTC)
        matrix = values["matrix"]
        record, provenance = self.projection.project(
            matrix["result"],
            raw_sha256=matrix["raw_sha256"],
            fetched_at=matrix["fetched_at"],
            now=now,
            bar_end=bar_end,
            freeze=False,
        )
        refs = self.db.execute(
            "SELECT observed_at,price,body_hash FROM gamma_native_quotes "
            "ORDER BY observed_at DESC LIMIT 600"
        ).fetchall()
        refs = [
            r
            for r in refs
            if abs((datetime.fromisoformat(r[0]) - record.source_timestamp).total_seconds()) <= 2
        ]
        if not refs:
            raise ValueError("gamma_synchronized_native_reference_missing")
        ref = min(
            refs,
            key=lambda r: (
                abs((datetime.fromisoformat(r[0]) - record.source_timestamp).total_seconds()),
                r[0],
            ),
        )
        if self.bars is None or self.bars[0].latest.end_at != bar_end:
            started = self.clock()
            end = int(started.timestamp() * 1000)
            raw = self.native.fetch(
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
            proof = validate_xyz_bars(raw, request_started=started, now=self.clock())
            closed = [NativeCandle.model_validate(c) for c in raw if c["T"] <= end - 2000]
            if len(closed) < 15:
                raise ValueError("gamma_native_atr_history_missing")
            ranges = [
                max(b.h - b.l, abs(b.h - a.c), abs(b.l - a.c))
                for a, b in zip(closed[-15:], closed[-14:])
            ]
            self.bars = proof, sum(ranges) / 14
        proof, atr = self.bars
        spot = values["spot"]
        spot_time = datetime.fromisoformat(spot["result"]["updatedAt"].replace("Z", "+00:00"))
        self._collect_samples()
        candidates = [
            q
            for q in self.current_quotes
            if abs((q.observed_at - spot_time).total_seconds()) <= 2
            and 0 <= (self.clock() - q.observed_at).total_seconds() <= 5
        ]
        if not candidates:
            raise ValueError("gamma_current_synchronized_quote_missing")
        quote = min(
            candidates,
            key=lambda q: (
                abs((q.observed_at - spot_time).total_seconds()),
                -q.observed_at.timestamp(),
            ),
        )
        spread = quote.ask - quote.bid
        payload = {
            "schema_version": "xyz-qqq-proxy-v1",
            "record": record.model_dump(mode="json"),
            "reference": {
                "source_symbol": "QQQ",
                "market_id": "xyz:XYZ100",
                "qqq_price": provenance["spot"],
                "xyz_price": ref[1],
                "qqq_observed_at": record.source_timestamp.isoformat(),
                "xyz_observed_at": ref[0],
                "evidence_sha256": digest({"gamma": provenance, "native": tuple(ref)}),
            },
            "session": {
                "opens_at": session.open_at.isoformat(),
                "closes_at": session.close_at.isoformat(),
                "calendar_evidence_sha256": session.schedule_digest,
            },
            "current_qqq_price": str(spot["result"]["price"]),
            "current_qqq_observed_at": spot_time.isoformat(),
            "candle_closed": True,
            "observation": {
                "market": {
                    "symbol": "XYZ100",
                    "observed_at": quote.observed_at.isoformat(),
                    "price": str(quote.midpoint),
                    "atr_15m": str(atr),
                    "spread_price": str(spread),
                    "spread_bps": str(spread / quote.midpoint * 10000),
                    "depth_multiple": str(
                        min(quote.bid_notional, quote.ask_notional) / XYZ_MAX_ORDER_NOTIONAL_USD
                    ),
                },
                "prior_close": str(proof.prior.close),
                "latest_close": str(proof.latest.close),
                "trend_1h": "long"
                if proof.one_hour_return > 0
                else "short"
                if proof.one_hour_return < 0
                else "none",
            },
        }
        shadow = prepare_shadow(payload, self.clock())
        observation = XYZ100GexObservationV1.model_validate(shadow.observation)
        self.latest_exit_context = observation.gex.model_dump(mode="json")
        signal = self.engine.evaluate(observation)
        if not self.db.execute(
            "SELECT 1 FROM gamma_bar_geometry WHERE bar_end=?", (bar_end.isoformat(),)
        ).fetchone():
            self.projection.freeze(record, provenance)
        return "xyz-v1:" + bar_end.isoformat(), signal, observation, quote, provenance
