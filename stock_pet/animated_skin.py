"""On-demand GIF playback: one decoder, no full-library decoded-frame cache."""
from __future__ import annotations

import json
import random
from pathlib import Path

from PySide6.QtCore import QObject, QSize, QTimer, Signal
from PySide6.QtGui import QMovie, QPixmap

from .resources import asset_path


DSH_SKIN_KEY = "dsh-maid"
DSH_ASSET_ROOT = "skins/dsh-maid"
DSH_CANVAS_WIDTH = 266
DSH_CANVAS_HEIGHT = 150
DEFAULT_CLICK_ACTION = "点击回应-元气挥手"
ONE_SHOT_STATES = {"click", "action", "jump", "failed", "review"}


class AnimatedSkin(QObject):
    frame_ready = Signal(QPixmap)
    completed = Signal()
    failed = Signal(str)

    def __init__(self, pixel_ratio: float = 1.0, *, random_idle: bool = True, parent=None):
        super().__init__(parent)
        self.catalog = json.loads(asset_path(f"{DSH_ASSET_ROOT}/manifest.json").read_text(encoding="utf-8"))
        self.pixel_ratio = max(1.0, pixel_ratio)
        self.random_idle = random_idle
        self.animation = "idle"
        self.action = ""
        self._active = False
        self._closed = False
        self._previous_frame = -1
        self._idle_cycles = 0
        self._last_random_action = ""
        self._last_click_action = ""
        self._cycle_pending = False
        self._movie: QMovie | None = None
        self._pixmap = QPixmap()
        # Use a Python callback: some PySide builds cannot marshal this enum
        # when connecting it directly to a dynamically generated QObject slot.
        self._error_callback = lambda _error: self._on_error()
        self._cycle_timer = QTimer(self)
        self._cycle_timer.setSingleShot(True)
        self._cycle_timer.timeout.connect(self._finish_cycle)
        if not self._load(self.catalog["idle"]):
            raise OSError("DSH 待机动画缺失或无法解码")

    @property
    def groups(self) -> list[dict]:
        return self.catalog["groups"]

    @property
    def movie(self) -> QMovie | None:
        return self._movie

    def render(self) -> QPixmap:
        return self._pixmap

    def _load(self, action: str) -> bool:
        filename = self.catalog["files"].get(action)
        if not filename or Path(filename).name != filename:
            return False
        try:
            path = asset_path(f"{DSH_ASSET_ROOT}/{filename}")
        except OSError:
            return False
        movie = QMovie(str(path), parent=self)
        movie.setCacheMode(QMovie.CacheNone)
        movie.setScaledSize(QSize(round(DSH_CANVAS_WIDTH * self.pixel_ratio),
                                  round(DSH_CANVAS_HEIGHT * self.pixel_ratio)))
        if not movie.isValid() or not movie.jumpToFrame(0):
            movie.deleteLater()
            return False
        self._cycle_timer.stop()
        self._cycle_pending = False
        if self._movie is not None:
            self._movie.stop()
            self._movie.frameChanged.disconnect(self._on_frame)
            self._movie.error.disconnect(self._error_callback)
            self._movie.deleteLater()
        self._movie = movie
        self.action = action
        self._previous_frame = -1
        movie.frameChanged.connect(self._on_frame)
        movie.error.connect(self._error_callback)
        self._on_frame(0)
        if self._active:
            movie.start()
        return True

    def _on_frame(self, number: int) -> None:
        if self._closed or self._movie is None:
            return
        pixmap = self._movie.currentPixmap()
        if not pixmap.isNull():
            pixmap.setDevicePixelRatio(self.pixel_ratio)
            self._pixmap = pixmap
            self.frame_ready.emit(pixmap)
        if number == 0 and self._previous_frame > 0:
            # Defer replacement until QMovie has returned from its frame callback.
            self._cycle_pending = True
            self._cycle_timer.start(0)
        self._previous_frame = number

    def _on_error(self) -> None:
        if not self._closed:
            self.set_active(False)
            self.failed.emit("DSH 动画解码失败，已恢复默认皮肤")

    def _finish_cycle(self) -> None:
        if self._closed or not self._active or not self._cycle_pending:
            return
        self._cycle_pending = False
        if self.animation in ONE_SHOT_STATES:
            self.completed.emit()
        elif self.animation == "idle":
            idle = self.catalog["idle"]
            if self.action != idle:
                self._idle_cycles = 0
                self._load(idle)
            else:
                self._idle_cycles += 1
                if self.random_idle and self._idle_cycles >= 2:
                    choices = [name for name in self.catalog["idle_pool"] if name != self._last_random_action]
                    self._last_random_action = random.choice(choices)
                    self._idle_cycles = 0
                    self._load(self._last_random_action)

    def _pick_click_action(self) -> str:
        candidates = self.catalog.get("clicks", [])
        if isinstance(candidates, str):
            candidates = [candidates]
        if not isinstance(candidates, (list, tuple)):
            candidates = []
        candidates = list(dict.fromkeys(
            name for name in candidates
            if isinstance(name, str) and name in self.catalog["files"]
        ))
        if not candidates:
            return self._default_click_action()
        # Idle/refresh changes self.action, so retain the last successful click
        # separately. A singleton must remain usable, even on repeated clicks.
        choices = [name for name in candidates if name != self._last_click_action] or candidates
        choices = [name for name in choices if name != self.action] or choices
        return random.choice(choices)

    def _default_click_action(self) -> str:
        if DEFAULT_CLICK_ACTION in self.catalog["files"]:
            return DEFAULT_CLICK_ACTION
        return self.catalog["idle"]

    def set_animation(self, animation: str, *, restart: bool = False) -> bool:
        if self._closed:
            return False
        if animation == self.animation and not restart:
            return True
        if animation in {"drag_left", "drag_right"}:
            action = self.catalog["drag"]
        elif animation == "click":
            action = self._pick_click_action()
        else:
            action = {
                "refresh": "工作状态-忙碌点按", "waiting": "工作状态-原地踱步张望",
                "jump": "点击回应-开心跃动", "review": "工作状态-雀跃庆祝",
                "failed": "工作状态-垂头叹气冒汗",
            }.get(animation, self.catalog["idle"])
        if not self._load(action):
            fallback = self._default_click_action()
            if animation != "click" or action == fallback or not self._load(fallback):
                return False
        if animation == "click":
            self._last_click_action = self.action
        self.animation = animation
        self._idle_cycles = 0
        return True

    def play_action(self, action: str) -> bool:
        if self._closed or not self._load(action):
            return False
        self.animation = "action"
        return True

    def set_random_idle(self, enabled: bool) -> None:
        self.random_idle = enabled
        if not enabled and self.animation == "idle":
            self.set_animation("idle", restart=True)

    def set_active(self, active: bool) -> None:
        self._active = active and not self._closed
        if self._movie is None:
            return
        if self._active:
            if self._movie.state() == QMovie.Paused:
                self._movie.setPaused(False)
            elif self._movie.state() == QMovie.NotRunning:
                self._movie.start()
            if self._cycle_pending:
                self._cycle_timer.start(0)
        else:
            self._cycle_timer.stop()
            if self._movie.state() == QMovie.Running:
                self._movie.setPaused(True)

    def close(self) -> None:
        self.set_active(False)
        self._closed = True
        if self._movie is not None:
            self._movie.stop()
