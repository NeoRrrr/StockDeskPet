"""Local-only UI heartbeat and bounded, native watchdog stack dumps.

faulthandler's watchdog runs without the Python GIL or Qt event loop, so it
can still capture a stalled main thread when an ordinary QTimer cannot run.
It is process-global; the application owns exactly one UiStallMonitor.
"""
from __future__ import annotations

import faulthandler
from collections import deque
from functools import wraps
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import traceback
from time import monotonic
from typing import Callable

from PySide6.QtCore import QObject, QTimer, QtMsgType, qInstallMessageHandler, qVersion

from . import __version__


STALL_SECONDS = 3.0
HEARTBEAT_MS = 1000
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 2
SLOW_UI_SECONDS = 0.1
_active_monitor: UiStallMonitor | None = None


def record_render_event(event: str, detail: str, *, warning: bool = False) -> None:
    monitor = _active_monitor
    if monitor is not None and monitor.active:
        monitor._render_events.append((logging.WARNING if warning else logging.INFO, event, detail))


def trace_ui_operation(name: str):
    """Record slow GUI work without file I/O in the rendering hot path."""
    def decorate(function):
        @wraps(function)
        def measured(*args, **kwargs):
            monitor = _active_monitor
            if monitor is None or not monitor.active:
                return function(*args, **kwargs)
            start = monotonic()
            try:
                return function(*args, **kwargs)
            finally:
                elapsed = monotonic() - start
                if elapsed >= SLOW_UI_SECONDS:
                    monitor._slow_operations.append((name, elapsed))
        return measured
    return decorate


def diagnostic_log_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return root / "StockDeskPet" / "logs"


