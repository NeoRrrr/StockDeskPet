"""US equity sessions in exchange time (weekdays, not a holiday calendar)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo

US_TIMEZONE = ZoneInfo("America/New_York")
US_SESSION_LABELS = {"pre": "盘前", "regular": "盘中", "after": "盘后", "overnight": "夜盘"}


def us_now(at: datetime | None = None) -> datetime:
    value = at or datetime.now(US_TIMEZONE)
    return value.replace(tzinfo=US_TIMEZONE) if value.tzinfo is None else value.astimezone(US_TIMEZONE)


def parse_us_time(value: str) -> datetime | None:
    try:
        # OpenD may use either HH:mm:ss.xxx or HH:mm:ss:xxx.
        text = re.sub(r"(\d{2}:\d{2}:\d{2}):(\d+)$", r"\1.\2", value.strip())
        return us_now(datetime.fromisoformat(text.replace("/", "-")))
    except (ValueError, TypeError, AttributeError):
        return None


@dataclass(frozen=True, slots=True)
class USSession:
    kind: str
    trading_date: date
    start: datetime
    end: datetime

    @property
    def label(self) -> str:
        return US_SESSION_LABELS[self.kind]

    def contains(self, at: datetime) -> bool:
        return self.start <= us_now(at) < self.end


def us_session_at(at: datetime | None = None, *, close_grace: bool = False) -> USSession | None:
    current = us_now(at)
    day = current.date()
    weekday = current.weekday()
    minute = current.hour * 60 + current.minute

    def instant(on: date, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(on, time(hour, minute), US_TIMEZONE)

    if minute >= 20 * 60 and weekday in {6, 0, 1, 2, 3}:
        tomorrow = day + timedelta(days=1)
        return USSession("overnight", tomorrow, instant(day, 20), instant(tomorrow, 4))
    if weekday < 5:
        if minute < 4 * 60:
            return USSession("overnight", day, instant(day - timedelta(days=1), 20), instant(day, 4))
        if minute < 9 * 60 + 30:
            return USSession("pre", day, instant(day, 4), instant(day, 9, 30))
        if minute < 16 * 60:
            return USSession("regular", day, instant(day, 9, 30), instant(day, 16))
        if minute < 20 * 60 or (close_grace and weekday == 4 and minute < 20 * 60 + 30):
            return USSession("after", day, instant(day, 16), instant(day, 20))
    return None
