from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QLineEdit, QPushButton, QSlider

from stock_pet import __version__
from stock_pet.models import Quote
from stock_pet.quote_provider import TencentQuoteProvider
from stock_pet.symbols import normalize_symbol
from stock_pet.ui import (
    DEFAULT_A_SHARE_ETFS,
    DEFAULT_HK_WATCHLIST,
    FAVORITE_BUBBLE_PAGE_INTERVAL_MS,
    FAVORITE_REFRESH_INTERVAL_MS,
    INDEX_SYMBOLS,
    MARKET_TIMEZONE,
    OPEN_TAB_REFRESH_INTERVAL_MS,
    SPRITE_ANIMATIONS,
    TAB_MARKET_SUMMARIES,
    QuotePanel,
    StockPetWidget,
    ThemeSwitch,
    _is_visible_in_idle_bubble,
)


class QuotePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_current_tab_refresh_emits_only_visible_market(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(os.path.join(temp_dir, "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("market_defaults_v1_added", True)
            settings.setValue("a_share_etf_defaults_v1_added", True)
            settings.setValue("watchlist", [*DEFAULT_A_SHARE_ETFS, *DEFAULT_HK_WATCHLIST])
            panel = QuotePanel(TencentQuoteProvider(), settings)
            captured: list[list[str]] = []
            periodic: list[list[str]] = []
            panel.page_refresh_requested.connect(lambda symbols: captured.append(list(symbols)))
            panel.periodic_page_refresh_requested.connect(
                lambda symbols: periodic.append(list(symbols))
            )
            panel.show()
            self.app.processEvents()
            self.assertTrue(panel._open_tab_refresh_timer.isActive())
            self.assertEqual(
                panel._open_tab_refresh_timer.interval(), OPEN_TAB_REFRESH_INTERVAL_MS
            )

            tab_bar = panel.market_tabs.tabBar()
            tab_widths = [tab_bar.tabRect(index).width() for index in range(tab_bar.count())]
            self.assertLessEqual(max(tab_widths) - min(tab_widths), 1)
            self.assertEqual(tab_bar.width(), panel.market_tabs.width())

            self.assertEqual(
                panel.a_share_list.currentItem().data(Qt.ItemDataRole.UserRole),
                DEFAULT_A_SHARE_ETFS[0],
            )
            panel.refresh_current_tab()
            panel.market_tabs.setCurrentIndex(1)
            self.assertEqual(
                panel.hk_share_list.currentItem().data(Qt.ItemDataRole.UserRole),
                DEFAULT_HK_WATCHLIST[0],
            )
            panel.market_tabs.setCurrentIndex(2)

            a_summary = [symbol for _label, symbol in TAB_MARKET_SUMMARIES[0]]
            hk_summary = [symbol for _label, symbol in TAB_MARKET_SUMMARIES[1]]
            self.assertEqual(captured[0], [*DEFAULT_A_SHARE_ETFS, *a_summary])
            self.assertEqual(captured[1], [*DEFAULT_HK_WATCHLIST, *hk_summary])
            self.assertEqual(captured[2], list(INDEX_SYMBOLS))
            self.assertEqual(
                panel.index_list.currentItem().data(Qt.ItemDataRole.UserRole),
                INDEX_SYMBOLS[0],
            )
            panel._refresh_open_tab()
            self.assertEqual(periodic[-1], list(INDEX_SYMBOLS))
            self.assertTrue(panel.market_summary_frame.isHidden())
            button_texts = {button.text() for button in panel.findChildren(QPushButton)}
            self.assertIn("刷新当前页", button_texts)
            self.assertNotIn("检查自选", button_texts)
            self.assertEqual(panel.findChild(QLabel, "title").text(), f"股票桌宠 · v{__version__}")
            self.assertIsInstance(panel.symbol_input, QLineEdit)
            self.assertEqual(panel.findChildren(QComboBox), [])
            self.assertIsInstance(panel.theme_switch, ThemeSwitch)
            self.assertFalse(panel.theme_switch.isChecked())
            self.assertTrue(panel.theme_switch._sun_icon.isValid())
            self.assertTrue(panel.theme_switch._moon_icon.isValid())

            theme_changes: list[str] = []
            panel.theme_changed.connect(theme_changes.append)

            panel.toggle_theme()
            self.assertEqual(panel.current_theme, "beige")
            self.assertEqual(settings.value("theme"), "beige")
            self.assertTrue(panel.theme_switch.isChecked())
            self.assertEqual(theme_changes, ["beige"])
            panel.hide()
            self.app.processEvents()
            self.assertFalse(panel._open_tab_refresh_timer.isActive())
            panel.close()

    def test_main_price_uses_market_direction_color(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(os.path.join(temp_dir, "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("market_defaults_v1_added", True)
            settings.setValue("a_share_etf_defaults_v1_added", True)
            panel = QuotePanel(TencentQuoteProvider(), settings)

            def quote(change_percent: float) -> Quote:
                return Quote(
                    symbol=normalize_symbol("159516"),
                    name="半导体设备ETF国泰",
                    price=0.702,
                    previous_close=0.64,
                    open_price=0.642,
                    high=0.704,
                    low=0.642,
                    change=0.062,
                    change_percent=change_percent,
                    volume=1.0,
                    volume_unit="手",
                    amount=1.0,
                    quote_time="2026-08-05 13:45:00",
                    source="测试行情",
                    turnover_rate=16.18,
                )

            panel._display_quote(quote(9.69))
            self.assertIn(panel._up_color(), panel.price_label.styleSheet())
            self.assertEqual(panel.price_label.styleSheet(), panel.change_label.styleSheet())
            self.assertIn("换手 16.18%", panel.details_label.text())

            panel._display_quote(quote(-2.35))
            self.assertIn(panel._down_color(), panel.price_label.styleSheet())
            self.assertEqual(panel.price_label.styleSheet(), panel.change_label.styleSheet())

            panel._display_quote(quote(0.0))
            self.assertIn(panel._flat_color(), panel.price_label.styleSheet())
            self.assertEqual(panel.price_label.styleSheet(), panel.change_label.styleSheet())
            panel.close()

    def test_item_favorite_button_and_panel_opacity_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(os.path.join(temp_dir, "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("market_defaults_v1_added", True)
            settings.setValue("a_share_etf_defaults_v1_added", True)
            settings.setValue("watchlist", [*DEFAULT_A_SHARE_ETFS, *DEFAULT_HK_WATCHLIST])
            settings.setValue("favorites", [DEFAULT_A_SHARE_ETFS[0], "HSI"])
            settings.setValue("panel_opacity", 72)
            panel = QuotePanel(TencentQuoteProvider(), settings)
            panel.show()
            self.app.processEvents()

            self.assertIsInstance(panel.opacity_slider, QSlider)
            self.assertEqual((panel.opacity_slider.minimum(), panel.opacity_slider.maximum()), (90, 100))
            self.assertEqual(panel.opacity_slider.value(), 90)
            self.assertAlmostEqual(panel.windowOpacity(), 0.90, places=2)
            panel.set_panel_opacity(1)
            self.assertEqual(panel.opacity_value_label.text(), "90%")
            self.assertEqual(int(settings.value("panel_opacity")), 90)

            first_item = panel.a_share_list.item(0)
            first_row = panel.a_share_list.itemWidget(first_item)
            favorite_button = first_row.findChild(QPushButton, "favoriteButton")
            self.assertIsNotNone(favorite_button)
            self.assertTrue(favorite_button.isChecked())

            changes: list[list[str]] = []
            panel.favorites_changed.connect(lambda favorites: changes.append(list(favorites)))
            favorite_button.click()
            self.app.processEvents()
            self.assertNotIn(DEFAULT_A_SHARE_ETFS[0], panel.favorite_symbols)
            self.assertEqual(changes[-1], ["HSI"])

            index_item = panel.index_list.item(1)
            index_row = panel.index_list.itemWidget(index_item)
            index_button = index_row.findChild(QPushButton, "favoriteButton")
            self.assertTrue(index_button.isChecked())
            panel.close()

    def test_favorite_quotes_rotate_in_large_idle_bubble(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(os.path.join(temp_dir, "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("market_defaults_v1_added", True)
            settings.setValue("a_share_etf_defaults_v1_added", True)
            favorite_symbols = ["159516", "515880", "512200", "512800", "600519", "00700"]
            settings.setValue("favorites", favorite_symbols)
            pet = StockPetWidget(settings)
            pet._animation.stop()
            pet._watch_timer.stop()
            self.assertEqual(pet._favorite_refresh_timer.interval(), FAVORITE_REFRESH_INTERVAL_MS)
            pet._favorite_refresh_timer.stop()
            pet._favorite_carousel_timer.stop()

            def quote(raw_symbol: str, name: str, change_percent: float) -> Quote:
                return Quote(
                    symbol=normalize_symbol(raw_symbol),
                    name=name,
                    price=10.5,
                    previous_close=10.0,
                    open_price=10.0,
                    high=10.8,
                    low=9.9,
                    change=0.5,
                    change_percent=change_percent,
                    volume=1.0,
                    volume_unit="手",
                    amount=1.0,
                    quote_time="2026-08-05 13:45:00",
                    source="测试行情",
                )

            quotes = [
                quote(raw_symbol, f"收藏行情{index}", 2.5 if index % 2 else -1.2)
                for index, raw_symbol in enumerate(favorite_symbols, start=1)
            ]
            with patch("stock_pet.ui._is_visible_in_idle_bubble", return_value=True):
                pet._on_favorite_result((quotes, []))
                self.assertGreaterEqual(pet.bubble.width(), 280)
                for index in range(1, 6):
                    self.assertIn(f"收藏行情{index}", pet.bubble.text())
                self.assertNotIn("收藏行情6", pet.bubble.text())
                self.assertEqual(
                    pet._favorite_carousel_timer.interval(),
                    FAVORITE_BUBBLE_PAGE_INTERVAL_MS,
                )
                self.assertTrue(pet._favorite_carousel_timer.isActive())
                pet._show_next_favorite()
                self.assertIn("收藏行情6", pet.bubble.text())
            pet._favorite_carousel_timer.stop()
            pet.panel.close()
            pet.close()

    def test_idle_bubble_hides_markets_after_their_close(self) -> None:
        thursday = (2026, 8, 6)
        self.assertTrue(
            _is_visible_in_idle_bubble(
                "159516", datetime(*thursday, 14, 59, tzinfo=MARKET_TIMEZONE)
            )
        )
        self.assertFalse(
            _is_visible_in_idle_bubble(
                "159516", datetime(*thursday, 15, 0, tzinfo=MARKET_TIMEZONE)
            )
        )
        self.assertTrue(
            _is_visible_in_idle_bubble(
                "01810", datetime(*thursday, 15, 59, tzinfo=MARKET_TIMEZONE)
            )
        )
        self.assertFalse(
            _is_visible_in_idle_bubble(
                "01810", datetime(*thursday, 16, 0, tzinfo=MARKET_TIMEZONE)
            )
        )

    def test_idle_bubble_waits_without_a_visible_market(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(os.path.join(temp_dir, "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("market_defaults_v1_added", True)
            settings.setValue("a_share_etf_defaults_v1_added", True)
            settings.setValue("favorites", ["159516"])
            pet = StockPetWidget(settings)
            pet._animation.stop()
            pet._watch_timer.stop()
            pet._favorite_refresh_timer.stop()
            pet._favorite_carousel_timer.stop()
            quote = Quote(
                symbol=normalize_symbol("159516"),
                name="半导体设备ETF国泰",
                price=0.7,
                previous_close=0.64,
                open_price=0.642,
                high=0.704,
                low=0.642,
                change=0.06,
                change_percent=9.38,
                volume=1.0,
                volume_unit="手",
                amount=1.0,
                quote_time="2026-08-06 15:00:00",
                source="测试行情",
            )

            with patch("stock_pet.ui._is_visible_in_idle_bubble", return_value=False):
                pet._on_favorite_result(([quote], []))
                pet._show_next_favorite()

            self.assertTrue(pet.bubble.isHidden())
            self.assertEqual(pet._skin_animation, "idle")
            pet.panel.close()
            pet.close()

    def test_automatic_refreshes_do_not_trigger_refresh_animation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(os.path.join(temp_dir, "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("market_defaults_v1_added", True)
            settings.setValue("a_share_etf_defaults_v1_added", True)
            settings.setValue("favorites", ["159516"])
            pet = StockPetWidget(settings)
            pet._animation.stop()
            pet._watch_timer.stop()
            pet._favorite_refresh_timer.stop()
            pet._favorite_carousel_timer.stop()

            with patch("stock_pet.ui.QThreadPool.globalInstance") as pool:
                pet.refresh_favorites()
                self.assertEqual(pet._refresh_activity_sources, set())
                pet.scan_watchlist()
                self.assertEqual(pet._refresh_activity_sources, set())
                self.assertEqual(pool.return_value.start.call_count, 2)

            pet._favorite_task = None
            pet._watch_task = None
            pet.panel.close()
            pet.close()

    def test_codexpet_skin_uses_distinct_interaction_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(os.path.join(temp_dir, "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("market_defaults_v1_added", True)
            settings.setValue("a_share_etf_defaults_v1_added", True)
            settings.setValue("skin", "ikunchick")
            pet = StockPetWidget(settings)
            pet._animation.stop()
            pet._watch_timer.stop()
            pet._favorite_refresh_timer.stop()
            pet._favorite_carousel_timer.stop()

            self.assertEqual(pet.current_skin, "ikunchick")
            self.assertEqual(pet._skin_animation, "idle")
            self.assertEqual(SPRITE_ANIMATIONS["idle"][:2], (0, 6))
            self.assertEqual(SPRITE_ANIMATIONS["drag_right"][:2], (1, 8))
            self.assertEqual(SPRITE_ANIMATIONS["drag_left"][:2], (2, 8))
            self.assertEqual(SPRITE_ANIMATIONS["click"][:2], (3, 4))
            self.assertEqual(SPRITE_ANIMATIONS["refresh"][:2], (7, 6))

            idle_pixmap_key = pet.pet_label.pixmap().cacheKey()
            pet._play_skin_animation("click", restart=True, after_once="idle")
            self.assertEqual(pet._skin_animation, "click")
            self.assertNotEqual(pet.pet_label.pixmap().cacheKey(), idle_pixmap_key)

            pet._set_refresh_activity("page", True)
            self.assertEqual(pet._skin_animation_after_once, "refresh")
            for _ in range(4):
                pet._advance_skin_frame()
            self.assertEqual(pet._skin_animation, "refresh")

            pet._play_skin_animation("drag_left", restart=True)
            self.assertEqual(pet._skin_animation, "drag_left")
            pet._set_refresh_activity("page", False)
            self.assertEqual(pet._skin_animation, "drag_left")
            pet._play_skin_animation("idle", restart=True)
            self.assertEqual(pet._skin_animation, "idle")
            pet.panel.close()
            pet.close()


if __name__ == "__main__":
    unittest.main()
