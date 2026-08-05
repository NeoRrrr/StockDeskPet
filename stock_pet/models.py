from __future__ import annotations

from dataclasses import dataclass


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

    @property
    def direction(self) -> int:
        if self.change_percent > 0:
            return 1
        if self.change_percent < 0:
            return -1
        return 0
