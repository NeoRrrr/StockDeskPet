from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
from PySide6.QtWidgets import QApplication

from stock_pet.models import Quote
from stock_pet.quote_provider import TencentQuoteProvider
from stock_pet.symbols import normalize_symbol
from stock_pet.ui import QuotePanel


def quote_for(code: str) -> Quote:
    return Quote(normalize_symbol(code), "测试行情", 10.1, 10, 10, 10.2, 9.9,
                 .1, 1., 1000, "股", 10000, "2026-10-28 14:00:00", "test")


class IncrementalQuoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pool = patch("stock_pet.ui.QThreadPool.globalInstance")
        self.pool.start()
        self.addCleanup(self.pool.stop)
        settings = QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.IniFormat)
        self.codes = [f"600{i:03}" for i in range(26)] + ["00700", "01810"]
        for key, value in {
            "market_defaults_v1_added": True, "a_share_etf_defaults_v1_added": True,
            "watchlist": self.codes, "favorites": ["600000", "01810", "HSI"],
        }.items():
            settings.setValue(key, value)
        self.panel = QuotePanel(TencentQuoteProvider(), settings)
        self.addCleanup(self.close_panel)

    def close_panel(self):
        self.panel.close()
        self.panel.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_hidden_refresh_reuses_rows_and_updates_all_occurrences(self):
        panel = self.panel
        original = {key: list(rows) for key, rows in panel._quote_rows.items()}
        self.assertFalse(panel.isVisible())
        quotes = [quote_for(code) for code in ("600000", "01810", "HSI")]
        with patch.object(panel, "_refresh_watchlist_lists") as rebuild:
            panel.update_watchlist_quotes(quotes)
            rebuild.assert_not_called()
        for key, rows in original.items():
            for index, (item, row, compact) in enumerate(rows):
                self.assertIs(panel._quote_rows[key][index][0], item)
                self.assertIs(panel._quote_rows[key][index][1], row)
        for quote in quotes:
            rows = panel._quote_rows[quote.symbol.provider_symbol]
            self.assertEqual(len(rows), 2)
            for item, row, compact in rows:
                self.assertIn("测试行情", row.text_label.text())
                self.assertIn("+1.00%", row.text_label.text())
                self.assertEqual(item.data(Qt.ItemDataRole.AccessibleTextRole), row.text_label.text())
                self.assertEqual(row._text_color, panel._up_color())
        with patch.object(panel, "_update_quote_rows", wraps=panel._update_quote_rows) as update:
            panel.update_watchlist_quotes(quotes)
            update.assert_called_once_with(set())

    def test_refresh_preserves_selection_scroll_and_focus(self):
        panel = self.panel
        panel.show()
        panel._open_tab_refresh_timer.stop()
        self.app.processEvents()
        stock_list = panel.a_share_list
        selected = stock_list.item(0)
        stock_list.setCurrentItem(selected)
        scroll = stock_list.verticalScrollBar()
        scroll.setValue(scroll.maximum())
        stock_list.setFocus()
        self.app.processEvents()
        before_scroll = scroll.value()
        before_focus = QApplication.focusWidget()
        for index in range(4):
            panel.update_watchlist_quotes([replace(quote_for("600000"), price=10.1 + index)])
            self.app.processEvents()
        self.assertIs(stock_list.currentItem(), selected)
        self.assertEqual(scroll.value(), before_scroll)
        self.assertIs(QApplication.focusWidget(), before_focus)
        self.assertGreater(before_scroll, 0)

    def test_theme_changes_recolor_existing_rows_and_stars(self):
        panel = self.panel
        quote = quote_for("600000")
        panel.update_watchlist_quotes([quote])
        item, row, _ = panel._quote_rows[quote.symbol.provider_symbol][0]
        panel.toggle_theme()
        self.assertIs(panel._quote_rows[quote.symbol.provider_symbol][0][1], row)
        self.assertEqual(row._text_color, panel._up_color())
        self.assertEqual(row.favorite_button.theme, panel.current_theme)
        self.assertEqual(item.foreground().color().name(), panel._up_color())
        down = replace(quote, change=-.1, change_percent=-1)
        panel.update_watchlist_quotes([down])
        self.assertEqual(row._text_color, panel._down_color())

    def test_rebuild_replaces_index_and_drops_deleted_favorite_rows(self):
        panel = self.panel
        panel.toggle_favorite("600000")
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        key = normalize_symbol("600000").provider_symbol
        self.assertEqual(len(panel._quote_rows[key]), 1)
        panel.update_watchlist_quotes([quote_for("600000")])
        panel.toggle_favorite("600000")
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        panel.update_watchlist_quotes([replace(quote_for("600000"), price=12)])
        self.assertEqual(len(panel._quote_rows[key]), 2)
        for _, row, _ in panel._quote_rows[key]:
            self.assertIn("¥12", row.text_label.text())
            self.assertTrue(row.favorite_button.isChecked())

    def test_search_only_quote_cache_is_still_pruned(self):
        panel = self.panel
        panel._set_symbol_input_display("002821", "")
        quote = quote_for("002821")
        panel.update_watchlist_quotes([quote])
        self.assertIn(quote.symbol.provider_symbol, panel._quote_cache)
        self.assertIn("10.1", panel.price_label.text())
        panel._set_symbol_input_display("600000", "")
        panel.update_watchlist_quotes([])
        self.assertNotIn(quote.symbol.provider_symbol, panel._quote_cache)

    def test_empty_favorites_placeholder_follows_theme(self):
        panel = self.panel
        for code in list(panel.favorite_symbols):
            panel.toggle_favorite(code)
        item = panel.favorite_list.item(0)
        panel.toggle_theme()
        self.assertIs(panel.favorite_list.item(0), item)
        self.assertEqual(item.foreground().color().name(), "#7a899b")


if __name__ == "__main__":
    unittest.main()
