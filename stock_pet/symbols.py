from __future__ import annotations

import re

from .models import StockSymbol


class SymbolError(ValueError):
    pass


MAX_WATCHLIST_ITEMS = 100


_PREFIXED_RE = re.compile(r"^(HK|SH|SZ|BJ)(\d{1,6})$", re.IGNORECASE)
_SUFFIXED_RE = re.compile(r"^(\d{1,6})\.(HK|SS|SH|SZ|BJ)$", re.IGNORECASE)

_SPECIAL_SYMBOLS: dict[str, tuple[str, str, str, str, str]] = {
    "HSI": ("hkHSI", "HSI", "hk_index", "港股指数", "PTS"),
    "HSTECH": ("hkHSTECH", "HSTECH", "hk_index", "港股指数", "PTS"),
    "HSCEI": ("hkHSCEI", "HSCEI", "hk_index", "港股指数", "PTS"),
    "SHCOMP": ("sh000001", "SHCOMP", "cn_index", "大A指数", "PTS"),
    "CSI300": ("sh000300", "CSI300", "cn_index", "大A指数", "PTS"),
    "SZCOMP": ("sz399001", "SZCOMP", "cn_index", "大A指数", "PTS"),
    "CHINEXT": ("sz399006", "CHINEXT", "cn_index", "大A指数", "PTS"),
    "GOLD": ("hf_XAU", "GOLD", "gold", "伦敦金", "USD"),
    "XAU": ("hf_XAU", "GOLD", "gold", "伦敦金", "USD"),
}


def normalize_symbol(raw: str) -> StockSymbol:
    value = re.sub(r"[\s_-]+", "", raw or "").upper()
    if not value:
        raise SymbolError("请输入股票代码，例如 00700、600519 或 000001。")

    special = _SPECIAL_SYMBOLS.get(value)
    if special is not None:
        provider_symbol, code, market, label, currency = special
        return StockSymbol(
            raw=raw,
            provider_symbol=provider_symbol,
            code=code,
            market=market,
            market_label=label,
            currency=currency,
        )

    match = _SUFFIXED_RE.fullmatch(value)
    if match:
        digits, suffix = match.groups()
        market = {"SS": "sh", "SH": "sh", "SZ": "sz", "BJ": "bj", "HK": "hk"}[suffix]
        return _make_symbol(raw, market, digits)

    match = _PREFIXED_RE.fullmatch(value)
    if match:
        prefix, digits = match.groups()
        return _make_symbol(raw, prefix.lower(), digits)

    if not value.isdigit():
        raise SymbolError("代码格式不正确。支持 00700、HK00700、600519、SH600519、000001。")

    if len(value) <= 5:
        return _make_symbol(raw, "hk", value)
    if len(value) != 6:
        raise SymbolError("港股代码最多 5 位，A 股代码应为 6 位。")

    if value.startswith(("5", "6")):
        market = "sh"
    elif value.startswith(("0", "1", "2", "3")):
        market = "sz"
    elif value.startswith(("4", "8", "92")):
        market = "bj"
    elif value.startswith("9"):
        market = "sh"
    else:
        raise SymbolError("暂时无法自动判断交易所，请使用 SH、SZ、BJ 或 HK 前缀。")
    return _make_symbol(raw, market, value)


def normalize_watchlist(
    values: list[str],
    max_items: int = MAX_WATCHLIST_ITEMS,
) -> list[str]:
    """Normalize, de-duplicate and validate user-saved stock IDs."""
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or not value.strip():
            continue
        symbol = normalize_symbol(value)
        if symbol.market not in {"hk", "sh", "sz", "bj"}:
            raise SymbolError("指数和黄金已固定显示在“指数”Tab，无需加入自选股。")
        if symbol.provider_symbol in seen:
            continue
        seen.add(symbol.provider_symbol)
        normalized.append(symbol.code)
        if len(normalized) > max_items:
            raise SymbolError(f"自选股最多保存 {max_items} 只。")
    return normalized


def partition_watchlist(values: list[str]) -> tuple[list[str], list[str]]:
    """Return normalized A-share and HK-share IDs while preserving their order."""
    a_shares: list[str] = []
    hk_shares: list[str] = []
    for value in normalize_watchlist(values):
        symbol = normalize_symbol(value)
        target = hk_shares if symbol.market == "hk" else a_shares
        target.append(symbol.code)
    return a_shares, hk_shares


def _make_symbol(raw: str, market: str, digits: str) -> StockSymbol:
    if market == "hk":
        if len(digits) > 5:
            raise SymbolError("港股代码最多 5 位。")
        code = digits.zfill(5)
        label = "港股"
        currency = "HKD"
    else:
        if len(digits) != 6:
            raise SymbolError("A 股代码应为 6 位。")
        code = digits
        label = {"sh": "沪市", "sz": "深市", "bj": "北交所"}.get(market)
        if label is None:
            raise SymbolError("不支持的交易所。")
        currency = "CNY"

    return StockSymbol(
        raw=raw,
        provider_symbol=f"{market}{code}",
        code=code,
        market=market,
        market_label=label,
        currency=currency,
    )
