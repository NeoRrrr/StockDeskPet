from __future__ import annotations

import math
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from .models import Quote, StockSearchResult, StockSymbol
from .quote_provider import QuoteError, TencentQuoteProvider
from .symbols import normalize_symbol
from .us_sessions import parse_us_time, us_now, us_session_at


FUTU_RETRY_SECONDS = 30.0
OPEND_PROBE_TIMEOUT_SECONDS = 0.1
OPEND_ABSENT_PROBE_SECONDS = 300.0
OPEND_PRESENT_PROBE_SECONDS = 30.0
INTERACTIVE_LOCK_TIMEOUT_SECONDS = 0.15
EXTENDED_SUBSCRIPTION_RETRY_SECONDS = 300.0
FUTU_WATCHLIST_STOCK_TYPES = {"STOCK", "ETF"}
FUTU_WATCHLIST_MARKETS = {"HK", "SH", "SZ", "BJ", "US"}


class FutuBusyError(QuoteError):
    """Raised when a foreground quote should not wait behind background polling."""


def is_opend_listening(host: str, port: int) -> bool:
    """A bounded TCP check, without importing the SDK or opening a quote session.

    Called only by background quote/status workers. An open port is a prerequisite,
    not proof of a logged-in OpenD session or market data permission.
    """
    try:
        with socket.create_connection((host, port), timeout=OPEND_PROBE_TIMEOUT_SECONDS):
            return True
    except OSError:
        return False


def futu_code_for(symbol: StockSymbol) -> str | None:
    if symbol.market == "us":
        return symbol.code
    if symbol.market == "hk":
        return f"HK.{symbol.code}"
    if symbol.market == "sh":
        return f"SH.{symbol.code}"
    if symbol.market == "sz":
        return f"SZ.{symbol.code}"
    if symbol.code == "HSI":
        return "HK.800000"
    return None


def quote_from_futu_row(
    row: Any, symbol: StockSymbol, *, ticks: Any = None,
    at: datetime | None = None, extended_error: str = "",
) -> Quote:
    price = _float_value(row, "last_price")
    previous_close = _float_value(row, "prev_close_price")
    change = price - previous_close
    change_percent = change / previous_close * 100.0 if previous_close else 0.0
    data_date = str(_row_value(row, "data_date", "")).strip()
    data_time = str(_row_value(row, "data_time", "")).strip()
    turnover_rate = _optional_float_value(row, "turnover_rate")

    quote = Quote(
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
        quote_time=data_time if data_time.startswith(data_date + " ") and data_date else f"{data_date} {data_time}".strip(),
        source="富途 OpenD（实时行情）",
        turnover_rate=turnover_rate,
        session="regular" if symbol.market == "us" else "",
        session_date=data_date if symbol.market == "us" else "",
    )
    if symbol.market != "us":
        return quote
    current = us_now(at)
    session = us_session_at(current, close_grace=True)
    if session is None or session.kind == "regular":
        return quote

    def unavailable(reason: str) -> Quote:
        return replace(quote, session_note=f"{session.label}{reason}；显示常规行情")

    if extended_error:
        return unavailable(f"数据不可用（{extended_error}）")
    prefix = session.kind
    session_price = _optional_float_value(row, f"{prefix}_price")
    session_change = _optional_float_value(row, f"{prefix}_change_val")
    if session_price is None or session_price <= 0 or session_change is None:
        return unavailable("暂无有效报价")
    # Aggregate extended quotes have no own timestamp in this SDK. Never attach
    # the regular-session data_time or wall clock to an overnight price.
    candidates = []
    if ticks is not None and not getattr(ticks, "empty", True):
        for _, tick in ticks.iterrows():
            stamp = parse_us_time(str(_row_value(tick, "time", "")))
            price = _optional_float_value(tick, "price")
            if (
                str(_row_value(tick, "code", "")) == symbol.code
                and stamp and session.contains(stamp)
                and stamp <= current + timedelta(seconds=5)
                and price is not None and price > 0
            ):
                candidates.append((stamp, price))
    if not candidates:
        return unavailable("尚无本时段可验证的成交")
    stamp, price = max(candidates, key=lambda item: item[0])
    reference = session_price - session_change
    if reference <= 0:
        return unavailable("涨跌基准无效")
    if abs(reference - quote.price) > max(0.0005, abs(quote.price) * 0.000001):
        return unavailable("报价与最近常规收盘不一致，等待本时段数据")
    # Use the timed trade as price authority. Quote aggregates can trail it.
    high = _optional_float_value(row, f"{prefix}_high_price")
    low = _optional_float_value(row, f"{prefix}_low_price")
    return replace(
        quote, price=price, previous_close=reference, open_price=None,
        high=max(high, price) if high is not None and high > 0 else None,
        low=min(low, price) if low is not None and low > 0 else None,
        change=price - reference, change_percent=(price - reference) / reference * 100,
        volume=_optional_float_value(row, f"{prefix}_volume"),
        amount=_optional_float_value(row, f"{prefix}_turnover"), turnover_rate=None,
        quote_time=stamp.replace(tzinfo=None).isoformat(sep=" ", timespec="milliseconds"),
        session=prefix, session_date=session.trading_date.isoformat(),
        session_note=f"{session.label}涨跌相对基准收盘价；开盘价和换手率不提供",
    )


