"""Closed, timestamped native XYZ100 bar provenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from liquid_autonomous_trader.cash_calendar import CashSession
from liquid_autonomous_trader.desk_store import digest

BAR_SCHEMA = "xyz100-closed-15m-v1"
INSTRUMENT = "xyz:XYZ100"
INTERVAL = timedelta(minutes=15)


class InvalidXYZBars(ValueError):
    pass


@dataclass(frozen=True)
class XYZBar:
    instrument: str
    start_at: datetime
    end_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if self.instrument != INSTRUMENT:
            raise InvalidXYZBars("native_xyz100_instrument_required")
        if self.start_at.tzinfo is None or self.end_at.tzinfo is None:
            raise InvalidXYZBars("bar_timestamp_must_be_aware")
        if self.end_at.astimezone(UTC) - self.start_at.astimezone(UTC) != INTERVAL:
            raise InvalidXYZBars("bar_interval_not_15m")
        if any(
            not value.is_finite()
            for value in (
                self.open,
                self.high,
                self.low,
                self.close,
                self.volume,
            )
        ):
            raise InvalidXYZBars("nonfinite_bar_value")
        if min(self.open, self.high, self.low, self.close) <= 0 or self.volume < 0:
            raise InvalidXYZBars("invalid_bar_value")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise InvalidXYZBars("invalid_ohlc_range")

    def material(self) -> dict[str, str]:
        return {
            "instrument": self.instrument,
            "start_at": self.start_at.astimezone(UTC).isoformat(),
            "end_at": self.end_at.astimezone(UTC).isoformat(),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": str(self.volume),
        }


@dataclass(frozen=True)
class XYZBarProof:
    prior: XYZBar
    latest: XYZBar
    one_hour_anchor: XYZBar
    one_hour_return: Decimal
    bar_schema: str
    bars_digest: str
    calendar_digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "instrument": INSTRUMENT,
            "prior": self.prior.material(),
            "latest": self.latest.material(),
            "one_hour_anchor": self.one_hour_anchor.material(),
            "one_hour_return": str(self.one_hour_return),
            "bar_schema": self.bar_schema,
            "bars_digest": self.bars_digest,
            "calendar_digest": self.calendar_digest,
        }


def prove_closed_bars(bars: list[XYZBar], session: CashSession, now: datetime) -> XYZBarProof:
    """Validate contiguous session-aligned bars and derive latest/prior/1h trend."""
    if now.tzinfo is None:
        raise InvalidXYZBars("now_must_be_aware")
    if not 5 <= len(bars) <= 100:
        raise InvalidXYZBars("insufficient_bars_for_one_hour_trend")
    ordered = sorted(bars, key=lambda bar: bar.end_at)
    if ordered != bars:
        raise InvalidXYZBars("bars_not_ordered")
    open_at = session.open_at.astimezone(UTC)
    close_at = session.close_at.astimezone(UTC)
    for index, bar in enumerate(bars):
        start = bar.start_at.astimezone(UTC)
        end = bar.end_at.astimezone(UTC)
        if start < open_at or end > close_at:
            raise InvalidXYZBars("bar_outside_cash_session")
        if (start - open_at) % INTERVAL:
            raise InvalidXYZBars("bar_not_session_aligned")
        if end > now.astimezone(UTC):
            raise InvalidXYZBars("forming_bar")
        if index and bars[index - 1].end_at.astimezone(UTC) != start:
            raise InvalidXYZBars("bar_gap")
    latest = bars[-1]
    prior = bars[-2]
    anchor = bars[-5]
    one_hour_return = latest.close / anchor.close - Decimal(1)
    material = {
        "bar_schema": BAR_SCHEMA,
        "calendar_digest": session.schedule_digest,
        "bars": [bar.material() for bar in bars],
    }
    return XYZBarProof(
        prior=prior,
        latest=latest,
        one_hour_anchor=anchor,
        one_hour_return=one_hour_return,
        bar_schema=BAR_SCHEMA,
        bars_digest=digest(material),
        calendar_digest=session.schedule_digest,
    )
