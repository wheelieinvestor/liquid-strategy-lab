"""Authoritative XNYS cash-session boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib.metadata import version

import exchange_calendars as xcals

from liquid_autonomous_trader.desk_store import digest

CALENDAR = "XNYS"
SCHEDULE_SCHEMA = "xnys-session-v1"


class CalendarDateOutOfRange(ValueError):
    pass


@dataclass(frozen=True)
class CashSession:
    session_date: date
    open_at: datetime
    close_at: datetime
    calendar: str
    calendar_version: str
    schedule_schema: str
    schedule_digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            "session_date": self.session_date.isoformat(),
            "open_at": self.open_at.isoformat(),
            "close_at": self.close_at.isoformat(),
            "calendar": self.calendar,
            "calendar_version": self.calendar_version,
            "schedule_schema": self.schedule_schema,
            "schedule_digest": self.schedule_digest,
        }


def xnys_session(session_date: date) -> CashSession | None:
    """Return exact UTC XNYS bounds, or None for a known non-session date."""
    calendar = xcals.get_calendar(CALENDAR)
    first = calendar.first_session.date()
    last = calendar.last_session.date()
    if not first <= session_date <= last:
        raise CalendarDateOutOfRange(
            f"calendar_date_out_of_range:{session_date.isoformat()}:{first}:{last}"
        )
    label = session_date.isoformat()
    if not calendar.is_session(label):
        return None
    open_at = calendar.session_open(label).to_pydatetime().astimezone(UTC)
    close_at = calendar.session_close(label).to_pydatetime().astimezone(UTC)
    calendar_version = version("exchange-calendars")
    material = {
        "schedule_schema": SCHEDULE_SCHEMA,
        "calendar": CALENDAR,
        "calendar_version": calendar_version,
        "session_date": label,
        "open_at": open_at.isoformat(),
        "close_at": close_at.isoformat(),
    }
    return CashSession(
        session_date=session_date,
        open_at=open_at,
        close_at=close_at,
        calendar=CALENDAR,
        calendar_version=calendar_version,
        schedule_schema=SCHEDULE_SCHEMA,
        schedule_digest=digest(material),
    )
