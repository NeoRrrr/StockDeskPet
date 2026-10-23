from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from stock_pet.expression_skin import EXPRESSION_SKIN_KEY
from stock_pet.models import Quote
from stock_pet.symbols import normalize_symbol
from stock_pet.ui import StockPetWidget


def quote_for(code: str = "01810") -> Quote:
    return Quote(
        symbol=normalize_symbol(code), name="测试行情", price=40,
        previous_close=39, open_price=39, high=41, low=38, change=1,
        change_percent=2.56, volume=100, volume_unit="股", amount=4000,
        quote_time="2026-10-23 11:00:00", source="Test fixture",
    )


class PetLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.now = 100.0
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        for target, options in (
            ("stock_pet.ui.QTimer.singleShot", {}),
            ("stock_pet.ui.QThreadPool.globalInstance", {}),
            ("stock_pet.ui.monotonic", {"side_effect": lambda: self.now}),
            ("stock_pet.expression_skin.monotonic", {"side_effect": lambda: self.now}),
            ("stock_pet.ui._is_open_for_automatic_refresh", {"return_value": False}),
            ("stock_pet.ui._is_visible_in_idle_bubble", {"return_value": True}),
        ):
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)
        settings = QSettings(os.path.join(directory.name, "settings.ini"), QSettings.IniFormat)
        settings.setValue("quote_source", "tencent")
        settings.setValue("alerts_enabled", False)
        settings.setValue("favorites", ["01810"])
        settings.setValue("skin", EXPRESSION_SKIN_KEY)
        self.pet = StockPetWidget(settings)
        self.addCleanup(self._close_pet)

    def _close_pet(self) -> None:
        self.pet._animation.stop()
        self.pet._watch_timer.stop()
        self.pet._favorite_refresh_timer.stop()
        self.pet._favorite_carousel_timer.stop()
        self.pet.close_provider()
        self.pet.panel.close()
        self.pet.close()

    def test_skin_switch_with_cached_quotes_and_zero_bubble_rows(self) -> None:
        self.pet._favorite_quotes = [quote_for()]
        self.pet._apply_bubble_settings(0, 10, 150)
        for skin in ("default", "ikunchick", EXPRESSION_SKIN_KEY):
            with self.subTest(skin=skin):
                self.pet.set_skin(skin)
                self.assertEqual(self.pet.current_skin, skin)
                self.assertTrue(self.pet.bubble.isHidden())
                self.assertFalse(self.pet._favorite_carousel_timer.isActive())

    def test_failed_result_cannot_show_disabled_or_closed_market_bubble(self) -> None:
        self.pet._apply_bubble_settings(0, 10, 100)
        self.pet._on_favorite_result(([], ["timeout"]))
        self.assertTrue(self.pet.bubble.isHidden())
        self.pet._apply_bubble_settings(5, 10, 100)
        with patch("stock_pet.ui._is_visible_in_idle_bubble", return_value=False):
            self.pet._on_favorite_result(([], ["timeout"]))
        self.assertTrue(self.pet.bubble.isHidden())
        self.assertFalse(self.pet._favorite_carousel_timer.isActive())

    def test_success_keeps_cache_while_bubble_is_disabled(self) -> None:
        self.pet._apply_bubble_settings(0, 10, 100)
        self.pet.refresh_favorites()
        self.pet._on_favorite_result(([quote_for()], []))
        self.assertEqual(len(self.pet._favorite_quotes), 1)
        self.assertIn("hk01810", self.pet._favorite_refresh_initialized_keys)
        self.assertTrue(self.pet.bubble.isHidden())
        self.pet._apply_bubble_settings(5, 10, 100)
        self.assertFalse(self.pet.bubble.isHidden())

    def test_existing_error_bubble_disappears_when_market_closes(self) -> None:
        self.pet.refresh_favorites()
        self.pet._on_favorite_result(([], ["timeout"]))
        self.assertFalse(self.pet.bubble.isHidden())
        with patch("stock_pet.ui._is_visible_in_idle_bubble", return_value=False):
            self.pet.refresh_favorites()
        self.assertTrue(self.pet.bubble.isHidden())
        self.assertIsNone(self.pet._favorite_task)

    def test_late_result_after_removing_last_favorite_stays_hidden(self) -> None:
        for result in (([quote_for()], []), ([], ["timeout"])):
            with self.subTest(result=result):
                self.pet._apply_favorites(["01810"])
                self.pet.refresh_favorites()
                self.pet._apply_favorites([])
                self.pet._on_favorite_result(result)
                self.assertTrue(self.pet.bubble.isHidden())
                self.assertEqual(self.pet._favorite_quotes, [])
                self.assertEqual(self.pet._favorite_refresh_initialized_keys, set())
                self.assertEqual(self.pet._favorite_initial_retries, {})

    def test_replaced_favorite_does_not_show_error_for_old_success(self) -> None:
        self.pet.refresh_favorites()
        self.pet._apply_favorites(["00700"])
        self.pet._on_favorite_result(([quote_for()], []))
        self.assertEqual(self.pet._favorite_quotes, [])
        self.assertEqual(self.pet.bubble.text(), "正在加载收藏行情…")
        self.assertNotIn("hk01810", self.pet._favorite_refresh_initialized_keys)

    def test_closed_market_favorite_retries_then_stops_after_success(self) -> None:
        self.pet.refresh_favorites()
        self.assertEqual(self.pet._favorite_task.symbols, ["01810"])
        self.assertEqual(self.pet._favorite_refresh_initialized_keys, set())
        self.pet._on_favorite_result(([], ["timeout"]))
        self.assertEqual(self.pet._favorite_initial_retries["hk01810"], (1, 130))
        self.now = 129
        self.pet.refresh_favorites()
        self.assertIsNone(self.pet._favorite_task)
        self.now = 130
        self.pet.refresh_favorites()
        self.assertIsNotNone(self.pet._favorite_task)
        self.pet._on_favorite_result(([quote_for()], []))
        self.assertEqual(self.pet._favorite_initial_retries, {})
        self.pet.refresh_favorites()
        self.assertIsNone(self.pet._favorite_task)

    def test_closed_market_page_retries_only_failed_symbols(self) -> None:
        self.pet.refresh_market_page_silent(["01810", "159516"])
        self.pet._on_watchlist_result(([quote_for()], ["159516: timeout"]))
        self.assertEqual(self.pet._page_refresh_initialized_keys, {"hk01810"})
        self.now = 129
        self.pet.refresh_market_page_silent(["01810", "159516"])
        self.assertIsNone(self.pet._watch_task)
        self.now = 130
        self.pet.refresh_market_page_silent(["01810", "159516"])
        self.assertEqual(self.pet._watch_task.symbols, ["159516"])
        self.pet._on_watchlist_result(([quote_for("159516")], []))
        self.assertEqual(self.pet._page_initial_retries, {})
        self.pet.refresh_market_page_silent(["01810", "159516"])
        self.assertIsNone(self.pet._watch_task)

    def test_retry_backoff_is_capped_and_market_open_bypasses_it(self) -> None:
        initialized: set[str] = set()
        retries: dict[str, tuple[int, float]] = {}
        for expected_delay in (30, 60, 120, 240, 300, 300):
            self.pet._record_initial_refresh_result(["01810"], [], initialized, retries)
            self.assertEqual(retries["hk01810"][1] - self.now, expected_delay)
            self.assertEqual(self.pet._automatic_symbols_with_initialization(["01810"], initialized, retries), [])
            with patch("stock_pet.ui._is_open_for_automatic_refresh", return_value=True):
                self.assertEqual(self.pet._automatic_symbols_with_initialization(["01810"], initialized, retries), ["01810"])
            self.now += expected_delay
        self.pet._page_initial_retries = retries
        self.now -= 1
        self.assertEqual(self.pet._automatic_symbols_with_initialization(["01810"], initialized, retries), [])
        self.pet.refresh_market_page(["01810"])
        self.assertIsNotNone(self.pet._watch_task)
        self.assertTrue(self.pet._watch_manual)

    def test_hidden_animation_stops_and_resumes_at_current_expression_time(self) -> None:
        self.assertFalse(self.pet._animation.isActive())
        self.pet.show()
        self.assertTrue(self.pet._animation.isActive())
        self.pet._play_skin_animation("click", restart=True, after_once="idle")
        self.pet.hide_all()
        self.assertFalse(self.pet._animation.isActive())
        self.assertTrue(self.pet._favorite_refresh_timer.isActive())
        self.now += 2
        renderer = self.pet._expression_skin
        with patch.object(renderer, "render", wraps=renderer.render) as render:
            for _ in range(20):
                self.pet._animate()
            self.assertEqual(render.call_count, 0)
            self.pet.show()
            self.assertGreater(render.call_count, 0)
        self.assertTrue(self.pet._animation.isActive())
        self.assertEqual(self.pet._skin_animation, "idle")


if __name__ == "__main__":
    unittest.main()
