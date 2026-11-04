from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from .us_sessions import US_TIMEZONE, US_SESSION_LABELS, parse_us_time, us_now, us_session_at


@dataclass(frozen=True, slots=True)
class StockSymbol:
    raw: str
    provider_symbol: str
    code: str
    market: str
    market_label: str
    currency: str

    @property
    def display_code(self) -> str:
        if self.market == "hk":
            return f"{self.code}.HK"
        if self.market == "us":
            return f"{self.code.removeprefix('US.')}.US"
        if self.market in {"hk_index", "cn_index", "us_index", "gold"}:
            return self.code
        return f"{self.market.upper()}{self.code}"


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: StockSymbol
    name: str
    price: float
    previous_close: float
    open_price: float | None
    high: float | None
    low: float | None
    change: float
    change_percent: float
    volume: float | None
    volume_unit: str
    amount: float | None
    quote_time: str
    source: str
    turnover_rate: float | None = None
    session: str = ""
    session_date: str = ""
    session_note: str = ""

    @property
    def direction(self) -> int:
        if self.change_percent > 0:
            return 1
        if self.change_percent < 0:
            return -1
        return 0


@dataclass(frozen=True, slots=True)
class StockSearchResult:
    symbol: StockSymbol
    name: str


def us_quote_is_current(quote: Quote, at: datetime | None = None) -> bool:
    session = us_session_at(at, close_grace=True)
    stamp = parse_us_time(quote.quote_time)
    return bool(
        session and stamp and session.kind == (quote.session or "regular")
        and session.contains(stamp) and stamp <= us_now(at) + timedelta(seconds=5)
    )


def quote_session_label(quote: Quote, at: datetime | None = None) -> str:
    if quote.symbol.market != "us":
        return ""
    kind = quote.session or "regular"
    current = us_session_at(at, close_grace=True)
    if kind == "regular" and (not current or current.kind != "regular"):
        return "常规参考" if current else "常规·休市"
    label = US_SESSION_LABELS.get(kind, "常规")
    if not us_quote_is_current(quote, at):
        return f"{label}·历史"
    stamp = parse_us_time(quote.quote_time)
    if stamp and (us_now(at) - stamp).total_seconds() > 120:
        return f"{label}·旧"
    return label


def quote_is_older(incoming: Quote, current: Quote | None) -> bool:
    """Do not let a late/undated response replace a newer market snapshot."""
    if current is None:
        return False

    def timestamp(quote: Quote) -> datetime | None:
        if quote.symbol.market in {"us", "us_index"}:
            return parse_us_time(quote.quote_time)
        try:
            value = datetime.fromisoformat(quote.quote_time.replace("/", "-"))
            zone = timezone(timedelta(hours=8))
            return value if value.tzinfo else value.replace(tzinfo=zone)
        except ValueError:
            return None

    old_time, new_time = timestamp(current), timestamp(incoming)
    if old_time is None:
        return False
    if new_time is None:
        return True
    return new_time < old_time
