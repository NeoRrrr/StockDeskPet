from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


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
        if self.market in {"hk_index", "cn_index", "gold"}:
            return self.code
        return f"{self.market.upper()}{self.code}"


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: StockSymbol
    name: str
    price: float
    previous_close: float
    open_price: float
    high: float
    low: float
    change: float
    change_percent: float
    volume: float
    volume_unit: str
    amount: float
    quote_time: str
    source: str
    turnover_rate: float | None = None

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


def quote_is_older(incoming: Quote, current: Quote | None) -> bool:
    """Do not let a late/undated response replace a newer market snapshot."""
    if current is None:
        return False

    def timestamp(quote: Quote) -> datetime | None:
        try:
            value = datetime.fromisoformat(quote.quote_time.replace("/", "-"))
            return value if value.tzinfo else value.replace(tzinfo=timezone(timedelta(hours=8)))
        except ValueError:
            return None

    old_time, new_time = timestamp(current), timestamp(incoming)
    if old_time is None:
        return False
    if new_time is None:
        return True
    return new_time < old_time
