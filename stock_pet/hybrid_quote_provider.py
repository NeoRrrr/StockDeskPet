from __future__ import annotations

import math
import threading
import time
from dataclasses import replace
from typing import Any

from .models import Quote, StockSearchResult, StockSymbol
from .quote_provider import QuoteError, TencentQuoteProvider
from .symbols import normalize_symbol


FUTU_RETRY_SECONDS = 30.0
INTERACTIVE_LOCK_TIMEOUT_SECONDS = 0.15
FUTU_WATCHLIST_STOCK_TYPES = {"STOCK", "ETF"}
FUTU_WATCHLIST_MARKETS = {"HK", "SH", "SZ", "BJ"}


class FutuBusyError(QuoteError):
    """Raised when a foreground quote should not wait behind background polling."""


def futu_code_for(symbol: StockSymbol) -> str | None:
    if symbol.market == "hk":
        return f"HK.{symbol.code}"
    if symbol.market == "sh":
        return f"SH.{symbol.code}"
    if symbol.market == "sz":
        return f"SZ.{symbol.code}"
    if symbol.code == "HSI":
        return "HK.800000"
    return None


def quote_from_futu_row(row: Any, symbol: StockSymbol) -> Quote:
    price = _float_value(row, "last_price")
    previous_close = _float_value(row, "prev_close_price")
    change = price - previous_close
    change_percent = change / previous_close * 100.0 if previous_close else 0.0
    data_date = str(_row_value(row, "data_date", "")).strip()
    data_time = str(_row_value(row, "data_time", "")).strip()
    turnover_rate = _optional_float_value(row, "turnover_rate")

    return Quote(
        symbol=symbol,
        name=str(_row_value(row, "name", "")).strip() or symbol.display_code,
        price=price,
        previous_close=previous_close,
        open_price=_float_value(row, "open_price"),
        high=_float_value(row, "high_price"),
        low=_float_value(row, "low_price"),
        change=change,
        change_percent=change_percent,
        volume=_float_value(row, "volume"),
        volume_unit="股",
        amount=_float_value(row, "turnover"),
        quote_time=f"{data_date} {data_time}".strip(),
        source="富途 OpenD（实时行情）",
        turnover_rate=turnover_rate,
    )


def watchlist_entries_from_futu_data(data: Any) -> list[tuple[str, str]]:
    """Convert Futu watchlist rows to supported A-share/HK stock entries."""
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    if data is None or getattr(data, "empty", True):
        return entries
    for _, row in data.iterrows():
        futu_code = str(_row_value(row, "code", "")).strip().upper()
        stock_type = str(_row_value(row, "stock_type", "")).strip().upper()
        if "." not in futu_code or stock_type not in FUTU_WATCHLIST_STOCK_TYPES:
            continue
        market, digits = futu_code.split(".", 1)
        if market not in FUTU_WATCHLIST_MARKETS or not digits.isdigit():
            continue
        try:
            symbol = normalize_symbol(f"{market}{digits}")
        except Exception:
            continue
        if symbol.provider_symbol in seen:
            continue
        seen.add(symbol.provider_symbol)
        name = str(_row_value(row, "name", "")).strip() or symbol.display_code
        entries.append((symbol.code, name))
    return entries


