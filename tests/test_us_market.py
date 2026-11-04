from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtWidgets import QApplication

from stock_pet.hybrid_quote_provider import HybridQuoteProvider, futu_code_for, quote_from_futu_row
from stock_pet.models import Quote, quote_is_older
from stock_pet.quote_provider import TencentQuoteProvider, parse_tencent_payload, parse_tencent_search_payload
from stock_pet.symbols import SymbolError, normalize_symbol, normalize_watchlist, partition_watchlist
from stock_pet.ui import (
    DEFAULT_US_WATCHLIST, DEFAULT_US_STOCK_NAMES, US_DEFAULTS_SETTINGS_KEY,
    FAVORITE_TAB, INDEX_TAB, INDEX_SYMBOLS, MARKET_TIMEZONE, QuotePanel,
    _is_open_for_automatic_refresh, _is_visible_in_idle_bubble, _normalize_stock_names,
    _merge_default_us_watchlist,
)


def us_payload(key="usNVDA"):
    fields = [""] * 75
    for index, value in {
        1: "英伟达", 3: "206.69", 4: "202.06", 5: "207.96",
        30: "2026-11-03 16:00:02", 31: "4.63", 32: "2.29",
        33: "209.14", 34: "205.93", 35: "USD", 36: "91943391",
        37: "19084610286", 38: "0.38", 57: "117.21", 59: "-4.52",
    }.items():
        fields[index] = value
    return f'v_{key}="{"~".join(fields)}";'


