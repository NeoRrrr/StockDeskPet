"""Coalesced redraws and diagnostics for the two translucent top-level windows.

A Qt Paint event is evidence of paint dispatch, NOT proof that Windows/DWM
presented the pixels. Native backing-store warnings are recorded separately.
"""
from __future__ import annotations

from time import monotonic
import weakref

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QWidget

from .diagnostics import record_render_event


class WindowPaintGuard(QObject):
    def __init__(self, window: QWidget, name: str) -> None:
        super().__init__(window)
        self._window = weakref.ref(window)
        self.name = name
        self.revision = 0
        self.issued_revision = 0
        self.painted_revision = 0
        self.paint_events = 0
        self.update_events = 0
        self.flushes = 0
        self._pending_since: float | None = None
        self._last_paint = monotonic()
        self._last_report = 0.0
        self._last_recovery = float("-inf")
        self._recovery_attempted = False
        self._reason = "initial"
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(16)
        self._flush_timer.timeout.connect(self._flush)
        self._health_timer = QTimer(self)
        self._health_timer.setInterval(1000)
        self._health_timer.timeout.connect(self._check)
        window.installEventFilter(self)

    def request(self, reason: str, *, full: bool = True) -> None:
        window = self._window()
        self.revision += 1
        self._reason = reason  # Internal action names only, never quote text.
        if window is None or not window.isVisible() or window.isMinimized():
            return  # Show will request a full frame; never wake a hidden window.
        if self._pending_since is None:
            self._pending_since = monotonic()
            self._recovery_attempted = False
        if not full:
            # Animation frames already dirty their QLabel. Observe that paint
            # without forcing an extra full-window frame for every GIF frame.
            self.issued_revision = self.revision
        elif not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush(self) -> None:
        window = self._window()
        if window is None or not window.isVisible() or window.isMinimized():
            return
        self.issued_revision = self.revision
        self.flushes += 1
        # Queue a full dirty region after layouts/child updates have settled.
        # Never repaint synchronously from data callbacks or paintEvent itself.
        window.update(window.rect())

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        kind = event.type()
        if kind == QEvent.Type.Paint:
            self.paint_events += 1
            self._last_paint = monotonic()
            self.painted_revision = self.issued_revision
            if self.painted_revision >= self.revision:
                if self._recovery_attempted:
                    record_render_event("PAINT_RESUMED", self._summary())
                self._pending_since = None
                self._recovery_attempted = False
        elif kind == QEvent.Type.UpdateRequest:
            self.update_events += 1
        elif kind == QEvent.Type.Show:
            self._health_timer.start()
            self.request("show")
            record_render_event("PAINT_SHOW", self._summary())
        elif kind == QEvent.Type.Hide:
            self._health_timer.stop()
            self._flush_timer.stop()
            self._pending_since = None
            self._recovery_attempted = False
            record_render_event("PAINT_HIDE", self._summary())
        elif kind in {QEvent.Type.Resize, QEvent.Type.WindowStateChange,
                      QEvent.Type.ScreenChangeInternal}:
            self.request("geometry_or_screen")
        return False

    def _summary(self) -> str:
        window = self._window()
        if window is None:
            return f"window={self.name} deleted=True"
        handle = window.windowHandle()  # Does not force creation of a native HWND.
        pending = 0.0 if self._pending_since is None else monotonic() - self._pending_since
        return (
            f"window={self.name} reason={self._reason} desired={self.revision} "
            f"issued={self.issued_revision} painted={self.painted_revision} "
            f"paint_events={self.paint_events} update_events={self.update_events} flushes={self.flushes} "
            f"pending_ms={pending * 1000:.0f} last_paint_ms={(monotonic() - self._last_paint) * 1000:.0f} "
            f"visible={window.isVisible()} minimized={window.isMinimized()} "
            f"updates_enabled={window.updatesEnabled()} exposed={bool(handle and handle.isExposed())} "
            f"size={window.width()}x{window.height()} dpr={window.devicePixelRatioF():.2f}"
        )

    def _check(self) -> None:
        window = self._window()
        if window is None or not window.isVisible() or window.isMinimized():
            return
        now = monotonic()
        if now - self._last_report >= 30:
            record_render_event("PAINT_HEALTH", self._summary())
            self._last_report = now
        handle = window.windowHandle()
        if not handle or not handle.isExposed():
            # Covered/minimized/offscreen windows are not paint failures.
            self._pending_since = None
            self._recovery_attempted = False
            return
        if self._pending_since is None and self.painted_revision < self.revision:
            self.request("exposed_with_pending_changes")
        if (self._pending_since is None or now - self._pending_since < 2.0
                or self._recovery_attempted or now - self._last_recovery < 10.0):
            return
        self._last_recovery = now
        self._recovery_attempted = True
        record_render_event("PAINT_STALLED", self._summary(), warning=True)
        if window.updatesEnabled():
            # One bounded recovery outside all paint/data callbacks. Do not
            # reset windows, steal focus, rebuild rows or alter user opacity.
            self.issued_revision = self.revision
            window.repaint()