class FutuQuoteProvider:
    def __init__(self, host: str = "127.0.0.1", port: int = 11111) -> None:
        self.host = host
        self.port = port
        self._lock = threading.RLock()
        self._context: Any | None = None
        self._ret_ok: int | None = None
        self._sub_type: Any | None = None
        self._subscribed: set[str] = set()

    def supports(self, symbol: StockSymbol) -> bool:
        return futu_code_for(symbol) is not None

    def fetch(self, raw_symbol: str, *, lock_timeout: float | None = None) -> Quote:
        symbol = normalize_symbol(raw_symbol)
        code = futu_code_for(symbol)
        if code is None:
            raise QuoteError(f"富途暂不支持 {symbol.display_code}，已切换备用行情。")

        if lock_timeout is None:
            acquired = self._lock.acquire()
        else:
            acquired = self._lock.acquire(timeout=max(0.0, lock_timeout))
        if not acquired:
            raise FutuBusyError("富途正在后台刷新")

        try:
            context = self._ensure_context()
            if code not in self._subscribed:
                ret, message = context.subscribe(
                    [code],
                    [self._sub_type.QUOTE],
                    is_first_push=False,
                    subscribe_push=False,
                )
                if ret != self._ret_ok:
                    self._reset_context()
                    raise QuoteError(f"富途行情订阅失败：{message}")
                self._subscribed.add(code)

            ret, data = context.get_stock_quote([code])
            if ret != self._ret_ok:
                self._reset_context()
                raise QuoteError(f"富途行情读取失败：{data}")
            if data is None or getattr(data, "empty", True):
                raise QuoteError(f"富途没有返回 {symbol.display_code} 的行情。")
            return quote_from_futu_row(data.iloc[0], symbol)
        finally:
            self._lock.release()

    def check_status(self) -> tuple[bool, str]:
        with self._lock:
            try:
                context = self._ensure_context()
                ret, data = context.query_subscription()
            except Exception as exc:
                self._reset_context()
                return False, _friendly_futu_error(exc)
            if ret != self._ret_ok:
                self._reset_context()
                return False, f"OpenD 连接失败：{data}"
            remain = data.get("remain") if isinstance(data, dict) else None
            suffix = f" · 剩余订阅额度 {remain}" if remain is not None else ""
            return True, f"OpenD 已连接{suffix}"

    def list_watchlist_groups(self) -> list[str]:
        with self._lock:
            context = self._ensure_context()
            ret, data = context.get_user_security_group()
            if ret != self._ret_ok:
                raise QuoteError(f"富途自选分组读取失败：{data}")
            if data is None or getattr(data, "empty", True):
                return []
            groups: list[str] = []
            for value in data.get("group_name", []):
                name = str(value).strip()
                if name and name not in groups:
                    groups.append(name)
            return groups

    def get_watchlist_group(self, group_name: str) -> list[tuple[str, str]]:
        with self._lock:
            context = self._ensure_context()
            ret, data = context.get_user_security(group_name)
            if ret != self._ret_ok:
                raise QuoteError(f"富途自选读取失败：{data}")
            return watchlist_entries_from_futu_data(data)

    def close(self) -> None:
        with self._lock:
            self._reset_context()

    def _ensure_context(self) -> Any:
        if self._context is not None:
            return self._context
        try:
            from futu import OpenQuoteContext, RET_OK, SubType
        except ImportError as exc:
            raise QuoteError("未安装 futu-api 行情组件。") from exc
        try:
            self._context = OpenQuoteContext(host=self.host, port=self.port)
        except Exception as exc:
            self._context = None
            raise QuoteError(_friendly_futu_error(exc)) from exc
        self._ret_ok = RET_OK
        self._sub_type = SubType
        return self._context

    def _reset_context(self) -> None:
        context = self._context
        self._context = None
        self._subscribed.clear()
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


