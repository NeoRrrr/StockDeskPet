from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QLabel, QLineEdit, QPushButton, QSlider

from stock_pet import __version__
from stock_pet.models import Quote
from stock_pet.quote_provider import TencentQuoteProvider
from stock_pet.resources import asset_path
from stock_pet.symbols import normalize_symbol
from stock_pet.ui import (
    DEFAULT_A_SHARE_ETFS,
    DEFAULT_HK_WATCHLIST,
    FAVORITE_BUBBLE_PAGE_INTERVAL_MS,
    FAVORITE_REFRESH_INTERVAL_MS,
    FavoriteButton,
    FutuWatchlistImportDialog,
    INDEX_SYMBOLS,
    MARKET_TIMEZONE,
    OPEN_TAB_REFRESH_INTERVAL_MS,
    SPRITE_ANIMATIONS,
    TAB_MARKET_SUMMARIES,
    QuotePanel,
    StockPetWidget,
    ThemeSwitch,
    WatchlistDialog,
    _is_open_for_automatic_refresh,
    _is_visible_in_idle_bubble,
)


class QuotePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_packaged_app_icon_assets_are_readable(self) -> None:
        self.assertFalse(QIcon(str(asset_path("app_icon.png"))).isNull())
        self.assertFalse(QIcon(str(asset_path("app_icon.ico"))).isNull())

    def test_futu_import_dialog_respects_existing_items_and_remaining_slots(self) -> None:
        existing = [f"0000{index:02d}" for index in range(1, 20)]
        with patch("stock_pet.ui.QThreadPool.globalInstance"):
            dialog = FutuWatchlistImportDialog(
                TencentQuoteProvider(),  # type: ignore[arg-type]
                existing,
            )
            dialog._on_task_result(("groups", "", ["全部", "港股"], ""))
            dialog._on_task_result(
                (
                    "entries",
                    "全部",
                    [("000001", "平安银行"), ("00700", "腾讯控股"), ("01810", "小米集团-W")],
                    "",
                )
            )

            self.assertEqual(dialog.available_slots, 1)
            self.assertEqual(dialog.stock_list.count(), 3)
            self.assertEqual(dialog._checked_codes(), ["00700"])
            self.assertFalse(dialog.stock_list.item(0).flags() & Qt.ItemFlag.ItemIsEnabled)
            self.assertTrue(dialog.import_button.isEnabled())
            self.assertEqual(dialog.one_click_button.text(), "一键导入前 1 只")
            dialog._import_all_available()
            self.assertEqual(dialog.selected_codes, ["00700"])
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            dialog.close()

    def test_watchlist_dialog_merges_selected_futu_items(self) -> None:
        class ImportDialogStub:
            def __init__(self, *_args, **_kwargs) -> None:
                self.selected_codes = ["01810", "600519"]

            def exec(self):
                return QDialog.DialogCode.Accepted

        with patch("stock_pet.ui.FutuWatchlistImportDialog", ImportDialogStub):
            dialog = WatchlistDialog(
                ["00700"],
                3.0,
                60,
                True,
                provider=TencentQuoteProvider(),  # type: ignore[arg-type]
            )
            dialog._import_futu_watchlist()
            self.assertEqual(
                dialog._checked_watchlist_codes(),
                ["00700", "01810", "600519"],
            )
            self.assertIn("已从富途合并 2 只", dialog.import_status_label.text())
            dialog.close()

    def test_watchlist_dialog_supports_checkbox_plus_and_minus_editing(self) -> None:
        dialog = WatchlistDialog(["00700", "159516"], 3.0, 60, True)
        self.assertEqual(dialog.stock_list.count(), 2)
        self.assertEqual(dialog._checked_watchlist_codes(), ["00700", "159516"])

        dialog.stock_list.item(1).setCheckState(Qt.CheckState.Unchecked)
        self.assertEqual(dialog._checked_watchlist_codes(), ["00700"])
        dialog.stock_add_input.setText("01810")
        dialog._add_stock_item()
        self.assertEqual(dialog._checked_watchlist_codes(), ["00700", "01810"])

        dialog.stock_list.setCurrentItem(dialog.stock_list.item(0))
        dialog._remove_selected_stock_items()
        self.assertEqual(dialog._checked_watchlist_codes(), ["01810"])
        self.assertEqual(dialog.watchlist_count_label.text(), "已勾选 1/20")
        dialog.close()

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
            self.assertEqual(periodic[0], [*DEFAULT_HK_WATCHLIST, *hk_summary])
            self.assertEqual(periodic[1], list(INDEX_SYMBOLS))
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
            self.assertEqual(panel.findChild(QLabel, "title").text(), "股票桌宠")
            self.assertEqual(
                panel.findChild(QLabel, "footerVersion").text(), f"v{__version__}"
            )
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

    def test_cached_detail_is_immediate_and_latest_click_is_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(os.path.join(temp_dir, "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("market_defaults_v1_added", True)
            settings.setValue("a_share_etf_defaults_v1_added", True)
            settings.setValue("watchlist", ["159516", "515880"])
            panel = QuotePanel(TencentQuoteProvider(), settings)

            def quote(raw_symbol: str, name: str, price: float) -> Quote:
                return Quote(
                    symbol=normalize_symbol(raw_symbol),
                    name=name,
                    price=price,
                    previous_close=price,
                    open_price=price,
                    high=price,
                    low=price,
                    change=0.0,
                    change_percent=0.0,
                    volume=1.0,
                    volume_unit="手",
                    amount=1.0,
                    quote_time="2026-08-12 10:00:00",
                    source="缓存测试",
                )

            first = quote("159516", "半导体设备ETF", 0.701)
            second = quote("515880", "通信ETF", 1.234)
            panel.update_watchlist_quotes([first, second])

            with patch.object(panel.thread_pool, "start") as start:
                panel.fetch_symbol("159516")
                first_task = panel._active_task
                self.assertIsNotNone(first_task)
                self.assertIn("0.701", panel.price_label.text())

                panel.fetch_symbol("515880")
                self.assertIn("1.234", panel.price_label.text())
                self.assertEqual(panel._pending_fetch_symbol, "515880")
                self.assertEqual(start.call_count, 1)

                panel._on_quote(first_task, first)  # type: ignore[arg-type]
                self.assertIsNotNone(panel._active_task)
                self.assertEqual(panel._active_task.symbol, "515880")
                self.assertEqual(start.call_count, 2)

            panel._active_task = None
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
            favorite_button = first_row.findChild(FavoriteButton, "favoriteButton")
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
            index_button = index_row.findChild(FavoriteButton, "favoriteButton")
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

    def test_automatic_refresh_continues_for_ten_minutes_after_close(self) -> None:
        thursday = (2026, 8, 6)
        self.assertTrue(
            _is_open_for_automatic_refresh(
                "159516", datetime(*thursday, 15, 9, tzinfo=MARKET_TIMEZONE)
            )
        )
        self.assertFalse(
            _is_open_for_automatic_refresh(
                "159516", datetime(*thursday, 15, 10, tzinfo=MARKET_TIMEZONE)
            )
        )
        self.assertTrue(
            _is_open_for_automatic_refresh(
                "01810", datetime(*thursday, 16, 9, tzinfo=MARKET_TIMEZONE)
            )
        )
        self.assertFalse(
            _is_open_for_automatic_refresh(
                "01810", datetime(*thursday, 16, 10, tzinfo=MARKET_TIMEZONE)
            )
        )
        self.assertFalse(
            _is_open_for_automatic_refresh(
                "159516", datetime(*thursday, 11, 40, tzinfo=MARKET_TIMEZONE)
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

    def test_alert_only_repeats_after_rearm_and_persists_for_quote_date(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(os.path.join(temp_dir, "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("market_defaults_v1_added", True)
            settings.setValue("a_share_etf_defaults_v1_added", True)
            settings.setValue("watchlist", ["159516"])
            settings.setValue("favorites", [])
            settings.setValue("alert_threshold", 3.0)
            settings.setValue("alerts_enabled", True)

            def quote(percent: float, quote_time: str = "2026-08-12 10:00:00") -> Quote:
                return Quote(
                    symbol=normalize_symbol("159516"),
                    name="半导体设备ETF",
                    price=1.0,
                    previous_close=1.0,
                    open_price=1.0,
                    high=1.0,
                    low=1.0,
                    change=percent / 100.0,
                    change_percent=percent,
                    volume=1.0,
                    volume_unit="手",
                    amount=1.0,
                    quote_time=quote_time,
                    source="测试行情",
                )

            pet = StockPetWidget(settings)
            pet._animation.stop()
            pet._watch_timer.stop()
            pet._favorite_refresh_timer.stop()
            notifications: list[str] = []
            pet.alert_requested.connect(lambda _title, message: notifications.append(message))

            pet._on_watchlist_result(([quote(3.10)], []))
            pet._on_watchlist_result(([quote(3.25)], []))
            pet._on_watchlist_result(([quote(2.90)], []))
            pet._on_watchlist_result(([quote(3.20)], []))
            self.assertEqual(len(notifications), 1)

            pet._on_watchlist_result(([quote(2.70)], []))
            pet._on_watchlist_result(([quote(3.15)], []))
            self.assertEqual(len(notifications), 2)
            pet.close_provider()
            pet.panel.close()
            pet.close()

            restarted = StockPetWidget(settings)
            restarted._animation.stop()
            restarted._watch_timer.stop()
            restarted._favorite_refresh_timer.stop()
            restarted_notifications: list[str] = []
            restarted.alert_requested.connect(
                lambda _title, message: restarted_notifications.append(message)
            )
            restarted._on_watchlist_result(([quote(3.30)], []))
            self.assertEqual(restarted_notifications, [])

            restarted._on_watchlist_result(
                ([quote(3.30, "2026-08-13 10:00:00")], [])
            )
            self.assertEqual(len(restarted_notifications), 1)
            restarted.close_provider()
            restarted.panel.close()
            restarted.close()

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
