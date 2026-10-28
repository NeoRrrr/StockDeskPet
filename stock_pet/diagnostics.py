"""Local-only UI heartbeat and bounded, native watchdog stack dumps.

faulthandler's watchdog runs without the Python GIL or Qt event loop, so it
can still capture a stalled main thread when an ordinary QTimer cannot run.
It is process-global; the application owns exactly one UiStallMonitor.
"""
from __future__ import annotations

import faulthandler
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
from time import monotonic
from typing import Callable

from PySide6.QtCore import QObject, QTimer

from . import __version__


STALL_SECONDS = 3.0
HEARTBEAT_MS = 1000
MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 2


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
                "SESSION_START version=%s pid=%s python=%s threshold=%.1fs context=%s",
                __version__, os.getpid(), sys.version.split()[0],
                threshold_seconds, self._last_context,
            )
            self._arm()
            self._timer.start()
        except Exception as exc:
            self._disable(exc)

    @property
    def active(self) -> bool:
        return self._timer.isActive()

    def set_context_provider(self, provider: Callable[[], str]) -> None:
        # Called only on the UI thread; no background worker accesses widgets.
        self._context = provider

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
        self._timer.stop()
        self._cancel()
        if self._handler is not None:
            handler = self._handler
            self._handler = None
            try:
                self._logger.info("SESSION_STOP")
            except Exception:
                pass
            self._logger.removeHandler(handler)
            try:
                handler.close()
            except OSError:
                pass
        self._context = None
