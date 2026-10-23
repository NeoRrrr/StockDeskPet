from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from stock_pet.expression_skin import (
    EXPRESSION_ASSETS, EXPRESSION_SKIN_KEY, ExpressionSkin, expression_for,
)
from stock_pet.ui import StockPetWidget


class ExpressionSkinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_reactions_use_stable_time_and_interaction_states(self) -> None:
        self.assertEqual(expression_for("idle", 0, 0), "deepseek-idle")
        self.assertEqual(expression_for("idle", 3.9, 3.9), "frame-idle-blink")
        self.assertEqual(expression_for("idle", 30, 30), "reaction-relaxed")
        self.assertEqual(expression_for("idle", 90, 90), "reaction-deepseek-rice")
        self.assertEqual(expression_for("idle", 600, 600), "reaction-hungry")
        self.assertEqual(expression_for("idle", 1800, 1800), "reaction-pillow")
        self.assertEqual(expression_for("idle", 3600, 3600), "reaction-sleeping")
        self.assertEqual(expression_for("click", 0, 0), "reaction-cheerful")
        self.assertEqual(expression_for("drag_left", 0, 0), "reaction-whip-frightened")
        self.assertEqual(expression_for("drag_right", 0, 0), "reaction-shocked")
        self.assertEqual(expression_for("refresh", 0, 0), "reaction-desk-coding")
        self.assertEqual(expression_for("refresh", 0.4, 0.4), "frame-desk-coding-hands-up")
        for state in ("idle", "click", "jump", "drag_left", "drag_right", "refresh", "waiting", "failed", "review"):
            for elapsed in (0, 0.4, 1, 4, 6, 12, 30, 60, 90, 120, 150, 600, 1800, 3600):
                self.assertIn(expression_for(state, elapsed, elapsed), EXPRESSION_ASSETS)

    def test_repeated_state_preserves_clock_and_render_uses_cache(self) -> None:
        now = [0.0]
        skin = ExpressionSkin(pixel_ratio=2.0, clock=lambda: now[0])
        now[0] = 31
        skin.set_animation("idle")
        self.assertEqual(skin._started_at, 0)
        skin.set_animation("click")
        self.assertEqual(skin._elapsed["idle"], 31)
        self.assertFalse(skin.finished)
        now[0] = 32.3
        self.assertTrue(skin.finished)
        skin.set_animation("idle")
        self.assertEqual(skin._elapsed["idle"], 31)
        with patch("stock_pet.expression_skin.asset_path", side_effect=AssertionError("disk read during tick")):
            frame = skin.render()
        self.assertFalse(frame.isNull())
        self.assertTrue(frame.hasAlphaChannel())
        self.assertEqual(frame.devicePixelRatio(), 2)
        self.assertEqual(frame.deviceIndependentSize().width(), 150)

    def test_widget_switches_skin_and_restores_refresh_after_click(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch("stock_pet.ui.QTimer.singleShot"), patch("stock_pet.expression_skin.monotonic") as clock:
            clock.return_value = 0
            settings = QSettings(os.path.join(directory, "settings.ini"), QSettings.IniFormat)
            settings.setValue("skin", EXPRESSION_SKIN_KEY)
            settings.setValue("quote_source", "tencent")
            pet = StockPetWidget(settings)
            try:
                self.assertEqual(pet.current_skin, EXPRESSION_SKIN_KEY)
                self.assertIsNotNone(pet._expression_skin)
                self.assertIsNone(pet._skin_sheet)
                pet._play_skin_animation("click", restart=True, after_once="idle")
                pet._set_refresh_activity("page", True)
                self.assertEqual(pet._skin_animation_after_once, "refresh")
                clock.return_value = 1.3
                pet._advance_skin_frame()
                self.assertEqual(pet._skin_animation, "refresh")
                self.assertEqual(pet._expression_skin.animation, "refresh")
                pet._play_skin_animation("drag_left")
                pet._set_refresh_activity("page", False)
                self.assertEqual(pet._skin_animation, "drag_left")
                pet._play_skin_animation("idle", restart=True)
                pet.set_skin("ikunchick")
                self.assertIsNone(pet._expression_skin)
                self.assertIsNotNone(pet._skin_sheet)
                pet.set_skin(EXPRESSION_SKIN_KEY)
                self.assertEqual(settings.value("skin"), EXPRESSION_SKIN_KEY)
                pet.set_skin("default")
                self.assertIsNone(pet._expression_skin)
                self.assertIsNone(pet._skin_sheet)
                with patch("stock_pet.ui.ExpressionSkin", side_effect=FileNotFoundError):
                    pet.set_skin(EXPRESSION_SKIN_KEY)
                self.assertEqual(pet.current_skin, "default")
                self.assertEqual(settings.value("skin"), "default")
                self.assertFalse(pet.pet_label.pixmap().isNull())
            finally:
                pet.close_provider()
                pet.panel.close()
                pet.close()


if __name__ == "__main__":
    unittest.main()
