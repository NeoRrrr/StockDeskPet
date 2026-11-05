from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer, Qt, QtMsgType, qInstallMessageHandler, qWarning
from PySide6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from stock_pet.diagnostics import UiStallMonitor, record_render_event
from stock_pet.painting import WindowPaintGuard


class PaintGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = QWidget()
        self.window.setAttribute(Qt.WA_TranslucentBackground)
        self.window.resize(200, 80)
        layout = QVBoxLayout(self.window)
        self.label = QLabel("before")
        layout.addWidget(self.label)
        self.guard = WindowPaintGuard(self.window, "test_window")
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.window.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def pump(self, milliseconds=55):
        loop = QEventLoop()
        QTimer.singleShot(milliseconds, loop.quit)
        loop.exec()

    def show_ready(self):
        self.window.show()
        self.pump()

    def test_changes_coalesce_to_one_full_window_request(self):
        self.show_ready()
        before = self.guard.flushes
        for index in range(20):
            self.label.setText(str(index))
            self.guard.request("test_change")
        self.pump()
        self.assertEqual(self.guard.flushes - before, 1)
        self.assertEqual(self.guard.painted_revision, self.guard.revision)
        self.assertIsNone(self.guard._pending_since)
        self.assertGreater(self.guard.paint_events, 0)
        self.assertGreater(self.guard.update_events, 0)

    def test_hidden_windows_stay_hidden_and_timers_stop(self):
        self.guard.request("hidden_data")
        self.assertFalse(self.guard._flush_timer.isActive())
        self.assertFalse(self.guard._health_timer.isActive())
        self.show_ready()
        self.guard.request("pending")
        self.window.hide()
        self.assertFalse(self.guard._flush_timer.isActive())
        self.assertFalse(self.guard._health_timer.isActive())
        self.assertIsNone(self.guard._pending_since)
        self.guard._check()
        self.assertFalse(self.window.isVisible())

    def test_animation_observation_does_not_schedule_a_second_full_frame(self):
        self.show_ready()
        before = self.guard.flushes
        self.label.setText("frame")
        self.guard.request("pet_frame", full=False)
        self.assertFalse(self.guard._flush_timer.isActive())
        self.pump()
        self.assertEqual(self.guard.flushes, before)
        self.assertEqual(self.guard.painted_revision, self.guard.revision)

    def test_missing_paint_gets_one_bounded_recovery_and_records_resumption(self):
        self.show_ready()
        self.guard.request("test_stall")
        self.guard._flush_timer.stop()
        self.guard._pending_since -= 3
        with patch.object(self.window, "repaint") as repaint, patch("stock_pet.painting.record_render_event") as log:
            self.guard._check()
            self.guard.request("more_changes")
            self.guard._check()
            self.assertEqual(repaint.call_count, 1)
            self.assertTrue(any(call.args[0] == "PAINT_STALLED" for call in log.call_args_list))
            self.guard.issued_revision = self.guard.revision
            self.guard.eventFilter(self.window, QEvent(QEvent.Type.Paint))
            self.assertTrue(any(call.args[0] == "PAINT_RESUMED" for call in log.call_args_list))
            self.assertIsNone(self.guard._pending_since)
            self.guard.request("second_stall")
            self.guard._pending_since -= 3
            self.guard._check()
            self.assertEqual(repaint.call_count, 1)  # 10-second recovery cooldown.

    def test_intentionally_disabled_updates_are_logged_not_overridden(self):
        self.show_ready()
        self.window.setUpdatesEnabled(False)
        self.guard.request("disabled")
        self.guard._pending_since -= 3
        with patch.object(self.window, "repaint") as repaint, patch("stock_pet.painting.record_render_event") as log:
            self.guard._check()
            repaint.assert_not_called()
            self.assertTrue(any("updates_enabled=False" in call.args[1] for call in log.call_args_list))
        self.assertFalse(self.window.updatesEnabled())
        self.window.setUpdatesEnabled(True)

    def test_unexposed_window_is_not_a_stall_and_requeues_when_exposed(self):
        self.show_ready()
        self.guard.request("covered")
        self.guard._flush_timer.stop()
        self.guard._pending_since -= 3
        with patch.object(self.window, "windowHandle", return_value=SimpleNamespace(isExposed=lambda: False)), patch.object(self.window, "repaint") as repaint:
            self.guard._check()
            self.assertIsNone(self.guard._pending_since)
            repaint.assert_not_called()
        self.guard._check()
        self.assertTrue(self.guard._flush_timer.isActive())

    def test_new_changes_do_not_postpone_old_pending_paint(self):
        self.show_ready()
        self.guard.request("first")
        first = self.guard._pending_since
        for _ in range(20):
            self.guard.request("later")
        self.assertEqual(self.guard._pending_since, first)


class RenderingDiagnosticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.monitor = UiStallMonitor(log_dir=Path(directory.name))
        self.addCleanup(self.monitor.stop)

    def test_native_warning_is_buffered_and_collapsed_from_worker_thread(self):
        def worker():
            for _ in range(5):
                self.monitor._on_qt_message(QtMsgType.QtWarningMsg, SimpleNamespace(category="qt.qpa"),
                                            "UpdateLayeredWindowIndirect failed")
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertNotIn("QT_RENDER", self.monitor.log_path.read_text(encoding="utf-8"))
        self.monitor._heartbeat()
        text = self.monitor.log_path.read_text(encoding="utf-8")
        self.assertIn("QT_RENDER category=qt.qpa count=5", text)
        self.assertIn("UpdateLayeredWindowIndirect failed", text)

    def test_real_qt_handler_and_paint_health_are_logged_without_widget_data(self):
        qWarning("QBackingStore test diagnostic")
        record_render_event("PAINT_HEALTH", "window=quote_panel desired=5 painted=5")
        self.monitor._heartbeat()
        text = self.monitor.log_path.read_text(encoding="utf-8")
        self.assertIn("QBackingStore test diagnostic", text)
        self.assertIn("PAINT_HEALTH window=quote_panel", text)
        self.assertIn("render_guard=1", text)

    def test_exception_hook_records_frame_not_values_and_restores_hooks(self):
        previous = self.monitor._previous_exception_handler
        self.monitor._previous_exception_handler = Mock()
        try:
            raise ValueError("PRIVATE_QUOTE_VALUE")
        except ValueError:
            self.monitor._on_python_exception(*sys.exc_info())
        self.monitor._previous_exception_handler.assert_called_once()
        self.monitor._previous_exception_handler = previous
        self.monitor.stop()
        text = self.monitor.log_path.read_text(encoding="utf-8")
        self.assertIn("UI_CALLBACK_EXCEPTION type=ValueError", text)
        self.assertIn("test_painting.py", text)
        self.assertNotIn("PRIVATE_QUOTE_VALUE", text)
        self.assertIs(sys.excepthook, previous)
        installed = qInstallMessageHandler(self.monitor._previous_qt_handler)
        self.assertIs(installed, self.monitor._previous_qt_handler)

    def test_hooks_installed_by_another_owner_are_not_removed(self):
        other_qt = lambda *_args: None
        other_python = lambda *_args: None
        qInstallMessageHandler(other_qt)
        sys.excepthook = other_python
        try:
            self.monitor.stop()
            self.assertIs(sys.excepthook, other_python)
            self.assertIs(qInstallMessageHandler(self.monitor._previous_qt_handler), other_qt)
        finally:
            qInstallMessageHandler(self.monitor._previous_qt_handler)
            sys.excepthook = self.monitor._previous_exception_handler

    def test_message_buffers_are_bounded_and_ignore_unrelated_payloads(self):
        context = SimpleNamespace(category="test")
        self.monitor._on_qt_message(QtMsgType.QtWarningMsg, context, "PRIVATE_QUOTE_VALUE")
        for index in range(100):
            self.monitor._on_qt_message(QtMsgType.QtWarningMsg, context, f"QPainter {index} " + "x" * 2000)
        self.assertEqual(len(self.monitor._qt_messages), 64)
        self.monitor.stop()
        text = self.monitor.log_path.read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_QUOTE_VALUE", text)
        self.assertNotIn("x" * 1001, text)


if __name__ == "__main__":
    unittest.main()