class HybridQuoteProvider:
    MODES = {"auto", "tencent"}

    def __init__(
        self,
        mode: str = "auto",
        *,
        tencent: TencentQuoteProvider | None = None,
        futu: FutuQuoteProvider | None = None,
    ) -> None:
        self.tencent = tencent or TencentQuoteProvider()
        self.futu = futu or FutuQuoteProvider()
        self.mode = mode if mode in self.MODES else "auto"
        self._retry_after = 0.0
        self._last_futu_error = "尚未检测 OpenD"
        self._state_lock = threading.Lock()

    def set_mode(self, mode: str) -> None:
        self.mode = mode if mode in self.MODES else "auto"
        if self.mode == "tencent":
            self.futu.close()
        else:
            with self._state_lock:
                self._retry_after = 0.0

    def fetch(self, raw_symbol: str) -> Quote:
        symbol = normalize_symbol(raw_symbol)
        futu_supported = self.futu.supports(symbol)
        if self.mode == "auto" and futu_supported and self._can_retry_futu():
            try:
                quote = self.futu.fetch(raw_symbol)
            except Exception as exc:
                self._mark_futu_failure(exc)
            else:
                with self._state_lock:
                    self._last_futu_error = "OpenD 已连接，正在使用实时行情"
                    self._retry_after = 0.0
                return quote

        quote = self.tencent.fetch(raw_symbol)
        if self.mode == "auto" and futu_supported:
            quote = replace(quote, source="腾讯行情（富途不可用，港股可能延迟）")
        return quote

    def fetch_interactive(self, raw_symbol: str) -> Quote:
        """Fetch a user-selected quote without waiting behind background batches."""
        symbol = normalize_symbol(raw_symbol)
        futu_supported = self.futu.supports(symbol)
        if self.mode == "auto" and futu_supported and self._can_retry_futu():
            try:
                quote = self.futu.fetch(
                    raw_symbol,
                    lock_timeout=INTERACTIVE_LOCK_TIMEOUT_SECONDS,
                )
            except FutuBusyError:
                quote = self.tencent.fetch(raw_symbol)
                return replace(quote, source="腾讯行情（富途后台刷新中）")
            except Exception as exc:
                self._mark_futu_failure(exc)
            else:
                with self._state_lock:
                    self._last_futu_error = "OpenD 已连接，正在使用实时行情"
                    self._retry_after = 0.0
                return quote

        quote = self.tencent.fetch(raw_symbol)
        if self.mode == "auto" and futu_supported:
            quote = replace(quote, source="腾讯行情（富途不可用，港股可能延迟）")
        return quote

    def search(self, query: str, limit: int = 8) -> list[StockSearchResult]:
        return self.tencent.search(query, limit=limit)

    def check_futu_status(self) -> tuple[bool, str]:
        with self._state_lock:
            self._retry_after = 0.0
        ok, message = self.futu.check_status()
        with self._state_lock:
            self._last_futu_error = message
            if not ok:
                self._retry_after = time.monotonic() + FUTU_RETRY_SECONDS
        return ok, message

    def list_futu_watchlist_groups(self) -> list[str]:
        return self.futu.list_watchlist_groups()

    def get_futu_watchlist_group(self, group_name: str) -> list[tuple[str, str]]:
        return self.futu.get_watchlist_group(group_name)

    def status_text(self) -> str:
        if self.mode == "tencent":
            return "当前使用腾讯公共行情；港股约延迟 15 分钟"
        with self._state_lock:
            return self._last_futu_error

    def close(self) -> None:
        self.futu.close()

    def _can_retry_futu(self) -> bool:
        with self._state_lock:
            return time.monotonic() >= self._retry_after

    def _mark_futu_failure(self, exc: Exception) -> None:
        message = _friendly_futu_error(exc)
        with self._state_lock:
            self._last_futu_error = message
            self._retry_after = time.monotonic() + FUTU_RETRY_SECONDS
        self.futu.close()


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        value = row[key]
    except (KeyError, TypeError, IndexError):
        return default
    return default if value is None else value


def _float_value(row: Any, key: str, default: float = 0.0) -> float:
    value = _row_value(row, key, default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _optional_float_value(row: Any, key: str) -> float | None:
    value = _row_value(row, key)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _friendly_futu_error(exc: Exception) -> str:
    message = str(exc).strip()
    lower = message.lower()
    if "connection" in lower or "connect" in lower or "11111" in lower:
        return "OpenD 未连接，请先启动并登录富途 OpenD"
    if "quote right" in lower or "permission" in lower or "权限" in message:
        return "OpenD 已连接，但当前账号缺少对应实时行情权限"
    return message or "OpenD 暂不可用，已切换腾讯备用行情"