class USMarketTests(unittest.TestCase):
    def test_default_us_stocks_keep_requested_order(self):
        self.assertEqual(DEFAULT_US_WATCHLIST,
                         ("US.NVDA", "US.SPCX", "US.MU", "US.TSLA", "US.SNDK"))

    def test_default_merge_preserves_hidden_stocks_and_existing_order(self):
        active, catalog = _merge_default_us_watchlist(["01810", "US.MU"],
                                                     ["01810", "US.NVDA", "US.MU"])
        self.assertEqual(active, ["01810", "US.MU", "US.SPCX", "US.TSLA", "US.SNDK"])
        self.assertEqual(catalog, ["01810", "US.NVDA", "US.MU", "US.SPCX", "US.TSLA", "US.SNDK"])
        self.assertEqual(_merge_default_us_watchlist(active, catalog), (active, catalog))

    def test_default_merge_never_evicts_full_or_nearly_full_catalog(self):
        codes = [str(n).zfill(5) for n in range(1, 101)]
        self.assertEqual(_merge_default_us_watchlist(codes, codes), (codes, codes))
        active, catalog = _merge_default_us_watchlist(codes[:98], codes[:98])
        self.assertEqual(active, [*codes[:98], "US.NVDA", "US.SPCX"])
        self.assertEqual(active, catalog)
        self.assertEqual(len(catalog), 100)

    def test_us_aliases_round_trip_without_affecting_existing_ids(self):
        for raw in ("NVDA", "nvda", "US.NVDA", "NVDA.US", "usNVDA"):
            with self.subTest(raw=raw):
                symbol = normalize_symbol(raw)
                self.assertEqual((symbol.code, symbol.provider_symbol, symbol.display_code),
                                 ("US.NVDA", "usNVDA", "NVDA.US"))
                self.assertEqual(symbol.currency, "USD")
        self.assertEqual(normalize_symbol("USM").code, "US.USM")
        self.assertEqual(normalize_symbol("usm").code, "US.USM")
        self.assertEqual(normalize_symbol("600519").market, "sh")
        self.assertEqual(normalize_symbol("00700").market, "hk")
        self.assertEqual(normalize_symbol("GOLD").market, "gold")
        self.assertEqual(normalize_symbol("US.GOLD").market, "us")
        for raw in ("BRK.B", "BRK-B", "US.BRK.B", "BRK.B.US", "usBRK.B"):
            self.assertEqual(normalize_symbol(raw).code, "US.BRK.B")
        for raw in ("US.123", "KR.005930", "AAPL.OQ", "US.AAPL/path"):
            with self.assertRaises(SymbolError):
                normalize_symbol(raw)

    def test_us_watchlist_is_separate_and_keeps_canonical_ids(self):
        self.assertEqual(normalize_watchlist(["NVDA", "US.NVDA", "US.GOLD"]),
                         ["US.NVDA", "US.GOLD"])
        self.assertEqual(partition_watchlist(["01810", "NVDA", "159516"]),
                         (["159516"], ["01810"], ["US.NVDA"]))
        self.assertEqual(_normalize_stock_names({"usNVDA": "英伟达"}), {"usNVDA": "英伟达"})

    def test_all_default_us_indices_use_points_and_skip_futu(self):
        for code, key in (("DJI", "us.DJI"), ("IXIC", "us.IXIC"), ("SPX", "us.INX")):
            self.assertIn(code, INDEX_SYMBOLS)
            symbol = normalize_symbol(code)
            self.assertEqual(normalize_symbol(key).code, code)
            self.assertEqual(symbol.provider_symbol, key)
            self.assertEqual(symbol.currency, "PTS")
            self.assertIsNone(futu_code_for(symbol))
            self.assertIsNone(parse_tencent_payload(us_payload(key), symbol).turnover_rate)
            with self.assertRaises(SymbolError):
                normalize_watchlist([code])
        self.assertEqual(futu_code_for(normalize_symbol("AAPL")), "US.AAPL")
        self.assertEqual(futu_code_for(normalize_symbol("BRK.B")), "US.BRK.B")

    def test_tencent_us_amount_volume_turnover_and_timestamp(self):
        quote = parse_tencent_payload(us_payload(), normalize_symbol("NVDA"))
        self.assertEqual(quote.amount, 19084610286)
        self.assertEqual(quote.volume, 91943391)
        self.assertEqual(quote.volume_unit, "股")
        self.assertEqual(quote.turnover_rate, .38)
        self.assertEqual(quote.quote_time, "2026-11-03 16:00:02")
        self.assertEqual(quote.symbol.currency, "USD")

    def test_search_strips_venue_but_preserves_share_class(self):
        results = parse_tencent_search_payload(
            'v_hint="us~nvda.oq~英伟达~ywd~GP^us~brk.b.n~伯克希尔B~b~GP^'
            'us~qqq.oq~纳指ETF~q~ETF^us~nvda.oq~重复~n~GP^us~.dji~道指~d~ZS"'
        )
        self.assertEqual([r.symbol.code for r in results], ["US.NVDA", "US.BRK.B", "US.QQQ"])
        urls = []
        def transport(url):
            urls.append(url)
            return b'v_hint="us~aapl.oq~Apple~a~GP"'
        provider = TencentQuoteProvider(transport=transport)
        self.assertEqual(provider.search("US.AAPL")[0].symbol.code, "US.AAPL")
        self.assertIn("q=AAPL&", urls[0])

    def test_mixed_stock_index_batch_and_no_opend_fallback(self):
        def transport(_url):
            return (us_payload() + us_payload("us.DJI")).encode("gb18030")
        provider = HybridQuoteProvider(tencent=TencentQuoteProvider(transport=transport),
                                       opend_probe=lambda *_: False)
        quotes, errors = provider.fetch_batch(["NVDA", "DJI", "US.NVDA"])
        self.assertFalse(errors)
        self.assertEqual([q.symbol.code for q in quotes], ["US.NVDA", "DJI"])
        self.assertIn("富途不可用", quotes[0].source)
        self.assertNotIn("富途不可用", quotes[1].source)

    def test_futu_us_time_is_not_duplicated(self):
        quote = quote_from_futu_row({"last_price": 200, "prev_close_price": 199,
                                    "data_date": "2026-11-03", "data_time": "2026-11-03 16:00:00"},
                                   normalize_symbol("NVDA"))
        self.assertEqual(quote.quote_time, "2026-11-03 16:00:00")

    def test_quote_order_uses_exchange_timezone_with_dst(self):
        quote = parse_tencent_payload(us_payload(), normalize_symbol("NVDA"))
        for date, utc_hour in (("2026-11-03", 21), ("2026-07-03", 20)):
            current = replace(quote, quote_time=f"{date} 16:00:00")
            same = replace(quote, quote_time=f"{date} {utc_hour}:00:00+00:00")
            older = replace(quote, quote_time=f"{date} {utc_hour-1}:59:59+00:00")
            self.assertFalse(quote_is_older(same, current))
            self.assertTrue(quote_is_older(older, current))

    def test_poll_and_bubble_windows_handle_dst_and_china_saturday(self):
        cases = [
            ((2026, 7, 6, 21, 29), False), ((2026, 7, 6, 21, 30), True),
            ((2026, 11, 2, 22, 29), False), ((2026, 11, 2, 22, 30), True),
            ((2026, 7, 4, 4, 29), True), ((2026, 7, 4, 4, 30), False),
            ((2026, 11, 7, 5, 29), True), ((2026, 11, 7, 5, 30), False),
            ((2026, 11, 8, 0, 0), False), ((2026, 11, 9, 0, 0), False),
        ]
        for args, expected in cases:
            for symbol in ("DJI", "IXIC", "SPX"):
                with self.subTest(at=args, symbol=symbol):
                    at = datetime(*args, tzinfo=MARKET_TIMEZONE)
                    self.assertEqual(_is_open_for_automatic_refresh(symbol, at), expected)
                    self.assertEqual(_is_visible_in_idle_bubble(symbol, at), expected)
                    self.assertEqual(_is_open_for_automatic_refresh(symbol, at.astimezone(timezone.utc)), expected)


class USPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_us_tab_search_names_favorites_and_default_index_summaries(self):
        with tempfile.TemporaryDirectory() as directory, patch("stock_pet.ui.QThreadPool.globalInstance"), patch("stock_pet.ui.QTimer.singleShot"):
            settings = QSettings(str(Path(directory) / "test.ini"), QSettings.IniFormat)
            settings.setValue("watchlist", ["NVDA", "01810", "159516"])
            settings.setValue("market_defaults_v1_added", True)
            settings.setValue("a_share_etf_defaults_v1_added", True)
            settings.setValue(US_DEFAULTS_SETTINGS_KEY, True)
            panel = QuotePanel(TencentQuoteProvider(), settings)
            try:
                panel.show()
                self.app.processEvents()
                self.assertEqual(panel.market_tabs.count(), 5)
                self.assertTrue(panel.market_tabs.tabText(FAVORITE_TAB).startswith("收藏"))
                bar = panel.market_tabs.tabBar()
                self.assertLessEqual(max(bar.tabRect(i).width() for i in range(5)) - min(bar.tabRect(i).width() for i in range(5)), 1)
                panel.market_tabs.setCurrentIndex(2)
                self.assertEqual(panel._current_tab_refresh_symbols(), ["US.NVDA", "DJI", "IXIC", "SPX"])
                self.assertIs(panel._current_stock_list(), panel.us_share_list)
                self.assertFalse(panel.market_summary_frame.isHidden())
                self.assertEqual(panel._extract_symbol_from_text("Apple · AAPL.US · 美股"), "US.AAPL")
                panel._schedule_name_search("NVDA")
                self.assertTrue(panel._search_timer.isActive())
                quote = parse_tencent_payload(us_payload(), normalize_symbol("NVDA"))
                panel.update_watchlist_quotes([quote])
                panel._display_quote(quote)
                self.assertIn("US$", panel.price_label.text())
                self.assertIn("美东", panel.status_label.text())
                panel.toggle_favorite("NVDA")
                panel.toggle_favorite("SPX")
                self.assertEqual(panel.favorite_symbols, ["US.NVDA", "SPX"])
                panel.market_tabs.setCurrentIndex(FAVORITE_TAB)
                self.assertEqual(panel._current_tab_list_symbols(), ["US.NVDA", "SPX"])
                panel.market_tabs.setCurrentIndex(INDEX_TAB)
                self.assertEqual(panel._current_tab_list_symbols(), list(INDEX_SYMBOLS))
                index_quote = replace(quote, symbol=normalize_symbol("SPX"), name="标普500")
                panel._display_quote(index_quote)
                self.assertNotIn("US$", panel.price_label.text())
                self.assertIn("指数点", panel.details_label.text())
            finally:
                panel.close()
                panel.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_new_and_existing_profiles_get_defaults_only_once(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as directory, patch("stock_pet.ui.QThreadPool.globalInstance"), patch("stock_pet.ui.QTimer.singleShot"):
                settings = QSettings(str(Path(directory) / "defaults.ini"), QSettings.IniFormat)
                if existing:
                    settings.setValue("watchlist", ["01810", "US.MU"])
                    settings.setValue("watchlist_catalog", ["01810", "US.MU"])
                    settings.setValue("market_defaults_v1_added", True)
                    settings.setValue("a_share_etf_defaults_v1_added", True)
                settings.setValue("favorites", ["HSI"])
                panel = QuotePanel(TencentQuoteProvider(), settings)
                try:
                    self.assertTrue(set(DEFAULT_US_WATCHLIST).issubset(panel.watchlist))
                    self.assertEqual(panel.us_share_list.count(), 5)
                    self.assertEqual(panel.favorite_symbols, ["HSI"])
                    self.assertTrue(settings.value(US_DEFAULTS_SETTINGS_KEY))
                    for code, name in DEFAULT_US_STOCK_NAMES.items():
                        self.assertIn(name, panel._quote_row_content(code)[0])
                    saved = list(panel.watchlist)
                    # Simulate deleting one stock and unchecking another.
                    settings.setValue("watchlist", [code for code in saved if code not in {"US.NVDA", "US.MU"}])
                    settings.setValue("watchlist_catalog", [code for code in saved if code != "US.NVDA"])
                    settings.sync()
                finally:
                    panel.close()
                    panel.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                reopened = QuotePanel(TencentQuoteProvider(), settings)
                try:
                    self.assertNotIn("US.NVDA", reopened.watchlist_catalog)
                    self.assertNotIn("US.MU", reopened.watchlist)
                    self.assertIn("US.MU", reopened.watchlist_catalog)
                    self.assertEqual(reopened.favorite_symbols, ["HSI"])
                finally:
                    reopened.close()
                    reopened.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == "__main__":
    unittest.main()
