from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication

from stock_pet.animated_skin import DSH_SKIN_KEY
from stock_pet.models import Quote
from stock_pet.symbols import normalize_symbol
from stock_pet.ui import INDEX_SYMBOLS, MARKET_TIMEZONE, StockPetWidget, _is_visible_in_idle_bubble


class FavoriteIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = QSettings(str(Path(directory.name) / "settings.ini"), QSettings.IniFormat)
        self.codes = [*INDEX_SYMBOLS, "01810", "159516"]
        for key, value in {
            "quote_source": "tencent", "alerts_enabled": False,
            "favorites": self.codes, "favorite_bubble_page_size": 10,
            "skin": "default",
        }.items():
            self.settings.setValue(key, value)
        self.at = datetime(2026, 10, 28, 14, 45, tzinfo=MARKET_TIMEZONE)
        for target, options in (
            ("stock_pet.ui.QTimer.singleShot", {}),
            ("stock_pet.ui.QThreadPool.globalInstance", {}),
            ("stock_pet.ui._is_visible_in_idle_bubble", {
                "side_effect": lambda symbol: _is_visible_in_idle_bubble(symbol, self.at)
            }),
        ):
            patcher = patch(target, **options)
            patcher.start()
            self.addCleanup(patcher.stop)

    def make_pet(self):
        pet = StockPetWidget(self.settings)
        def cleanup():
            pet._animation.stop()
            pet._watch_timer.stop()
            pet._favorite_refresh_timer.stop()
            pet._favorite_carousel_timer.stop()
            pet.close_provider()
            pet.panel.close()
            pet.close()
            if pet._animated_skin is not None:
                pet._animated_skin.close()
            pet.panel.deleteLater()
            pet.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.addCleanup(cleanup)
        return pet

    def load_favorites(self):
        pet = self.make_pet()
        quotes = [Quote(
            normalize_symbol(code), code, 40, 39, 39, 41, 38, 1,
            2.56, 100, "股", 4000, "2026-10-28 14:45:00", "Test fixture",
        ) for code in self.codes]
        pet._on_favorite_result((quotes, []))
        return pet

    def test_all_indices_and_gold_render_alongside_stocks(self):
        pet = self.load_favorites()
        self.assertEqual([q.symbol.code for q in pet._visible_favorite_quotes()], self.codes)
        for code in self.codes:
            self.assertIn(code, pet.bubble.text())
        self.assertIn("40点", pet.bubble.text())
        self.assertIn("US$", pet.bubble.text())
        self.assertEqual(pet._favorite_page_count(), 1)

    def test_indices_keep_existing_close_grace_and_weekend_rules(self):
        pet = self.load_favorites()
        for at, expected in (
            (self.at.replace(hour=15, minute=29), set(self.codes)),
            (self.at.replace(hour=15, minute=30), {"HSI", "HSTECH", "HSCEI", "GOLD", "01810"}),
            (self.at.replace(hour=16, minute=29), {"HSI", "HSTECH", "HSCEI", "GOLD", "01810"}),
            (self.at.replace(hour=16, minute=30), {"GOLD"}),
            (datetime(2026, 10, 31, 14, 45, tzinfo=MARKET_TIMEZONE), set()),
        ):
            with self.subTest(at=at):
                self.at = at
                self.assertEqual({q.symbol.code for q in pet._visible_favorite_quotes()}, expected)
                pet._show_next_favorite()
                self.assertEqual(pet.bubble.isHidden(), not bool(expected))

    def test_index_favorites_participate_in_pagination(self):
        pet = self.load_favorites()
        pet._apply_bubble_settings(2, 10, 100)
        self.assertEqual(pet._favorite_page_count(), 5)
        pet._favorite_index = 0
        for offset in range(0, len(self.codes), 2):
            pet._show_next_favorite()
            for code in self.codes[offset:offset + 2]:
                self.assertIn(code, pet.bubble.text())

    def test_new_profile_uses_dsh_skin(self):
        self.settings.remove("skin")
        pet = self.make_pet()
        self.assertEqual(pet.current_skin, DSH_SKIN_KEY)
        self.assertIsNotNone(pet._animated_skin)
        self.assertFalse(pet.pet_label.pixmap().isNull())

    def test_existing_skin_choices_are_preserved(self):
        for saved in ("default", "ikunchick", DSH_SKIN_KEY):
            with self.subTest(saved=saved):
                self.settings.setValue("skin", saved)
                pet = self.make_pet()
                self.assertEqual(pet.current_skin, saved)
                self.assertEqual(self.settings.value("skin"), saved)

    def test_invalid_saved_skin_uses_new_default(self):
        self.settings.setValue("skin", "skin-no-longer-available")
        pet = self.make_pet()
        self.assertEqual(pet.current_skin, DSH_SKIN_KEY)

    def test_missing_default_animation_still_has_a_static_fallback(self):
        self.settings.remove("skin")
        with patch("stock_pet.ui.AnimatedSkin", side_effect=OSError("missing asset")):
            pet = self.make_pet()
        self.assertEqual(pet.current_skin, "default")
        self.assertIsNone(pet._animated_skin)
        self.assertFalse(pet.pet_label.pixmap().isNull())


if __name__ == "__main__":
    unittest.main()
