from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

from stock_pet.diagnostics import UiStallMonitor


class UiStallMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_monitor(self, directory, **options):
        monitor = UiStallMonitor(log_dir=Path(directory), **options)
        self.addCleanup(monitor.stop)
        return monitor

    def test_healthy_qt_heartbeats_do_not_dump(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self.make_monitor(directory, threshold_seconds=.5, heartbeat_ms=20)
            loop = QEventLoop()
            QTimer.singleShot(650, loop.quit)
            loop.exec()
            monitor.stop()
            text = monitor.log_path.read_text(encoding="utf-8")
            self.assertIn("SESSION_START", text)
            self.assertNotIn("Timeout", text)
            self.assertNotIn("UI_STALL_RECOVERED", text)

    def test_native_watchdog_dumps_while_ui_blocked_then_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self.make_monitor(directory, threshold_seconds=.15, heartbeat_ms=20)
            monitor.set_context_provider(lambda: "panel_visible=True tab=0")
            monitor._heartbeat()
            time.sleep(.35)  # Deliberately block the main thread, no Qt events.
            text = monitor.log_path.read_text(encoding="utf-8")
            self.assertIn("Timeout", text)
            self.assertIn("test_native_watchdog_dumps_while_ui_blocked_then_recovers", text)
            time.sleep(.2)
            self.assertEqual(monitor.log_path.read_text(encoding="utf-8").count("Timeout"), 1)
            monitor._heartbeat()
            time.sleep(.3)
            monitor._heartbeat()
            monitor.stop()
            text = monitor.log_path.read_text(encoding="utf-8")
            self.assertEqual(text.count("Timeout"), 2)
            self.assertEqual(text.count("UI_STALL_RECOVERED"), 2)
            self.assertIn("panel_visible=True tab=0", text)
            self.assertIn("SESSION_STOP", text)

    def test_rotation_cancels_native_writer_before_closing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("stock_pet.diagnostics.faulthandler.dump_traceback_later") as arm, \
                 patch("stock_pet.diagnostics.faulthandler.cancel_dump_traceback_later") as cancel:
                monitor = self.make_monitor(directory, max_bytes=1024)
                events = []
                cancel.side_effect = lambda: events.append("cancel")
                original_rollover = monitor._handler.doRollover
                def rollover():
                    self.assertEqual(events[-1], "cancel")
                    events.append("rollover")
                    original_rollover()
                with patch.object(monitor._handler, "doRollover", side_effect=rollover):
                    for _ in range(5):
                        monitor._handler.stream.write("x" * 1200)
                        monitor._handler.flush()
                        monitor._heartbeat()
                self.assertEqual(events.count("rollover"), 5)
                self.assertEqual(arm.call_count, 6)
                monitor.stop()
                monitor.stop()
            self.assertLessEqual(len(list(Path(directory).glob("ui-stalls.log*"))), 3)

    @unittest.skipUnless(os.name == "nt", "Windows native GIL-blocking probe")
    def test_native_watchdog_can_capture_a_call_holding_the_gil(self):
        import ctypes
        with tempfile.TemporaryDirectory() as directory:
            monitor = self.make_monitor(directory, threshold_seconds=.15, heartbeat_ms=20)
            # PyDLL deliberately retains the GIL, unlike WinDLL/CDLL.
            kernel = ctypes.PyDLL("kernel32")
            kernel.Sleep.argtypes = [ctypes.c_ulong]
            kernel.Sleep.restype = None
            kernel.Sleep(350)
            monitor.stop()
            text = monitor.log_path.read_text(encoding="utf-8")
            self.assertIn("Timeout", text)
            self.assertIn("test_native_watchdog_can_capture_a_call_holding_the_gil", text)

    def test_context_failure_disables_monitor_without_throwing_into_qt(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = self.make_monitor(directory)
            def broken_context():
                raise RuntimeError("deleted widget")
            monitor.set_context_provider(broken_context)
            monitor._heartbeat()
            self.assertFalse(monitor.active)
            self.assertIn("deleted widget", monitor.last_error)

    def test_unwritable_log_dir_does_not_prevent_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("stock_pet.diagnostics.Path.mkdir", side_effect=PermissionError("read only")):
                monitor = self.make_monitor(directory)
            self.assertFalse(monitor.active)
            self.assertIn("read only", monitor.last_error)


if __name__ == "__main__":
    unittest.main()
