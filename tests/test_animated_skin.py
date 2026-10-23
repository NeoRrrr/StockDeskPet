from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QSize
from PySide6.QtGui import QImageReader, QMovie
from PySide6.QtWidgets import QApplication

from stock_pet.animated_skin import AnimatedSkin, DEFAULT_CLICK_ACTION, DSH_ASSET_ROOT, DSH_SKIN_KEY
from stock_pet.resources import asset_path
from stock_pet.ui import StockPetWidget


class AnimatedSkinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_skin(self, **kwargs):
        skin = AnimatedSkin(**kwargs)
        self.addCleanup(skin.deleteLater)
        self.addCleanup(skin.close)
        return skin

    def end_cycle(self, skin):
        # CacheNone is a sequential stream, not an arbitrary seekable frame cache.
        while skin.movie.currentFrameNumber() < skin.movie.frameCount() - 1:
            self.assertTrue(skin.movie.jumpToNextFrame())
        self.assertTrue(skin.movie.jumpToFrame(0))
        self.app.processEvents()

    def test_all_106_original_assets_are_valid_transparent_and_categorized(self):
        skin = self.make_skin()
        names = [name for group in skin.groups for name in group["actions"]]
        self.assertEqual(len(names), 106)
        self.assertEqual(set(names), set(skin.catalog["files"]))
        for filename in skin.catalog["files"].values():
            with self.subTest(filename=filename):
                path = asset_path(f"{DSH_ASSET_ROOT}/{filename}")
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), skin.catalog["sha256"][filename])
                reader = QImageReader(str(path))
                self.assertGreater(reader.imageCount(), 1)
                frame = reader.read()
                self.assertFalse(frame.isNull())
                self.assertTrue(frame.hasAlphaChannel())
                self.assertEqual(frame.pixelColor(0, 0).alpha(), 0)

    def test_high_dpi_frame_streaming_pause_resume_and_invalid_action(self):
        skin = self.make_skin(pixel_ratio=2)
        self.assertEqual(skin.render().devicePixelRatio(), 2)
        self.assertEqual(skin.render().deviceIndependentSize().toSize(), QSize(266, 150))
        self.assertEqual(skin.movie.cacheMode(), QMovie.CacheNone)
        self.assertEqual(skin.movie.state(), QMovie.NotRunning)
        skin.set_active(True)
        self.assertEqual(skin.movie.state(), QMovie.Running)
        skin.set_active(False)
        self.assertEqual(skin.movie.state(), QMovie.Paused)
        skin.set_active(True)
        self.assertEqual(skin.movie.state(), QMovie.Running)
        before = skin.movie
        self.assertFalse(skin.play_action("not-an-action"))
        self.assertIs(skin.movie, before)
        skin.close()
        skin.set_active(True)
        self.assertEqual(skin.movie.state(), QMovie.NotRunning)

    def test_idle_actions_run_between_idle_loops_and_can_be_disabled(self):
        skin = self.make_skin()
        skin.set_active(True)
        self.end_cycle(skin)
        self.assertEqual(skin.action, skin.catalog["idle"])
        self.end_cycle(skin)
        self.assertIn(skin.action, skin.catalog["idle_pool"])
        self.end_cycle(skin)
        self.assertEqual(skin.action, skin.catalog["idle"])
        skin.set_random_idle(False)
        for _ in range(3):
            self.end_cycle(skin)
        self.assertEqual(skin.action, skin.catalog["idle"])

    def test_click_remembers_previous_choice_across_other_states(self):
        skin = self.make_skin()
        with patch("stock_pet.animated_skin.random.choice", side_effect=lambda choices: choices[0]):
            self.assertTrue(skin.set_animation("click", restart=True))
            previous = skin.action
            for state in ("idle", "refresh", "drag_left", "waiting"):
                with self.subTest(state=state):
                    self.assertTrue(skin.set_animation(state))
                    self.assertTrue(skin.set_animation("click", restart=True))
                    self.assertNotEqual(skin.action, previous)
                    self.assertIn(skin.action, skin.catalog["clicks"])
                    previous = skin.action
            self.assertTrue(skin.set_animation("click", restart=True))
            self.assertNotEqual(skin.action, previous)

    def test_single_click_candidate_can_repeat_and_duplicates_are_ignored(self):
        skin = self.make_skin()
        name = skin.catalog["clicks"][0]
        for candidates in ([name], name, [name, name, None, "unknown"]):
            with self.subTest(candidates=candidates):
                skin.catalog["clicks"] = candidates
                for _ in range(2):
                    self.assertTrue(skin.set_animation("click", restart=True))
                    self.assertEqual(skin.action, name)

    def test_empty_missing_or_invalid_click_pool_uses_default(self):
        skin = self.make_skin()
        for candidates in ([], None, {}, ["unknown", None, 123]):
            with self.subTest(candidates=candidates):
                skin.catalog["clicks"] = candidates
                self.assertTrue(skin.set_animation("click", restart=True))
                self.assertEqual(skin.action, DEFAULT_CLICK_ACTION)
        del skin.catalog["clicks"]
        self.assertTrue(skin.set_animation("click", restart=True))
        self.assertEqual(skin.action, DEFAULT_CLICK_ACTION)
        del skin.catalog["files"][DEFAULT_CLICK_ACTION]
        self.assertTrue(skin.set_animation("click", restart=True))
        self.assertEqual(skin.action, skin.catalog["idle"])

    def test_missing_click_asset_falls_back_and_failed_load_keeps_history(self):
        skin = self.make_skin()
        name = skin.catalog["clicks"][0]
        skin.catalog["clicks"] = [name]
        skin.catalog["files"][name] = "missing-click.gif"
        self.assertTrue(skin.set_animation("click", restart=True))
        self.assertEqual(skin.action, DEFAULT_CLICK_ACTION)
        self.assertEqual(skin._last_click_action, DEFAULT_CLICK_ACTION)
        with patch.object(skin, "_load", return_value=False):
            self.assertFalse(skin.set_animation("click", restart=True))
        self.assertEqual(skin._last_click_action, DEFAULT_CLICK_ACTION)

    def test_hidden_pending_cycle_waits_and_replacement_cancels_old_cycle(self):
        skin = self.make_skin()
        skin.set_active(True)
        results = []
        skin.completed.connect(lambda: results.append(True))
        skin.play_action("涮火锅")
        skin.movie.jumpToFrame(skin.movie.frameCount() - 1)
        skin.movie.jumpToFrame(0)
        skin.set_active(False)
        self.app.processEvents()
        self.assertFalse(results)
        skin.set_active(True)
        self.app.processEvents()
        self.assertEqual(results, [True])
        skin.play_action("吃白饭")
        skin.movie.jumpToFrame(skin.movie.frameCount() - 1)
        skin.movie.jumpToFrame(0)
        skin.set_animation("drag_left")
        self.app.processEvents()
        self.assertEqual(results, [True])
        self.assertEqual(skin.action, skin.catalog["drag"])

    def test_widget_interactions_menu_refresh_priority_and_skin_cleanup(self):
        with tempfile.TemporaryDirectory() as directory, patch("stock_pet.ui.QTimer.singleShot"), patch("stock_pet.ui.QThreadPool.globalInstance"):
            settings = QSettings(os.path.join(directory, "settings.ini"), QSettings.IniFormat)
            settings.setValue("skin", DSH_SKIN_KEY)
            settings.setValue("quote_source", "tencent")
            settings.setValue("favorite_bubble_page_size", 0)
            pet = StockPetWidget(settings)
            try:
                pet.show()
                self.assertIsNotNone(pet._animated_skin)
                self.assertEqual(pet.pet_label.width(), 266)
                self.assertEqual(pet.width(), 282)
                self.assertTrue(pet.bubble.isHidden())
                menu = pet._build_pet_menu()
                menu_actions = menu.actions()
                action_menu = next(a.menu() for a in menu_actions if a.text() == "播放动作")
                category_actions = action_menu.actions()
                interactive = category_actions[0].menu()
                interaction_actions = interactive.actions()
                interaction_actions[-1].trigger()
                self.assertEqual(pet._skin_animation, "action")
                pet._play_named_action("涮火锅")
                pet._set_refresh_activity("page", True)
                self.assertEqual(pet._animated_skin.action, "涮火锅")
                self.end_cycle(pet._animated_skin)
                self.assertEqual(pet._skin_animation, "refresh")
                pet._play_skin_animation("click", restart=True)
                self.assertIn(pet._animated_skin.action, pet._animated_skin.catalog["clicks"])
                pet._set_refresh_activity("page", False)
                self.assertEqual(pet._skin_animation, "click")
                self.end_cycle(pet._animated_skin)
                self.assertEqual(pet._skin_animation, "idle")
                pet._set_random_idle(False)
                self.assertFalse(settings.value("pet_random_idle", True, type=bool))
                pet.hide_all()
                self.assertEqual(pet._animated_skin.movie.state(), QMovie.Paused)
                pet.show()
                self.assertEqual(pet._animated_skin.movie.state(), QMovie.Running)
                old = pet._animated_skin
                pet.set_skin("default")
                self.assertIsNone(pet._animated_skin)
                self.assertEqual(old.movie.state(), QMovie.NotRunning)
                self.assertEqual(pet.pet_label.width(), 156)
                pet.set_skin(DSH_SKIN_KEY)
                self.assertFalse(pet._animated_skin.random_idle)
                menu.deleteLater()
            finally:
                pet._watch_timer.stop()
                pet._favorite_refresh_timer.stop()
                pet._favorite_carousel_timer.stop()
                pet.close_provider()
                pet.panel.close()
                pet.close()


if __name__ == "__main__":
    unittest.main()