class UiStallMonitor(QObject):
    """Rearm a one-shot native watchdog after every successful UI heartbeat.

    A stall produces one stack dump, not a continuous stream. The next heartbeat
    appends its duration and re-enables detection. Never log locals, quotes or
    account information. File access failures must not prevent app startup.
    """

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        log_dir: Path | None = None,
        threshold_seconds: float = STALL_SECONDS,
        heartbeat_ms: int = HEARTBEAT_MS,
        max_bytes: int = MAX_LOG_BYTES,
    ) -> None:
        global _active_monitor
        super().__init__(parent)
        if threshold_seconds <= heartbeat_ms / 1000:
            raise ValueError("Stall threshold must exceed the heartbeat interval")
        self.log_path = (log_dir or diagnostic_log_dir()) / "ui-stalls.log"
        self.threshold_seconds = threshold_seconds
        self.last_error = ""
        self._handler: RotatingFileHandler | None = None
        self._logger = logging.Logger("stock_pet.ui_stalls", logging.INFO)
        self._context: Callable[[], str] | None = None
        self._last_context = "initializing"
        self._last_heartbeat = monotonic()
        self._armed = False
        self._slow_operations: deque[tuple[str, float]] = deque(maxlen=32)
        self._render_events: deque[tuple[int, str, str]] = deque(maxlen=128)
        # Qt invokes its message handler on the emitting thread. Only append to
        # a bounded deque there; the GUI heartbeat owns file I/O and rotation.
        self._qt_messages: deque[tuple[str, str]] = deque(maxlen=64)
        self._python_errors: deque[str] = deque(maxlen=16)
        self._qt_handler = self._on_qt_message
        self._exception_handler = self._on_python_exception
        self._previous_qt_handler = None
        self._previous_exception_handler = None
        self._hooks_installed = False
        self._timer = QTimer(self)
        self._timer.setInterval(heartbeat_ms)
        self._timer.timeout.connect(self._heartbeat)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._handler = RotatingFileHandler(
                self.log_path, maxBytes=max_bytes, backupCount=LOG_BACKUPS,
                encoding="utf-8",
            )
            self._handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)s %(message)s"
            ))
            self._logger.addHandler(self._handler)
            self._logger.info(
                "SESSION_START version=%s pid=%s python=%s qt=%s render_guard=1 threshold=%.1fs context=%s",
                __version__, os.getpid(), sys.version.split()[0], qVersion(),
                threshold_seconds, self._last_context,
            )
            self._previous_qt_handler = qInstallMessageHandler(self._qt_handler)
            self._previous_exception_handler = sys.excepthook
            sys.excepthook = self._exception_handler
            self._hooks_installed = True
            self._arm()
            self._timer.start()
            _active_monitor = self
        except Exception as exc:
            self._disable(exc)

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    def set_context_provider(self, provider: Callable[[], str]) -> None:
        # Called only on the UI thread; no background worker accesses widgets.
        self._context = provider

    def _on_qt_message(self, kind, context, message: str) -> None:
        if kind in {QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg}:
            # Limit payload and retain only rendering/lifecycle diagnostics.
            if any(word in message for word in (
                "QPainter", "QBackingStore", "QWidget", "QWindows", "QPixmap",
                "QImage", "UpdateLayeredWindow", "QPaintDevice", "QObject::",
            )):
                self._qt_messages.append((str(getattr(context, "category", "qt")),
                                          message.replace("\n", " ").replace("\r", " ")[:1000]))
        if self._previous_qt_handler is not None:
            self._previous_qt_handler(kind, context, message)

    def _on_python_exception(self, kind, value, tb) -> None:
        # Do not retain exception objects/tracebacks, locals or exception values
        # (which could contain search text/quotes). Frame locations suffice.
        frames = traceback.extract_tb(tb, limit=12)
        location = " > ".join(f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames)
        self._python_errors.append(f"type={kind.__name__} frames={location}")
        if self._previous_exception_handler is not None:
            self._previous_exception_handler(kind, value, tb)

    def _drain_render_logs(self) -> None:
        messages: dict[tuple[str, str], int] = {}
        while self._qt_messages:
            key = self._qt_messages.popleft()
            messages[key] = messages.get(key, 0) + 1
        for (category, message), count in messages.items():
            self._logger.warning("QT_RENDER category=%s count=%s message=%s", category, count, message)
        while self._python_errors:
            self._logger.error("UI_CALLBACK_EXCEPTION %s", self._python_errors.popleft())
        while self._render_events:
            level, event, detail = self._render_events.popleft()
            self._logger.log(level, "%s %s", event, detail)

    def _arm(self) -> None:
        if self._handler is not None:
            self._handler.flush()
            faulthandler.dump_traceback_later(
                self.threshold_seconds, repeat=False, file=self._handler.stream,
                exit=False,
            )
            self._armed = True

    def _cancel(self) -> None:
        if self._armed:
            # Cancel/join the native writer BEFORE rotating or closing its fd.
            faulthandler.cancel_dump_traceback_later()
            self._armed = False

    def _heartbeat(self) -> None:
        now = monotonic()
        elapsed = now - self._last_heartbeat
        self._cancel()
        if self._handler is None:
            return
        try:
            self._drain_render_logs()
            while self._slow_operations:
                operation, duration = self._slow_operations.popleft()
                self._logger.warning("SLOW_UI operation=%s duration_ms=%.1f", operation, duration * 1000)
            if elapsed >= self.threshold_seconds:
                self._logger.warning(
                    "UI_STALL_RECOVERED heartbeat_gap=%.3fs previous_context=%s "
                    "(includes system sleep/suspend if applicable)",
                    elapsed, self._last_context,
                )
            if self._context is not None:
                context = self._context()
                if context != self._last_context:
                    self._logger.info("CONTEXT %s", context)
                    self._last_context = context
            # Native dumps bypass logging.emit; also check their bytes even
            # when there are no context changes. Rotation is safe while unarmed.
            self._handler.stream.seek(0, os.SEEK_END)
            if self._handler.stream.tell() >= self._handler.maxBytes:
                self._handler.doRollover()
                self._logger.info("CONTINUED pid=%s context=%s", os.getpid(), self._last_context)
            self._last_heartbeat = monotonic()
            self._arm()
        except Exception as exc:
            self._disable(exc)

    def _disable(self, exc: Exception) -> None:
        self.last_error = str(exc)
        self.stop()

    def stop(self) -> None:
        global _active_monitor
        if _active_monitor is self:
            _active_monitor = None
        self._timer.stop()
        self._cancel()
        if self._hooks_installed:
            self._hooks_installed = False
            current = qInstallMessageHandler(self._previous_qt_handler)
            if current is not self._qt_handler:
                qInstallMessageHandler(current)
            if sys.excepthook is self._exception_handler:
                sys.excepthook = self._previous_exception_handler
        if self._handler is not None:
            handler = self._handler
            try:
                self._drain_render_logs()
                self._logger.info("SESSION_STOP")
            except Exception:
                pass
            self._handler = None
            self._logger.removeHandler(handler)
            try:
                handler.close()
            except OSError:
                pass
        self._context = None