def watchlist_entries_from_futu_data(data: Any) -> list[tuple[str, str]]:
    """Convert Futu watchlist rows to supported A/HK/US stock entries."""
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
        if market not in FUTU_WATCHLIST_MARKETS or (market != "US" and not digits.isdigit()):
            continue
        try:
            symbol = normalize_symbol(futu_code if market == "US" else f"{market}{digits}")
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
        self._ticker_subscribed: set[str] = set()
        self._ticker_retry_after: dict[str, float] = {}
        self._ticker_errors: dict[str, str] = {}
        self._session_all: Any | None = None
        self._retired = threading.Event()

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
            session = us_session_at(close_grace=True) if symbol.market == "us" else None
            ticks, error = (None, "")
            if session is not None and session.kind != "regular":
                ticks, error = self._extended_ticks(context, code)
            return quote_from_futu_row(data.iloc[0], symbol, ticks=ticks, extended_error=error)
        finally:
            self._lock.release()

    def _extended_ticks(self, context: Any, code: str) -> tuple[Any, str]:
        if time.monotonic() < self._ticker_retry_after.get(code, 0):
            return None, self._ticker_errors.get(code, "暂不可用")
        try:
            if code not in self._ticker_subscribed:
                ret, message = context.subscribe(
                    [code], [self._sub_type.TICKER], is_first_push=False,
                    subscribe_push=False, session=self._session_all,
                )
                if ret != self._ret_ok:
                    raise QuoteError(str(message))
                self._ticker_subscribed.add(code)
            ret, ticks = context.get_rt_ticker(code, num=20)
            if ret != self._ret_ok:
                self._ticker_subscribed.discard(code)
                raise QuoteError(str(ticks))
            self._ticker_retry_after.pop(code, None)
            self._ticker_errors.pop(code, None)
            return ticks, ""
        except Exception as exc:
            # Night permissions are per symbol/market, not an OpenD outage.
            # Keep regular/HK subscriptions alive and back off this extra query.
            message = _friendly_futu_error(exc)
            self._ticker_errors[code] = message
            self._ticker_retry_after[code] = time.monotonic() + EXTENDED_SUBSCRIPTION_RETRY_SECONDS
            return None, message

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

    def retire(self) -> None:
        self._retired.set()
        threading.Thread(target=self.close, name="opend-close", daemon=True).start()

    def _ensure_context(self) -> Any:
        if self._retired.is_set():
            raise QuoteError("行情源已切换，忽略旧 OpenD 请求")
        if self._context is not None:
            return self._context
        try:
            from futu import OpenQuoteContext, RET_OK, Session, SubType
        except ImportError as exc:
            raise QuoteError("未安装 futu-api 行情组件。") from exc
        try:
            self._context = OpenQuoteContext(host=self.host, port=self.port)
        except Exception as exc:
            self._context = None
            raise QuoteError(_friendly_futu_error(exc)) from exc
        self._ret_ok = RET_OK
        self._sub_type = SubType
        self._session_all = Session.ALL
        return self._context

    def _reset_context(self) -> None:
        context = self._context
        self._context = None
        self._subscribed.clear()
        self._ticker_subscribed.clear()
        self._ticker_retry_after.clear()
        self._ticker_errors.clear()
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
        opend_probe: Callable[[str, int], bool] | None = None,
    ) -> None:
        self.tencent = tencent or TencentQuoteProvider()
        self.futu = futu or FutuQuoteProvider()
        self.mode = mode if mode in self.MODES else "auto"
        self._retry_after = 0.0
        self._last_futu_error = "尚未检测 OpenD"
        self._state_lock = threading.Lock()
        self._probe_lock = threading.Lock()
        self._opend_probe = opend_probe or is_opend_listening
        self._opend_available = False
        self._session_ready = False  # Confirmed by the SDK, not just a listening port.
        self._probe_after = 0.0
        self.generation = 0

    def set_mode(self, mode: str) -> None:
        mode = mode if mode in self.MODES else "auto"
        retired = None
        with self._state_lock:
            if mode == self.mode:
                return
            self.mode = mode
            self.generation += 1
            self._retry_after = 0.0
            self._opend_available = False
            self._session_ready = False
            self._last_futu_error = "尚未检测 OpenD"
            self._probe_after = 0.0
            if mode == "tencent":
                retired = self.futu
                # An old close must never close a newly re-enabled connection.
                self.futu = FutuQuoteProvider(
                    host=getattr(retired, "host", "127.0.0.1"),
                    port=getattr(retired, "port", 11111),
                )
        if retired is not None:
            retired.retire()

    def fetch(self, raw_symbol: str) -> Quote:
        return self._fetch(raw_symbol, interactive=False)

    def fetch_interactive(self, raw_symbol: str) -> Quote:
        """Fetch a user-selected quote without waiting behind background batches."""
        return self._fetch(raw_symbol, interactive=True)

    def _fetch(self, raw_symbol: str, *, interactive: bool) -> Quote:
        symbol = normalize_symbol(raw_symbol)
        with self._state_lock:
            mode, futu, generation = self.mode, self.futu, self.generation
        futu_supported = futu.supports(symbol)
        if mode == "auto" and futu_supported and self._can_retry_futu(futu, generation):
            try:
                quote = (
                    futu.fetch(raw_symbol, lock_timeout=INTERACTIVE_LOCK_TIMEOUT_SECONDS)
                    if interactive else futu.fetch(raw_symbol)
                )
            except FutuBusyError:
                quote = self.tencent.fetch(raw_symbol)
                return replace(quote, source="腾讯行情（富途后台刷新中）")
            except Exception as exc:
                self._mark_futu_failure(exc, futu, generation)
            else:
                self._record_futu_success(generation)
                return quote

        quote = self.tencent.fetch(raw_symbol)
        if mode == "auto" and futu_supported:
            quote = replace(quote, source="腾讯行情（富途不可用，行情可能延迟）")
        return quote

    def fetch_batch(self, raw_symbols: list[str]) -> tuple[list[Quote], list[str]]:
        with self._state_lock:
            mode, futu, generation = self.mode, self.futu, self.generation
        if mode == "tencent":
            return self.tencent.fetch_batch(raw_symbols)

        quotes: list[Quote] = []
        errors: list[str] = []
        fallback: list[str] = []
        supported_keys: set[str] = set()
        seen: set[str] = set()
        for raw in raw_symbols:
            if generation != self.generation:
                break  # The UI discards this generation and retries with the new source.
            try:
                symbol = normalize_symbol(raw)
            except ValueError as exc:
                errors.append(f"{raw}: {exc}")
                continue
            key = symbol.provider_symbol
            if key in seen:
                continue
            seen.add(key)
            supported = futu.supports(symbol)
            if supported:
                supported_keys.add(key)
            if supported and self._can_retry_futu(futu, generation):
                try:
                    quotes.append(futu.fetch(raw))
                except Exception as exc:
                    self._mark_futu_failure(exc, futu, generation)
                else:
                    self._record_futu_success(generation)
                    continue
            fallback.append(raw)
        if fallback and generation == self.generation:
            backup, failures = self.tencent.fetch_batch(fallback)
            quotes.extend(
                replace(q, source="腾讯行情（富途不可用，行情可能延迟）")
                if q.symbol.provider_symbol in supported_keys else q
                for q in backup
            )
            errors.extend(failures)
        return quotes, errors

    def search(self, query: str, limit: int = 8) -> list[StockSearchResult]:
        return self.tencent.search(query, limit=limit)

    def check_futu_status(self) -> tuple[bool, str]:
        ok, message, _ = self.check_opend_session(self.generation)
        return ok, message

    def check_opend_session(self, generation: int) -> tuple[bool, str, bool]:
        """Force a session check without publishing a positive TCP probe as recovery."""
        with self._state_lock:
            if generation != self.generation:
                return False, "行情源已切换", False
            futu = self.futu
        # Serialize with demand-driven probes on the worker, never on the GUI thread.
        with self._probe_lock:
            with self._state_lock:
                if generation != self.generation:
                    return False, "行情源已切换", False
            listening = self._opend_probe(getattr(futu, "host", "127.0.0.1"), getattr(futu, "port", 11111))
            ok, message = False, "未检测到 OpenD 服务"
            if listening:
                with self._state_lock:
                    if generation != self.generation:
                        return False, "行情源已切换", False
                try:
                    ok, message = futu.check_status()
                except Exception as exc:
                    message = _friendly_futu_error(exc)
            with self._state_lock:
                if generation != self.generation:
                    return False, "行情源已切换", False
                recovered = ok and not self._session_ready
                self._session_ready = ok
                self._opend_available = ok
                now = time.monotonic()
                self._retry_after = now + FUTU_RETRY_SECONDS if listening and not ok else 0.0
                self._probe_after = now + (OPEND_PRESENT_PROBE_SECONDS if listening else OPEND_ABSENT_PROBE_SECONDS)
                self._last_futu_error = message
                return ok, message, recovered

    def list_futu_watchlist_groups(self) -> list[str]:
        futu = self._available_futu_for_import()
        return futu.list_watchlist_groups()

    def get_futu_watchlist_group(self, group_name: str) -> list[tuple[str, str]]:
        futu = self._available_futu_for_import()
        return futu.get_watchlist_group(group_name)

    def _available_futu_for_import(self) -> FutuQuoteProvider:
        with self._state_lock:
            futu, generation = self.futu, self.generation
        if not self._can_retry_futu(futu, generation, force=True):
            raise QuoteError(self._probe_status_message(generation))
        return futu

    def status_text(self) -> str:
        if self.mode == "tencent":
            return "当前使用腾讯公共行情；港股约延迟 15 分钟"
        with self._state_lock:
            return self._last_futu_error

    def close(self) -> None:
        with self._state_lock:
            self.generation += 1
            futu = self.futu
        futu.retire()  # Don't wait for SDK I/O on the GUI thread during exit.

    def _can_retry_futu(self, futu: FutuQuoteProvider, generation: int, *, force: bool = False) -> bool:
        now = time.monotonic()
        with self._state_lock:
            if generation != self.generation:
                return False
            if not force:
                if now < self._retry_after:
                    return False
                if now < self._probe_after:
                    return self._opend_available
        # Favorites, tab refresh and detail fetch may arrive together. Only one
        # performs the check; the others use Tencent immediately, without waiting.
        if not self._probe_lock.acquire(blocking=False):
            return False
        try:
            with self._state_lock:
                if generation != self.generation:
                    return False
                if not force:
                    if time.monotonic() < self._retry_after:
                        return False
                    if time.monotonic() < self._probe_after:
                        return self._opend_available
            available = self._opend_probe(
                getattr(futu, "host", "127.0.0.1"), getattr(futu, "port", 11111),
            )
            with self._state_lock:
                if generation != self.generation:
                    return False
                self._opend_available = available
                delay = OPEND_PRESENT_PROBE_SECONDS if available else OPEND_ABSENT_PROBE_SECONDS
                self._probe_after = time.monotonic() + delay
                if force:
                    self._retry_after = 0.0
                if not available:
                    self._session_ready = False
                    self._last_futu_error = "未检测到 OpenD 服务，暂用腾讯行情"
            return available
        finally:
            self._probe_lock.release()

    def _probe_status_message(self, generation: int) -> str:
        with self._state_lock:
            if generation != self.generation:
                return "行情源已切换，请重新检测"
            if not self._opend_available and time.monotonic() < self._probe_after:
                return self._last_futu_error
        return "OpenD 检测进行中，请稍后重试；行情暂用腾讯"

    def _record_futu_success(self, generation: int) -> None:
        with self._state_lock:
            if generation == self.generation:
                self._session_ready = True
                self._last_futu_error = "OpenD 已连接，正在使用实时行情"
                self._retry_after = 0.0

    def _mark_futu_failure(self, exc: Exception, futu: FutuQuoteProvider, generation: int) -> None:
        message = _friendly_futu_error(exc)
        with self._state_lock:
            if generation == self.generation:
                self._last_futu_error = message
                self._retry_after = time.monotonic() + FUTU_RETRY_SECONDS
                self._opend_available = False
                self._session_ready = False
                self._probe_after = 0.0
        futu.close()


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
