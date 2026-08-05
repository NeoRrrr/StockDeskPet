from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QLineEdit, QPushButton

from stock_pet import __version__
from stock_pet.models import Quote
from stock_pet.quote_provider import TencentQuoteProvider
from stock_pet.symbols import normalize_symbol
from stock_pet.ui import (
    DEFAULT_A_SHARE_ETFS,
    DEFAULT_HK_WATCHLIST,
    INDEX_SYMBOLS,
    SPRITE_ANIMATIONS,
    TAB_MARKET_SUMMARIES,
    QuotePanel,
    StockPetWidget,
    ThemeSwitch,
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
            panel.page_refresh_requested.connect(lambda symbols: captured.append(list(symbols)))
            panel.show()
            self.app.processEvents()

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
                )

            panel._display_quote(quote(9.69))
            self.assertIn(panel._up_color(), panel.price_label.styleSheet())
            self.assertEqual(panel.price_label.styleSheet(), panel.change_label.styleSheet())

            panel._display_quote(quote(-2.35))
            self.assertIn(panel._down_color(), panel.price_label.styleSheet())
            self.assertEqual(panel.price_label.styleSheet(), panel.change_label.styleSheet())

            panel._display_quote(quote(0.0))
            self.assertIn(panel._flat_color(), panel.price_label.styleSheet())
            self.assertEqual(panel.price_label.styleSheet(), panel.change_label.styleSheet())
            panel.close()

    def test_codexpet_skin_uses_distinct_interaction_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(os.path.join(temp_dir, "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("market_defaults_v1_added", True)
            settings.setValue("a_share_etf_defaults_v1_added", True)
            settings.setValue("skin", "ikunchick")
            pet = StockPetWidget(settings)
            pet._animation.stop()
            pet._watch_timer.stop()

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
