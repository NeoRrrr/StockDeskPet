"""Whole-image pet reactions, with lightweight motion and stable time-based choices."""

from __future__ import annotations

import math
from collections.abc import Callable
from time import monotonic

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPainter, QPixmap

from .resources import asset_path


EXPRESSION_SKIN_KEY = "deepseek-expressions"
EXPRESSION_ASSET_ROOT = "skins/deepseek-expressions"
EXPRESSION_ASSETS = (
    "deepseek-idle", "frame-idle-blink", "reaction-relaxed", "reaction-proud",
    "reaction-deepseek-rice", "reaction-eating-rice", "reaction-cheerful",
    "reaction-hungry", "reaction-pillow", "reaction-sleeping",
    "reaction-desk-coding", "frame-desk-coding-hands-up", "reaction-thinking",
    "frame-thinking-keypress", "reaction-skeptical", "reaction-whip-frightened",
    "reaction-shocked", "reaction-apologetic",
)
ONE_SHOT_ANIMATIONS = {"click", "jump", "failed", "review"}
ONE_SHOT_SECONDS = 1.2


def expression_for(animation: str, age: float, accumulated: float) -> str:
    """Periodic quote updates do not reset these clocks or choose random images."""
    if animation == "idle":
        if age >= 3600:
            return "reaction-sleeping"
        if age >= 1800:
            return "reaction-pillow"
        if age >= 600:
            return "reaction-hungry"
        choices = (
            "deepseek-idle", "reaction-relaxed", "reaction-proud",
            "reaction-deepseek-rice", "reaction-eating-rice", "reaction-cheerful",
        )
        selected = choices[int(accumulated // 30) % len(choices)]
        if selected == "deepseek-idle" and accumulated % 4 >= 3.8:
            return "frame-idle-blink"
        return selected
    if animation == "refresh":
        phase = int(accumulated // 5) % 3
        pressed = int(accumulated / 0.4) % 2
        if phase == 0:
            return "frame-desk-coding-hands-up" if pressed else "reaction-desk-coding"
        if phase == 1:
            return "frame-thinking-keypress" if pressed else "reaction-thinking"
        return "reaction-skeptical"
    return {
        "click": "reaction-cheerful" if age < 0.8 else "reaction-proud",
        "jump": "reaction-cheerful",
        "drag_left": "reaction-whip-frightened",
        "drag_right": "reaction-shocked",
        "waiting": "reaction-relaxed",
        "failed": "reaction-apologetic",
        "review": "reaction-proud",
    }.get(animation, "deepseek-idle")


class ExpressionSkin:
    """Cache decoded images; only paint a small transformed canvas on each tick."""

    def __init__(
        self, pixel_ratio: float = 1.0, *, clock: Callable[[], float] | None = None,
    ) -> None:
        self._clock = clock or monotonic
        self._ratio = max(1.0, pixel_ratio)
        self._images: dict[str, QPixmap] = {}
        for name in EXPRESSION_ASSETS:
            pixmap = QPixmap(str(asset_path(f"{EXPRESSION_ASSET_ROOT}/{name}.webp")))
            if pixmap.isNull():
                raise OSError(f"Invalid pet expression: {name}")
            scaled = pixmap.scaled(
                round(138 * self._ratio), round(138 * self._ratio),
                Qt.KeepAspectRatio, Qt.SmoothTransformation,
            )
            scaled.setDevicePixelRatio(self._ratio)
            self._images[name] = scaled
        self.animation = "idle"
        self._started_at = self._clock()
        self._elapsed: dict[str, float] = {}

    def set_animation(self, animation: str, *, restart: bool = False) -> None:
        if animation == self.animation and not restart:
            return
        now = self._clock()
        self._elapsed[self.animation] = self._elapsed.get(self.animation, 0) + max(
            0.0, now - self._started_at,
        )
        self.animation = animation
        self._started_at = now

    @property
    def finished(self) -> bool:
        return (
            self.animation in ONE_SHOT_ANIMATIONS
            and self._clock() - self._started_at >= ONE_SHOT_SECONDS
        )

    def render(self) -> QPixmap:
        age = max(0.0, self._clock() - self._started_at)
        accumulated = self._elapsed.get(self.animation, 0) + age
        name = expression_for(self.animation, age, accumulated)
        frame = self._images[name]
        canvas = QPixmap(round(150 * self._ratio), round(150 * self._ratio))
        canvas.setDevicePixelRatio(self._ratio)
        canvas.fill(Qt.transparent)
        phase = age * math.tau / 3
        tilt = 0.0
        bounce = 0.0
        if self.animation.startswith("drag_"):
            tilt = (-5 if self.animation == "drag_left" else 5) + math.sin(phase * 2)
        elif self.animation in {"click", "jump"}:
            bounce = -4 * abs(math.sin(age * math.pi / ONE_SHOT_SECONDS))
            tilt = 2 * math.sin(age * math.tau)
        scale = 1 + 0.012 * math.sin(phase)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.translate(75, 75 + bounce)
        painter.rotate(tilt)
        painter.scale(scale, scale)
        size = frame.deviceIndependentSize()
        painter.drawPixmap(QPointF(-size.width() / 2, -size.height() / 2), frame)
        painter.end()
        return canvas
