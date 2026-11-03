from __future__ import annotations

from dataclasses import replace
import gc
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import weakref
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QThread, QThreadPool, QTimer
from PySide6.QtWidgets import QApplication

from stock_pet.hybrid_quote_provider import FutuQuoteProvider, HybridQuoteProvider
from stock_pet.models import Quote, quote_is_older
from stock_pet.quote_provider import QuoteError, TencentQuoteProvider
from stock_pet.symbols import normalize_symbol
from stock_pet.ui import QuotePanel, StockPetWidget, WatchlistTask


def quote(code="01810", price=40, stamp="2026-11-03 14:00:00"):
    return Quote(normalize_symbol(code), "测试行情", price, 39, 39, 41, 38,
                 price - 39, (price - 39) / 39 * 100, 100, "股", 4000, stamp, "Tencent fixture")


def payload(key, currency="HKD"):
    fields = [""] * 88
    fields[1] = "Fixture"
    fields[2] = key[2:]
    fields[3] = fields[4] = "40"
    fields[30] = "20261103140000"
    fields[75 if key.startswith("hk") else 82] = currency
    return f'v_{key}="' + "~".join(fields) + '";'


class BatchRefreshTests(unittest.TestCase):
    def test_tencent_watchlist_uses_two_requests_for_100_stocks(self):
        calls = []
        def transport(url):
            keys = url.split("q=", 1)[1].split(",")
            calls.append(keys)
            return "\n".join(payload(key, "CNY") for key in keys).encode("gb18030")
        provider = HybridQuoteProvider(mode="tencent", tencent=TencentQuoteProvider(transport))
        codes = [f"600{i:03}" for i in range(100)]
        results = []
        task = WatchlistTask(provider, [*codes, codes[0]])
        task.signals.finished.connect(results.append)
        with patch.object(provider.futu, "fetch", side_effect=AssertionError("must not use OpenD")):
            task.run()
        self.assertEqual([len(keys) for keys in calls], [50, 50])
        self.assertEqual(len(results[0][0]), 100)
        self.assertEqual(results[0][1], [])

    def test_failed_chunk_preserves_other_successful_chunks(self):
        for failed_chunk in (0, 1):
            with self.subTest(failed_chunk=failed_chunk):
                calls = []
                def transport(url):
                    calls.append(url)
                    if len(calls) - 1 == failed_chunk:
                        raise TimeoutError("fixture timeout")
                    return "\n".join(payload(k) for k in url.split("q=", 1)[1].split(",")).encode()
                quotes, errors = TencentQuoteProvider(transport).fetch_batch([f"600{i:03}" for i in range(100)])
                self.assertEqual(len(quotes), 50)
                self.assertEqual(len(errors), 50)
                self.assertEqual(len(calls), 2)

    def test_missing_invalid_and_unparseable_rows_do_not_discard_good_rows(self):
        raw = payload("hk01810") + 'v_hk00700="bad";'
        quotes, errors = TencentQuoteProvider(lambda _: raw.encode()).fetch_batch(
            ["01810", "00700", "00005", "invalid"]
        )
        self.assertEqual([q.symbol.code for q in quotes], ["01810"])
        self.assertEqual(len(errors), 3)

    def test_opend_failure_falls_back_in_one_batch(self):
        provider = HybridQuoteProvider()
        expected = [quote("01810"), quote("00700"), quote("GOLD")]
        with patch.object(provider.futu, "fetch", side_effect=QuoteError("offline")) as futu_fetch, \
             patch.object(provider.futu, "close"), \
             patch.object(provider.tencent, "fetch_batch", return_value=(expected, [])) as batch:
            quotes, errors = provider.fetch_batch(["01810", "00700", "GOLD"])
        self.assertEqual(futu_fetch.call_count, 1)
        batch.assert_called_once_with(["01810", "00700", "GOLD"])
        self.assertEqual(errors, [])
        self.assertIn("富途不可用", quotes[0].source)
        self.assertEqual(quotes[2].source, expected[2].source)

    def test_switch_to_tencent_does_not_wait_for_opend_and_cannot_reopen_retired_context(self):
        futu = FutuQuoteProvider()
        provider = HybridQuoteProvider(futu=futu)
        switched = threading.Event()
        futu._lock.acquire()
        switcher = threading.Thread(target=lambda: (provider.set_mode("tencent"), switched.set()))
        try:
            switcher.start()
            self.assertTrue(switched.wait(1), "source switch blocked on the old provider lock")
            self.assertIsNot(provider.futu, futu)
            provider.set_mode("auto")
            self.assertIsNot(provider.futu, futu)
        finally:
            futu._lock.release()
            switcher.join(2)
        with self.assertRaisesRegex(QuoteError, "旧 OpenD"):
            futu._ensure_context()
        self.assertEqual(provider.generation, 2)

    def test_late_failure_from_retired_provider_does_not_reset_new_source(self):
        provider = HybridQuoteProvider()
        old = provider.futu
        provider.set_mode("tencent")
        provider.set_mode("auto")
        with patch.object(provider.futu, "close") as close:
            provider._mark_futu_failure(QuoteError("old error"), old, 0)
        close.assert_not_called()
        self.assertEqual(provider._retry_after, 0)
        self.assertNotEqual(provider._last_futu_error, "old error")

    def test_currency_comes_from_quote_not_listing_exchange(self):
        for code, currency in (("01810", "HKD"), ("81810", "CNY")):
            with self.subTest(code=code):
                result = TencentQuoteProvider(lambda _: payload("hk" + code, currency).encode()).fetch(code)
                self.assertEqual(result.symbol.currency, currency)
        # Indices stay unitless even though the endpoint supplies HKD.
        result = TencentQuoteProvider(lambda _: payload("hkHSI").encode()).fetch("HSI")
        self.assertEqual(result.symbol.currency, "PTS")

    def test_market_timestamp_handles_missing_times_timezones_and_equal_time_corrections(self):
        current = quote()
        self.assertTrue(quote_is_older(replace(current, quote_time=""), current))
        self.assertTrue(quote_is_older(replace(current, quote_time="2026-11-03 13:59:59"), current))
        self.assertFalse(quote_is_older(replace(current, price=42), current))
        self.assertFalse(quote_is_older(replace(current, quote_time="2026-11-03T06:00:01+00:00"), current))
        self.assertFalse(quote_is_older(current, replace(current, quote_time="")))


class QuoteRefreshUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for target, kwargs in (
            ("stock_pet.ui.QTimer.singleShot", {}),
            ("stock_pet.ui.QThreadPool.globalInstance", {}),
            ("stock_pet.ui._is_visible_in_idle_bubble", {"return_value": True}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        settings = QSettings(str(Path(directory.name) / "test.ini"), QSettings.IniFormat)
        for key, value in {
            "quote_source": "tencent", "skin": "default", "alerts_enabled": False,
            "market_defaults_v1_added": True, "a_share_etf_defaults_v1_added": True,
            "watchlist": ["01810", "159516"], "favorites": ["01810"],
        }.items():
            settings.setValue(key, value)
        self.pet = StockPetWidget(settings)
        self.panel = self.pet.panel
        self.panel._set_symbol_input_display("01810")
        self.addCleanup(self.close_pet)

    def close_pet(self):
        for timer in self.pet.findChildren(QTimer) + self.panel.findChildren(QTimer):
            timer.stop()
        self.pet.close_provider()
        self.panel.close()
        self.pet.close()
        self.panel.deleteLater()
        self.pet.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def favorite_result(self, quotes):
        self.pet._favorite_task = WatchlistTask(self.pet.provider, ["01810"])
        self.pet._on_favorite_result((quotes, []))

    def page_result(self, quotes):
        self.pet._watch_task = WatchlistTask(self.pet.provider, ["01810"])
        self.pet._on_watchlist_result((quotes, []))

    def test_late_page_cannot_rewind_favorites_detail_or_alerts(self):
        newest = quote(price=41, stamp="2026-11-03 14:00:10")
        self.favorite_result([newest])
        self.pet.alerts_enabled = True
        with patch.object(self.pet, "_process_alert_quote") as alerts:
            self.page_result([quote()])
        alerts.assert_not_called()
        self.assertEqual(self.panel._quote_cache["hk01810"], newest)
        self.assertEqual(self.pet._favorite_quotes, [newest])
        self.assertIn("41", self.panel.price_label.text())

    def test_page_and_detail_updates_also_update_favorite_cache(self):
        self.favorite_result([quote()])
        newest = quote(price=42, stamp="2026-11-03 14:00:20")
        self.page_result([newest])
        self.favorite_result([quote()])
        self.assertEqual(self.pet._favorite_quotes, [newest])
        self.panel.fetch_symbol("01810")
        latest = replace(newest, price=43, quote_time="2026-11-03 14:00:30")
        self.panel._on_quote(self.panel._active_task, latest)
        self.assertEqual(self.pet._favorite_quotes, [latest])

    def test_unchanged_quotes_can_repopulate_new_favorites(self):
        current = quote()
        self.panel.update_watchlist_quotes([current])
        self.pet._favorite_quotes.clear()
        self.favorite_result([current])
        self.assertEqual(self.pet._favorite_quotes, [current])

    def test_previous_symbol_failure_does_not_clear_new_tab_detail(self):
        self.panel.update_watchlist_quotes([quote()])
        self.panel.fetch_symbol("002821")
        task = self.panel._active_task
        self.panel.market_tabs.setCurrentIndex(1)
        before = (self.panel.name_label.text(), self.panel.price_label.text(), self.panel.status_label.text())
        self.panel._on_fetch_error(task, "old timeout")
        self.assertEqual(before, (self.panel.name_label.text(), self.panel.price_label.text(), self.panel.status_label.text()))

    def test_clear_input_cancels_pending_selection_without_starting_obsolete_fetch(self):
        self.panel.fetch_symbol("01810")
        task = self.panel._active_task
        self.panel.fetch_symbol("002821")
        self.panel.symbol_input.clear()
        self.panel._on_quote(task, quote())
        self.assertIsNone(self.panel._active_task)
        self.assertEqual(self.panel.symbol_input.text(), "")

    def test_uncached_selection_shows_placeholder_instead_of_previous_stock(self):
        self.panel.update_watchlist_quotes([quote()])
        self.panel.fetch_symbol("002821")
        self.assertEqual(self.panel.price_label.text(), "--")
        self.assertIn("002821", self.panel.name_label.text())
        self.assertNotIn("4000", self.panel.details_label.text())

    def test_repeated_click_failure_does_not_leave_details_waiting_forever(self):
        self.panel.fetch_symbol("002821")
        task = self.panel._active_task
        self.panel.fetch_symbol("002821")
        self.panel._on_fetch_error(task, "synthetic timeout")
        self.assertIsNone(self.panel._active_task)
        self.assertIsNone(self.panel._pending_fetch_symbol)
        self.assertEqual(self.panel.name_label.text(), "行情拉取失败")
        self.assertIn("synthetic timeout", self.panel.status_label.text())

    def test_old_source_result_is_discarded_and_current_detail_is_retried(self):
        self.panel.fetch_symbol("01810")
        task = self.panel._active_task
        self.pet.provider.set_mode("auto")
        self.panel._on_quote(task, quote())
        self.assertNotIn("hk01810", self.panel._quote_cache)
        self.assertEqual(self.panel._active_task.generation, self.pet.provider.generation)
        self.assertIsNot(self.panel._active_task, task)

    def test_old_source_batch_does_not_populate_cache_or_initialization(self):
        self.pet._watch_task = WatchlistTask(self.pet.provider, ["01810"])
        self.pet._favorite_task = WatchlistTask(self.pet.provider, ["01810"])
        self.pet.provider.set_mode("auto")
        self.pet._on_watchlist_result(([quote()], []))
        self.pet._on_favorite_result(([quote()], []))
        self.assertNotIn("hk01810", self.panel._quote_cache)
        self.assertEqual(self.pet._favorite_quotes, [])
        self.assertNotIn("hk01810", self.pet._page_refresh_initialized_keys)

    def test_repeated_detail_does_not_reapply_styles_or_text(self):
        current = quote()
        self.panel._display_quote(current)
        with patch.object(self.panel.price_label, "setText") as text, \
             patch.object(self.panel.price_label, "setStyleSheet") as style, \
             patch.object(self.panel.symbol_input, "setText") as input_text, \
             patch.object(self.panel.details_label, "setText") as details:
            for _ in range(100):
                self.panel._display_quote(current)
            text.assert_not_called()
            style.assert_not_called()
            input_text.assert_not_called()
            details.assert_not_called()

    def test_rmb_counter_uses_yuan_in_row_detail_and_bubble(self):
        self.panel.favorite_symbols = ["81810"]
        self.pet.favorite_symbols = ["81810"]
        self.panel._set_symbol_input_display("81810")
        current = replace(quote("81810"), symbol=replace(normalize_symbol("81810"), currency="CNY"))
        self.panel.update_watchlist_quotes([current])
        self.pet._render_favorite_page(0)
        self.assertTrue(self.panel.price_label.text().startswith("¥"))
        self.assertNotIn("HK$", self.panel._quote_row_content("81810")[0])
        self.assertNotIn("HK$", self.pet.bubble.text())


class DetailTaskLifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_real_worker_callbacks_run_on_ui_and_finished_tasks_are_released(self):
        class Provider:
            calls = 0
            def fetch(self, code):
                self.calls += 1
                if self.calls % 2 == 0:
                    raise QuoteError("synthetic timeout")
                return quote(code)

        with tempfile.TemporaryDirectory() as directory:
            settings = QSettings(str(Path(directory) / "test.ini"), QSettings.IniFormat)
            for key, value in {"watchlist": ["01810"], "market_defaults_v1_added": True,
                               "a_share_etf_defaults_v1_added": True}.items():
                settings.setValue(key, value)
            panel = QuotePanel(Provider(), settings)
            refs, gui_threads = [], []
            panel.loading_changed.connect(lambda _: gui_threads.append(QThread.currentThread() == self.app.thread()))
            try:
                for _ in range(100):
                    panel.fetch_symbol("01810")
                    refs.append(weakref.ref(panel._active_task))
                    deadline = time.monotonic() + 2
                    while panel._active_task is not None and time.monotonic() < deadline:
                        self.app.processEvents()
                        time.sleep(.001)
                    self.assertIsNone(panel._active_task)
                self.assertTrue(QThreadPool.globalInstance().waitForDone(2000))
                self.app.processEvents()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                gc.collect()
                self.assertTrue(all(gui_threads))
                self.assertEqual(sum(ref() is not None for ref in refs), 0)
            finally:
                panel.close()
                panel.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
