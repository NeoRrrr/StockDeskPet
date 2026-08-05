from __future__ import annotations

import argparse
import re
from collections.abc import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from .models import Quote, StockSearchResult, StockSymbol
from .symbols import SymbolError, normalize_symbol


class QuoteError(RuntimeError):
    pass


Transport = Callable[[str], bytes]


class TencentQuoteProvider:
    """Small, dependency-free Tencent quote client for one symbol at a time.

    The endpoint is a public web quote interface rather than a contracted market
    data feed. Values can be delayed and must not be treated as trading-system data.
    """

    endpoint = "https://qt.gtimg.cn/q={symbol}"
    search_endpoint = "https://smartbox.gtimg.cn/s3/?q={query}&t=all"

    def __init__(self, transport: Transport | None = None, timeout: float = 8.0) -> None:
        self._transport = transport or self._download
        self.timeout = timeout

    def fetch(self, raw_symbol: str) -> Quote:
        try:
            symbol = normalize_symbol(raw_symbol)
        except SymbolError:
            raise

        url = self.endpoint.format(symbol=symbol.provider_symbol)
        try:
            payload = self._transport(url).decode("gb18030", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise QuoteError(f"行情网络请求失败：{exc}") from exc
        return parse_tencent_payload(payload, symbol)

    def search(self, query: str, limit: int = 8) -> list[StockSearchResult]:
        keyword = (query or "").strip()
        if not keyword:
            return []
        url = self.search_endpoint.format(query=quote_plus(keyword))
        try:
            payload = self._transport(url).decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise QuoteError(f"股票名称搜索失败：{exc}") from exc
        return parse_tencent_search_payload(payload, limit=limit)

    def _download(self, url: str) -> bytes:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 StockDeskPet/0.1",
                "Referer": "https://stockapp.finance.qq.com/",
                "Accept": "*/*",
            },
        )
        with urlopen(request, timeout=self.timeout) as response:
            return response.read()


def parse_tencent_search_payload(payload: str, limit: int = 8) -> list[StockSearchResult]:
    match = re.search(r'v_hint\s*=\s*"([^"]*)"', payload)
    if not match or not match.group(1):
        return []
    value = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda item: chr(int(item.group(1), 16)),
        match.group(1),
    )
    results: list[StockSearchResult] = []
    seen: set[str] = set()
    for raw_item in value.split("^"):
        fields = raw_item.split("~")
        if len(fields) < 5:
            continue
        market, code, name, _pinyin, security_type = fields[:5]
        market = market.lower()
        if market not in {"hk", "sh", "sz", "bj"}:
            continue
        if not (security_type.startswith("GP") or security_type == "ETF"):
            continue
        try:
            symbol = normalize_symbol(f"{market}{code}")
        except SymbolError:
            continue
        if symbol.provider_symbol in seen:
            continue
        seen.add(symbol.provider_symbol)
        results.append(StockSearchResult(symbol=symbol, name=name.strip()))
        if len(results) >= max(1, limit):
            break
    return results


def parse_tencent_payload(payload: str, symbol: StockSymbol) -> Quote:
    match = re.search(r'=\s*"([^"]*)"', payload)
    if not match or not match.group(1):
        raise QuoteError(f"没有找到 {symbol.display_code} 的行情，请检查代码。")

    if symbol.market == "gold":
        return _parse_gold_payload(match.group(1), symbol)

    fields = match.group(1).split("~")
    if len(fields) < 38:
        raise QuoteError("行情响应格式异常，请稍后再试。")

    name = fields[1].strip() or symbol.display_code
    price = _number(fields, 3)
    previous_close = _number(fields, 4)
    open_price = _number(fields, 5)
    change = _number(fields, 31, price - previous_close)
    change_percent = _number(
        fields,
        32,
        (change / previous_close * 100.0) if previous_close else 0.0,
    )
    high = _number(fields, 33)
    low = _number(fields, 34)

    if symbol.market in {"hk", "hk_index"}:
        volume = _number(fields, 36, _number(fields, 6))
        volume_unit = "股"
        amount = _number(fields, 37)
        turnover_rate = _optional_number(fields, 59)
    else:
        volume = _number(fields, 36, _number(fields, 6))
        volume_unit = "手"
        amount = _number(fields, 57, _number(fields, 37)) * 10_000.0
        turnover_rate = _optional_number(fields, 38)

    return Quote(
        symbol=symbol,
        name=name,
        price=price,
        previous_close=previous_close,
        open_price=open_price,
        high=high,
        low=low,
        change=change,
        change_percent=change_percent,
        volume=volume,
        volume_unit=volume_unit,
        amount=amount,
        quote_time=_normalize_time(fields[30].strip()),
        source="腾讯行情（公共网页接口）",
        turnover_rate=turnover_rate,
    )


def _parse_gold_payload(value: str, symbol: StockSymbol) -> Quote:
    fields = value.split(",")
    if len(fields) < 14:
        raise QuoteError("黄金行情响应格式异常，请稍后再试。")

    price = _number(fields, 0)
    previous_close = _number(fields, 7)
    change = price - previous_close
    return Quote(
        symbol=symbol,
        name=fields[13].strip() or "伦敦金（现货黄金）",
        price=price,
        previous_close=previous_close,
        open_price=_number(fields, 8),
        high=_number(fields, 4),
        low=_number(fields, 5),
        change=change,
        change_percent=_number(
            fields,
            1,
            (change / previous_close * 100.0) if previous_close else 0.0,
        ),
        volume=0.0,
        volume_unit="—",
        amount=0.0,
        quote_time=f"{fields[12].strip()} {fields[6].strip()}".strip(),
        source="腾讯行情（公共网页接口）",
    )


def _number(fields: list[str], index: int, default: float = 0.0) -> float:
    if index >= len(fields):
        return default
    value = fields[index].strip().replace(",", "")
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _optional_number(fields: list[str], index: int) -> float | None:
    if index >= len(fields):
        return None
    value = fields[index].strip().replace(",", "")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _normalize_time(value: str) -> str:
    if re.fullmatch(r"\d{14}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:8]} {value[8:10]}:{value[10:12]}:{value[12:14]}"
    return value.replace("/", "-")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check StockDeskPet quote provider")
    parser.add_argument("symbols", nargs="+", help="00700 600519 000001")
    args = parser.parse_args()
    provider = TencentQuoteProvider()
    failed = False
    for raw in args.symbols:
        try:
            quote = provider.fetch(raw)
            print(
                f"{quote.name} {quote.symbol.display_code} "
                f"{quote.price:.3f} {quote.change_percent:+.2f}% "
                f"@ {quote.quote_time}"
            )
        except (SymbolError, QuoteError) as exc:
            failed = True
            print(f"{raw}: {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
