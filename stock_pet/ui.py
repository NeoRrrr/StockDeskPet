from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta, timezone
from html import escape

from PySide6.QtCore import QObject, QPoint, QPointF, QRunnable, QSettings, QSize, QStringListModel, QThreadPool, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QAction, QBrush, QColor, QCursor, QDesktopServices, QFontDatabase, QMouseEvent, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QCompleter,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QSlider,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .hybrid_quote_provider import HybridQuoteProvider
from .models import Quote, StockSearchResult
from .quote_provider import TencentQuoteProvider
from .resources import asset_path
from .symbols import SymbolError, normalize_symbol, normalize_watchlist, partition_watchlist
from .update_checker import (
    PROJECT_URL,
    AutomaticUpdateResult,
    check_download_and_install,
)


SPRITE_CELL_WIDTH = 192
SPRITE_CELL_HEIGHT = 208
SPRITE_FRAME_TICKS = 4
SPRITE_ANIMATIONS: dict[str, tuple[int, int, bool]] = {
    "idle": (0, 6, True),
    "drag_right": (1, 8, True),
    "drag_left": (2, 8, True),
    "click": (3, 4, False),
    "jump": (4, 5, False),
    "failed": (5, 8, False),
    "waiting": (6, 6, True),
    "refresh": (7, 6, True),
    "review": (8, 6, False),
}
SKINS: dict[str, tuple[str, str, bool]] = {
    "default": ("默认牛", "ox_3d.png", False),
    "ella-wave": ("Ella Wave", "skins/ella-wave.webp", True),
    "endminguga": ("GUGUGAGA", "skins/endminguga.webp", True),
    "ikunchick": ("ikunchick", "skins/ikunchick.webp", True),
    "maid-deepseek-whale": ("DeepSeek 鲸鱼女仆", "skins/maid-deepseek-whale.webp", True),
    "deepseek": ("DeepSeek 鲸鱼", "skins/deepseek.webp", True),
}
DEFAULT_HK_WATCHLIST = ("00700", "01810")
DEFAULT_A_SHARE_ETFS = ("159516", "515880", "512200", "512800")
DEFAULT_WATCHLIST = (*DEFAULT_A_SHARE_ETFS, *DEFAULT_HK_WATCHLIST)
INDEX_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("港股指数", ("HSI", "HSTECH", "HSCEI")),
    ("大A指数", ("SHCOMP", "CSI300", "SZCOMP", "CHINEXT")),
    ("黄金", ("GOLD",)),
)
INDEX_SYMBOLS = tuple(symbol for _group, symbols in INDEX_GROUPS for symbol in symbols)
TAB_MARKET_SUMMARIES: dict[int, tuple[tuple[str, str], ...]] = {
    0: (("沪指", "SHCOMP"), ("深指", "SZCOMP"), ("创业板", "CHINEXT")),
    1: (("恒生", "HSI"), ("恒科", "HSTECH")),
}
FAVORITE_REFRESH_INTERVAL_MS = 5_000
OPEN_TAB_REFRESH_INTERVAL_MS = 10_000
FAVORITE_BUBBLE_PAGE_INTERVAL_MS = 2_000
FAVORITE_BUBBLE_PAGE_SIZE = 5
ALERT_REARM_MARGIN_PERCENT = 0.2
ALERT_CACHE_SETTINGS_KEY = "alert_cache_v1"
MARKET_CLOSE_REFRESH_GRACE_MINUTES = 10
MARKET_TIMEZONE = timezone(timedelta(hours=8))
A_SHARE_MARKETS = {"sh", "sz", "bj", "cn_index"}
HK_MARKETS = {"hk", "hk_index"}
A_SHARE_SESSIONS = ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60))
HK_SESSIONS = ((9 * 60 + 30, 12 * 60), (13 * 60, 16 * 60))

_UI_FONTS_LOADED = False


def _is_open_for_automatic_refresh(
    raw_symbol: str,
    at: datetime | None = None,
) -> bool:
    """Return whether a symbol should be polled automatically in China time."""
    try:
        market = normalize_symbol(raw_symbol).market
    except SymbolError:
        return False

    current = at or datetime.now(MARKET_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MARKET_TIMEZONE)
    else:
        current = current.astimezone(MARKET_TIMEZONE)

    if current.weekday() >= 5:
        return market not in A_SHARE_MARKETS | HK_MARKETS | {"gold"}
    if market == "gold":
        return True

    minute = current.hour * 60 + current.minute
    sessions = (
        A_SHARE_SESSIONS
        if market in A_SHARE_MARKETS
        else HK_SESSIONS if market in HK_MARKETS else ()
    )
    if not sessions:
        return True
    return any(
        start <= minute < (
            end + MARKET_CLOSE_REFRESH_GRACE_MINUTES
            if index == len(sessions) - 1
            else end
        )
        for index, (start, end) in enumerate(sessions)
    )


def _automatic_refresh_symbols(
    symbols: list[str] | tuple[str, ...],
    at: datetime | None = None,
) -> list[str]:
    return list(
        dict.fromkeys(
            str(symbol)
            for symbol in symbols
            if _is_open_for_automatic_refresh(str(symbol), at)
        )
    )


def _is_visible_in_idle_bubble(
    raw_symbol: str,
    at: datetime | None = None,
) -> bool:
    """Show cached favorites only during their market's trading-day window."""
    try:
        market = normalize_symbol(raw_symbol).market
    except SymbolError:
        return False

    current = at or datetime.now(MARKET_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MARKET_TIMEZONE)
    else:
        current = current.astimezone(MARKET_TIMEZONE)

    if current.weekday() >= 5:
        return market not in A_SHARE_MARKETS | HK_MARKETS | {"gold"}
    if market == "gold":
        return True

    minute = current.hour * 60 + current.minute
    if market in A_SHARE_MARKETS:
        return A_SHARE_SESSIONS[0][0] <= minute < A_SHARE_SESSIONS[-1][1]
    if market in HK_MARKETS:
        return HK_SESSIONS[0][0] <= minute < HK_SESSIONS[-1][1]
    return True


def _load_ui_fonts() -> None:
    """Load the two editorial UI families explicitly for Qt and frozen builds."""
    global _UI_FONTS_LOADED
    if _UI_FONTS_LOADED:
        return
    fonts_dir = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for filename in ("NotoSansSC-VF.ttf", "NotoSerifSC-VF.ttf", "msyh.ttc"):
        font_path = os.path.join(fonts_dir, filename)
        if os.path.exists(font_path):
            QFontDatabase.addApplicationFont(font_path)
    _UI_FONTS_LOADED = True


def _setting_bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _settings_list(settings: QSettings, key: str) -> list[str]:
    value = settings.value(key, [])
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _normalize_favorites(values: list[str]) -> list[str]:
    """Normalize and de-duplicate stocks, indices and gold saved for the bubble."""
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        try:
            symbol = normalize_symbol(value)
        except SymbolError:
            continue
        if symbol.provider_symbol in seen:
            continue
        seen.add(symbol.provider_symbol)
        normalized.append(symbol.code)
    return normalized


class FetchSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)


class FetchTask(QRunnable):
    def __init__(self, provider: TencentQuoteProvider, symbol: str) -> None:
        super().__init__()
        self.provider = provider
        self.symbol = symbol
        self.signals = FetchSignals()

    @Slot()
    def run(self) -> None:
        try:
            interactive_fetch = getattr(self.provider, "fetch_interactive", None)
            quote = (
                interactive_fetch(self.symbol)
                if callable(interactive_fetch)
                else self.provider.fetch(self.symbol)
            )
        except Exception as exc:
            self.signals.failed.emit(str(exc))
            return
        self.signals.finished.emit(quote)


class WatchlistTask(QRunnable):
    def __init__(self, provider: TencentQuoteProvider, symbols: list[str]) -> None:
        super().__init__()
        self.provider = provider
        self.symbols = symbols
        self.signals = FetchSignals()

    @Slot()
    def run(self) -> None:
        quotes: list[Quote] = []
        errors: list[str] = []
        for symbol in self.symbols:
            try:
                quotes.append(self.provider.fetch(symbol))
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
        self.signals.finished.emit((quotes, errors))


class SearchTask(QRunnable):
    def __init__(self, provider: TencentQuoteProvider, query: str) -> None:
        super().__init__()
        self.provider = provider
        self.query = query
        self.signals = FetchSignals()

    @Slot()
    def run(self) -> None:
        try:
            results = self.provider.search(self.query)
            self.signals.finished.emit((self.query, results, ""))
        except Exception as exc:
            self.signals.finished.emit((self.query, [], str(exc)))


class ProviderStatusTask(QRunnable):
    def __init__(self, provider: HybridQuoteProvider) -> None:
        super().__init__()
        self.provider = provider
        self.signals = FetchSignals()

    @Slot()
    def run(self) -> None:
        try:
            self.signals.finished.emit(self.provider.check_futu_status())
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class FutuWatchlistTask(QRunnable):
    def __init__(
        self,
        provider: HybridQuoteProvider,
        action: str,
        group_name: str = "",
    ) -> None:
        super().__init__()
        self.provider = provider
        self.action = action
        self.group_name = group_name
        self.signals = FetchSignals()

    @Slot()
    def run(self) -> None:
        try:
            if self.action == "groups":
                data = self.provider.list_futu_watchlist_groups()
            else:
                data = self.provider.get_futu_watchlist_group(self.group_name)
            self.signals.finished.emit((self.action, self.group_name, data, ""))
        except Exception as exc:
            self.signals.finished.emit((self.action, self.group_name, [], str(exc)))


class UpdateTaskSignals(QObject):
    finished = Signal(object)
    failed = Signal(str)
    phase = Signal(str)
    progress = Signal(int)


class UpdateCheckTask(QRunnable):
    def __init__(self, current_version: str) -> None:
        super().__init__()
        self.current_version = current_version
        self.signals = UpdateTaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = check_download_and_install(
                self.current_version,
                phase_callback=self.signals.phase.emit,
                progress_callback=self.signals.progress.emit,
            )
            self.signals.finished.emit(result)
        except Exception as exc:
            self.signals.failed.emit(str(exc))


class QuoteItemDelegate(QStyledItemDelegate):
    """Keep semantic up/down colors visible when a quote row is selected."""

    def initStyleOption(self, option: QStyleOptionViewItem, index) -> None:  # type: ignore[override]
        super().initStyleOption(option, index)
        foreground = index.data(Qt.ItemDataRole.ForegroundRole)
        if isinstance(foreground, QBrush):
            foreground = foreground.color()
        if isinstance(foreground, QColor):
            option.palette.setColor(QPalette.ColorRole.Text, foreground)
            option.palette.setColor(QPalette.ColorRole.HighlightedText, foreground)


class FavoriteButton(QAbstractButton):
    """A consistent vector star that does not depend on platform emoji fonts."""

    def __init__(
        self,
        favorite: bool,
        theme: str,
        *,
        compact: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.theme = theme
        self._hovered = False
        self.setObjectName("favoriteButton")
        self.setCheckable(True)
        self.setChecked(favorite)
        self.setFixedSize(20 if compact else 24, 18 if compact else 24)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        is_light = self.theme == "beige"

        if self._hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#e7effa" if is_light else "#203249"))
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 6, 6)

        checked = self.isChecked()
        if checked:
            star_color = QColor("#d69218" if is_light else "#f0b83f")
        elif self._hovered:
            star_color = QColor("#2d6ed8" if is_light else "#dbe8f8")
        else:
            star_color = QColor("#8191a7" if is_light else "#8193aa")

        center = QPointF(self.width() / 2.0, self.height() / 2.0 - 0.6)
        target_radius = 6.1 if self.width() >= 24 else 5.3
        outer_radius = min(target_radius, max(4.2, (self.height() - 4.0) / 2.0))
        inner_radius = outer_radius * 0.45
        star = QPainterPath()
        for index in range(10):
            radius = outer_radius if index % 2 == 0 else inner_radius
            angle = -math.pi / 2.0 + index * math.pi / 5.0
            point = QPointF(
                center.x() + math.cos(angle) * radius,
                center.y() + math.sin(angle) * radius,
            )
            if index == 0:
                star.moveTo(point)
            else:
                star.lineTo(point)
        star.closeSubpath()

        painter.setPen(QPen(star_color, 1.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(star_color if checked else Qt.BrushStyle.NoBrush)
        painter.drawPath(star)


class QuoteRowWidget(QWidget):
    """Quote text plus a compact favorite button for a single list item."""

    quote_requested = Signal(str)
    favorite_toggled = Signal(str)

    def __init__(
        self,
        text: str,
        symbol: str,
        color: str,
        favorite: bool,
        *,
        compact: bool = False,
        theme: str = "dark",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.symbol = symbol
        self.setObjectName("quoteRow")
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 7, 0)
        layout.setSpacing(4)
        self.text_label = QLabel(text)
        self.text_label.setObjectName("quoteRowText")
        self.text_label.setStyleSheet(f"color: {color};")
        self.text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(self.text_label, 1)

        self.favorite_button = FavoriteButton(favorite, theme, compact=compact)
        self.favorite_button.setToolTip("取消收藏" if favorite else "收藏到桌宠气泡")
        self.favorite_button.setAccessibleName(self.favorite_button.toolTip())
        self.favorite_button.clicked.connect(self._toggle_favorite)
        layout.addWidget(self.favorite_button)

    @Slot(bool)
    def _toggle_favorite(self, favorite: bool) -> None:
        self.favorite_button.setToolTip("取消收藏" if favorite else "收藏到桌宠气泡")
        self.favorite_button.setAccessibleName(self.favorite_button.toolTip())
        self.favorite_toggled.emit(self.symbol)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.quote_requested.emit(self.symbol)
            event.accept()
            return
        super().mousePressEvent(event)


class EqualWidthTabWidget(QTabWidget):
    """Keep the three market tabs aligned to the full list width."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tabBar().setExpanding(True)
        self.tabBar().setUsesScrollButtons(False)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.tabBar().setFixedWidth(self.width())


class ThemeSwitch(QAbstractButton):
    """Two-position switch with explicit sun and moon mode cues."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setFixedSize(86, 28)
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setAccessibleName("切换深浅主题")
        self.setAccessibleDescription("左侧太阳代表浅色主题，右侧月亮代表深色主题")
        self._sun_icon = QSvgRenderer(str(asset_path("icons/weather_sunny_20_regular.svg")))
        self._moon_icon = QSvgRenderer(str(asset_path("icons/weather_moon_20_regular.svg")))

    @staticmethod
    def _tinted_icon(icon: QSvgRenderer, color: QColor) -> QPixmap:
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        icon.render(painter, pixmap.rect())
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pixmap.rect(), color)
        painter.end()
        return pixmap

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        is_light = self.isChecked()
        track = self.rect().adjusted(1, 1, -1, -1)
        track_color = QColor("#f0f3f8" if is_light else "#242b3c")
        border_color = QColor("#c6d0de" if is_light else "#3b4559")
        painter.setPen(QPen(border_color, 1))
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, 13, 13)

        sun_color = QColor("#d89a1d" if is_light else "#6f7b8e")
        moon_color = QColor("#7c8799" if is_light else "#9db9ef")
        painter.drawPixmap(7, 6, self._tinted_icon(self._sun_icon, sun_color))
        painter.drawPixmap(63, 6, self._tinted_icon(self._moon_icon, moon_color))

        switch_track = self.rect().adjusted(27, 5, -27, -5)
        switch_color = QColor("#f2ba3f" if is_light else "#2b6bd8")
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(switch_color)
        painter.drawRoundedRect(switch_track, 9, 9)
        painter.setBrush(QColor("#ffffff"))
        knob_size = 14
        knob_x = 29 if is_light else 43
        painter.drawEllipse(knob_x, 7, knob_size, knob_size)


class FutuWatchlistImportDialog(QDialog):
    MAX_WATCHLIST_ITEMS = 20

    def __init__(
        self,
        provider: HybridQuoteProvider,
        existing_watchlist: list[str],
        theme: str = "dark",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        _load_ui_fonts()
        self.provider = provider
        self.existing_watchlist = normalize_watchlist(existing_watchlist)
        self.existing_keys = {
            normalize_symbol(code).provider_symbol for code in self.existing_watchlist
        }
        self.available_slots = max(0, self.MAX_WATCHLIST_ITEMS - len(self.existing_watchlist))
        self.selected_codes: list[str] = []
        self._task: FutuWatchlistTask | None = None
        self._updating_items = False

        self.setWindowTitle("从富途导入自选")
        self.setModal(True)
        self.setFixedSize(470, 520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel("从富途导入自选")
        title.setObjectName("importTitle")
        help_label = QLabel(
            "只读取同一 OpenD 账号的自选，仅导入大A和港股，"
            "不会修改富途手机端。"
        )
        help_label.setObjectName("importHelp")
        help_label.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(help_label)

        group_row = QHBoxLayout()
        group_row.addWidget(QLabel("自选分组"))
        self.group_combo = QComboBox()
        self.group_combo.setEnabled(False)
        self.group_combo.currentTextChanged.connect(self._load_selected_group)
        group_row.addWidget(self.group_combo, 1)
        layout.addLayout(group_row)

        self.stock_list = QListWidget()
        self.stock_list.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.stock_list, 1)

        self.status_label = QLabel(
            f"当前还可导入 {self.available_slots} 只，正在读取富途自选分组…"
        )
        self.status_label.setObjectName("importStatus")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.import_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.import_button.setText("导入选中")
        self.import_button.setObjectName("importButton")
        self.import_button.setEnabled(False)
        self.one_click_button = buttons.addButton(
            "一键导入本组",
            QDialogButtonBox.ButtonRole.ActionRole,
        )
        self.one_click_button.setObjectName("oneClickImportButton")
        self.one_click_button.setEnabled(False)
        self.one_click_button.clicked.connect(self._import_all_available)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._accept_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        stylesheet = """
            QDialog { background:#0c1622; color:#c8d2df; }
            QLabel { color:#c8d2df; font-family:"Noto Sans SC"; }
            QLabel#importTitle { color:#eef4fb; font:600 18px "Microsoft YaHei UI"; }
            QLabel#importHelp, QLabel#importStatus { color:#8797aa; font-size:12px; }
            QComboBox, QListWidget {
                color:#e6edf6; background:#111e2c; border:1px solid #324257;
                border-radius:8px; padding:6px; font:12px "Noto Sans SC";
            }
            QListWidget::item { min-height:28px; padding:3px 5px; }
            QListWidget::item:selected { background:#1d3f70; }
            QPushButton { color:#b8c5d6; background:#111e2c; border:1px solid #324257;
                border-radius:8px; padding:7px 16px; font:600 12px "Noto Sans SC"; }
            QPushButton:hover { color:#eef5ff; border-color:#4b82de; }
            QPushButton#importButton { color:white; background:#2467d8; border-color:#3b7ce8; }
            QPushButton:disabled { color:#6f7e91; background:#172331; border-color:#29394c; }
        """
        if theme == "beige":
            stylesheet += """
                QDialog { background:#f6f8fb; color:#243247; }
                QLabel { color:#243247; }
                QLabel#importTitle { color:#17253a; }
                QLabel#importHelp, QLabel#importStatus { color:#68778c; }
                QComboBox, QListWidget { color:#1f2d42; background:#ffffff; border-color:#c6d2e1; }
                QListWidget::item:selected { color:#ffffff; background:#2d6ed8; }
                QPushButton { color:#40516a; background:#f0f4f9; border-color:#c6d2e1; }
                QPushButton#importButton { color:white; background:#2d6ed8; border-color:#2d6ed8; }
            """
        self.setStyleSheet(stylesheet)
        self._start_task("groups")

    def _start_task(self, action: str, group_name: str = "") -> None:
        if self._task is not None:
            return
        self.group_combo.setEnabled(False)
        self.import_button.setEnabled(False)
        self.one_click_button.setEnabled(False)
        if action == "entries":
            self.stock_list.clear()
            self.status_label.setText(f"正在读取“{group_name}”…")
        task = FutuWatchlistTask(self.provider, action, group_name)
        task.signals.finished.connect(self._on_task_result)
        self._task = task
        QThreadPool.globalInstance().start(task)

    @Slot(object)
    def _on_task_result(self, payload: tuple[str, str, list, str]) -> None:
        action, group_name, data, error = payload
        self._task = None
        if error:
            self.status_label.setText(error)
            self.group_combo.setEnabled(self.group_combo.count() > 0)
            return
        if action == "groups":
            groups = [str(group) for group in data if str(group).strip()]
            if not groups:
                self.status_label.setText("富途账号中没有可读取的自选分组。")
                return
            self.group_combo.blockSignals(True)
            self.group_combo.clear()
            self.group_combo.addItems(groups)
            preferred_index = self.group_combo.findText("全部")
            self.group_combo.setCurrentIndex(max(0, preferred_index))
            self.group_combo.blockSignals(False)
            self.group_combo.setEnabled(True)
            self._load_selected_group(self.group_combo.currentText())
            return
        if group_name != self.group_combo.currentText():
            return
        self.group_combo.setEnabled(True)
        self._populate_entries(data)

    @Slot(str)
    def _load_selected_group(self, group_name: str) -> None:
        if group_name and self._task is None:
            self._start_task("entries", group_name)

    def _populate_entries(self, entries: list[tuple[str, str]]) -> None:
        self._updating_items = True
        self.stock_list.clear()
        preselected = 0
        for code, name in entries:
            try:
                symbol = normalize_symbol(code)
            except SymbolError:
                continue
            existing = symbol.provider_symbol in self.existing_keys
            suffix = "  ·  已在桌宠自选" if existing else ""
            item = QListWidgetItem(f"{name}  ·  {symbol.display_code}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, symbol.code)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            if existing:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            elif preselected < self.available_slots:
                item.setCheckState(Qt.CheckState.Checked)
                preselected += 1
            self.stock_list.addItem(item)
        self._updating_items = False
        if self.stock_list.count() == 0:
            self.status_label.setText("该分组没有桌宠支持的大A或港股。")
            self.import_button.setEnabled(False)
            self.one_click_button.setEnabled(False)
        else:
            self._update_selection_status()

    @Slot(QListWidgetItem)
    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._updating_items:
            return
        if item.checkState() == Qt.CheckState.Checked and len(self._checked_codes()) > self.available_slots:
            self._updating_items = True
            item.setCheckState(Qt.CheckState.Unchecked)
            self._updating_items = False
            self.status_label.setText(
                f"桌宠自选最多 {self.MAX_WATCHLIST_ITEMS} 只，"
                f"当前最多还可选 {self.available_slots} 只。"
            )
            self._update_import_button()
            return
        self._update_selection_status()

    def _checked_codes(self) -> list[str]:
        return [
            str(item.data(Qt.ItemDataRole.UserRole))
            for index in range(self.stock_list.count())
            if (item := self.stock_list.item(index)).checkState() == Qt.CheckState.Checked
        ]

    def _update_import_button(self) -> None:
        self.import_button.setEnabled(bool(self._checked_codes()))
        candidates = self._available_codes()
        import_count = min(len(candidates), self.available_slots)
        self.one_click_button.setEnabled(import_count > 0)
        if import_count:
            suffix = f"前 {import_count} 只" if len(candidates) > import_count else f"{import_count} 只"
            self.one_click_button.setText(f"一键导入{suffix}")
            self.one_click_button.setToolTip(
                f"按当前列表顺序导入 {import_count} 只，已有自选会自动跳过"
            )

    def _available_codes(self) -> list[str]:
        return [
            str(item.data(Qt.ItemDataRole.UserRole))
            for index in range(self.stock_list.count())
            if (item := self.stock_list.item(index)).flags() & Qt.ItemFlag.ItemIsEnabled
        ]

    def _update_selection_status(self) -> None:
        selected_count = len(self._checked_codes())
        self.status_label.setText(
            f"该分组共 {self.stock_list.count()} 只可导入股票，"
            f"已选 {selected_count} 只，剩余名额 {self.available_slots - selected_count} 只。"
        )
        self._update_import_button()

    @Slot()
    def _accept_selected(self) -> None:
        self.selected_codes = self._checked_codes()
        if self.selected_codes:
            self.accept()

    @Slot()
    def _import_all_available(self) -> None:
        self.selected_codes = self._available_codes()[: self.available_slots]
        if self.selected_codes:
            self.accept()


class WatchlistDialog(QDialog):
    INTERVAL_OPTIONS = (30, 60, 120, 180, 300)

    def __init__(
        self,
        watchlist: list[str],
        threshold: float,
        interval_seconds: int,
        alerts_enabled: bool,
        provider: HybridQuoteProvider | None = None,
        theme: str = "dark",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        _load_ui_fonts()
        self.provider = provider
        self.theme = theme
        self.saved_config: tuple[list[str], float, int, bool] | None = None
        self.setWindowTitle("自选与提醒设置")
        self.setModal(True)
        self.setMinimumSize(430, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = QLabel("自选与提醒设置")
        title.setObjectName("dialogTitle")
        help_label = QLabel(
            "滑动查看并勾选要保留的股票，最多 20 只。"
            "支持港股、沪市、深市和北交所。"
        )
        help_label.setWordWrap(True)
        help_label.setObjectName("dialogHelp")
        layout.addWidget(title)
        layout.addWidget(help_label)

        self.stock_list = QListWidget()
        self.stock_list.setObjectName("watchlistEditor")
        self.stock_list.setMinimumHeight(180)
        self.stock_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        for code in normalize_watchlist(watchlist):
            self._append_watchlist_item(code)
        layout.addWidget(self.stock_list)

        edit_row = QHBoxLayout()
        self.stock_add_input = QLineEdit()
        self.stock_add_input.setObjectName("watchlistAddInput")
        self.stock_add_input.setPlaceholderText("输入股票代码，如 00700 / 600519")
        self.stock_add_input.returnPressed.connect(self._add_stock_item)
        self.add_stock_button = QPushButton("+")
        self.add_stock_button.setObjectName("watchlistAddButton")
        self.add_stock_button.setFixedWidth(42)
        self.add_stock_button.setToolTip("添加股票")
        self.add_stock_button.clicked.connect(self._add_stock_item)
        self.remove_stock_button = QPushButton("−")
        self.remove_stock_button.setObjectName("watchlistRemoveButton")
        self.remove_stock_button.setFixedWidth(42)
        self.remove_stock_button.setToolTip("删除当前选中的行")
        self.remove_stock_button.clicked.connect(self._remove_selected_stock_items)
        self.watchlist_count_label = QLabel("")
        self.watchlist_count_label.setObjectName("dialogHelp")
        edit_row.addWidget(self.stock_add_input, 1)
        edit_row.addWidget(self.add_stock_button)
        edit_row.addWidget(self.remove_stock_button)
        edit_row.addWidget(self.watchlist_count_label)
        layout.addLayout(edit_row)
        self.stock_list.itemChanged.connect(self._update_watchlist_count)
        self._update_watchlist_count()

        import_row = QHBoxLayout()
        self.futu_import_button = QPushButton("从富途导入")
        self.futu_import_button.setObjectName("futuImportButton")
        self.futu_import_button.clicked.connect(self._import_futu_watchlist)
        self.futu_import_button.setVisible(provider is not None)
        self.import_status_label = QLabel("")
        self.import_status_label.setObjectName("dialogHelp")
        import_row.addWidget(self.futu_import_button)
        import_row.addWidget(self.import_status_label, 1)
        layout.addLayout(import_row)

        form = QFormLayout()
        form.setVerticalSpacing(14)
        self.alerts_enabled = QCheckBox("启用系统通知")
        self.alerts_enabled.setChecked(alerts_enabled)
        self.threshold = QSlider(Qt.Orientation.Horizontal)
        self.threshold.setObjectName("settingsSlider")
        self.threshold.setRange(5, 200)
        self.threshold.setSingleStep(5)
        self.threshold.setPageStep(10)
        self.threshold.setValue(round(threshold * 10))
        self.threshold.setToolTip("涨跌幅提醒阈值：0.5%–20%")
        self.threshold_value = QLabel("")
        self.threshold_value.setObjectName("sliderValue")
        self.threshold_value.setFixedWidth(58)
        self.threshold_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.threshold.valueChanged.connect(
            lambda value: self.threshold_value.setText(f"{value / 10:.1f}%")
        )
        self.threshold_value.setText(f"{self.threshold.value() / 10:.1f}%")

        self.interval = QSlider(Qt.Orientation.Horizontal)
        self.interval.setObjectName("settingsSlider")
        self.interval.setRange(0, len(self.INTERVAL_OPTIONS) - 1)
        interval_index = min(
            range(len(self.INTERVAL_OPTIONS)),
            key=lambda index: abs(self.INTERVAL_OPTIONS[index] - interval_seconds),
        )
        self.interval.setValue(interval_index)
        self.interval.setToolTip("系统通知检查间隔：30 秒–5 分钟")
        self.interval_value = QLabel("")
        self.interval_value.setObjectName("sliderValue")
        self.interval_value.setFixedWidth(58)
        self.interval_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.interval.valueChanged.connect(self._update_interval_value)
        self._update_interval_value(self.interval.value())

        threshold_control = QWidget()
        threshold_layout = QHBoxLayout(threshold_control)
        threshold_layout.setContentsMargins(0, 0, 0, 0)
        threshold_layout.setSpacing(10)
        threshold_layout.addWidget(self.threshold, 1)
        threshold_layout.addWidget(self.threshold_value)
        interval_control = QWidget()
        interval_layout = QHBoxLayout(interval_control)
        interval_layout.setContentsMargins(0, 0, 0, 0)
        interval_layout.setSpacing(10)
        interval_layout.addWidget(self.interval, 1)
        interval_layout.addWidget(self.interval_value)

        self.alerts_enabled.toggled.connect(self.threshold.setEnabled)
        self.alerts_enabled.toggled.connect(self.interval.setEnabled)
        self.alerts_enabled.toggled.connect(self.threshold_value.setEnabled)
        self.alerts_enabled.toggled.connect(self.interval_value.setEnabled)
        self.threshold.setEnabled(alerts_enabled)
        self.interval.setEnabled(alerts_enabled)
        self.threshold_value.setEnabled(alerts_enabled)
        self.interval_value.setEnabled(alerts_enabled)
        form.addRow("提醒", self.alerts_enabled)
        form.addRow("涨跌幅阈值", threshold_control)
        form.addRow("检查间隔", interval_control)
        layout.addLayout(form)

        self.error_label = QLabel("")
        self.error_label.setObjectName("dialogError")
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName("saveButton")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("cancelButton")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        dialog_stylesheet = """
            QDialog { background: #0c1622; color: #c8d2df; }
            QLabel, QCheckBox { color: #c8d2df; font-family: "Noto Sans SC"; }
            QLabel#dialogTitle { color: #eef4fb; font: 600 18px "Microsoft YaHei UI"; }
            QLabel#dialogHelp { color: #8797aa; font-size: 12px; }
            QLabel#dialogError { color: #f05a5f; font-size: 12px; }
            QLabel#sliderValue { color: #aebed1; font: 600 12px "Noto Sans SC"; }
            QListWidget#watchlistEditor, QLineEdit#watchlistAddInput {
                color: #e6edf6; background: #111e2c; border: 1px solid #324257;
                border-radius: 8px; padding: 7px; font: 13px "Noto Sans SC";
            }
            QListWidget#watchlistEditor::item { min-height: 27px; padding: 3px 5px; }
            QListWidget#watchlistEditor::item:selected { background: #1d3f70; }
            QSlider#settingsSlider::groove:horizontal {
                height: 5px; background: #29394c; border-radius: 2px;
            }
            QSlider#settingsSlider::sub-page:horizontal {
                background: #3f7fe2; border-radius: 2px;
            }
            QSlider#settingsSlider::handle:horizontal {
                width: 16px; margin: -6px 0; background: #eef5ff;
                border: 1px solid #3f7fe2; border-radius: 8px;
            }
            QPushButton {
                color: #b8c5d6; background: #111e2c; border: 1px solid #324257;
                border-radius: 8px; padding: 7px 18px; font: 600 12px "Noto Sans SC";
            }
            QPushButton:hover { color: #eef5ff; border-color: #4b82de; background: #16263a; }
            QPushButton#saveButton { color: white; background: #2467d8; border-color: #3b7ce8; }
            QPushButton#saveButton:hover { background: #3278e8; }
            """
        if theme == "beige":
            dialog_stylesheet += """
            QDialog { background: #f6f8fb; color: #243247; }
            QLabel, QCheckBox { color: #243247; }
            QLabel#dialogTitle { color: #17253a; }
            QLabel#dialogHelp { color: #68778c; }
            QLabel#sliderValue { color: #40516a; }
            QListWidget#watchlistEditor, QLineEdit#watchlistAddInput {
                color: #1f2d42; background: #ffffff; border-color: #c6d2e1;
            }
            QListWidget#watchlistEditor::item:selected { color: #ffffff; background: #2d6ed8; }
            QSlider#settingsSlider::groove:horizontal { background: #d3dde9; }
            QSlider#settingsSlider::sub-page:horizontal { background: #2d6ed8; }
            QSlider#settingsSlider::handle:horizontal {
                background: #ffffff; border-color: #2d6ed8;
            }
            QPushButton { color: #40516a; background: #f0f4f9; border-color: #c6d2e1; }
            QPushButton:hover { color: #1e56af; background: #e7effb; border-color: #6b97dc; }
            QPushButton#saveButton { color: white; background: #2d6ed8; border-color: #2d6ed8; }
            QPushButton#saveButton:hover { background: #3b7de7; }
            """
        self.setStyleSheet(dialog_stylesheet)

    @Slot(int)
    def _update_interval_value(self, index: int) -> None:
        seconds = self.INTERVAL_OPTIONS[index]
        if seconds < 60:
            text = f"{seconds} 秒"
        else:
            text = f"{seconds // 60} 分钟"
        self.interval_value.setText(text)

    def _checked_watchlist_codes(self) -> list[str]:
        return [
            str(item.data(Qt.ItemDataRole.UserRole))
            for index in range(self.stock_list.count())
            if (item := self.stock_list.item(index)).checkState() == Qt.CheckState.Checked
        ]

    def _find_watchlist_item(self, provider_symbol: str) -> QListWidgetItem | None:
        for index in range(self.stock_list.count()):
            item = self.stock_list.item(index)
            try:
                item_key = normalize_symbol(
                    str(item.data(Qt.ItemDataRole.UserRole))
                ).provider_symbol
            except SymbolError:
                continue
            if item_key == provider_symbol:
                return item
        return None

    def _append_watchlist_item(self, raw_symbol: str, *, checked: bool = True) -> bool:
        normalized = normalize_watchlist([raw_symbol])
        if not normalized:
            return False
        symbol = normalize_symbol(normalized[0])
        existing = self._find_watchlist_item(symbol.provider_symbol)
        if existing is not None:
            if checked and existing.checkState() != Qt.CheckState.Checked:
                if len(self._checked_watchlist_codes()) >= 20:
                    return False
                existing.setCheckState(Qt.CheckState.Checked)
            self.stock_list.setCurrentItem(existing)
            return False
        if checked and len(self._checked_watchlist_codes()) >= 20:
            return False
        item = QListWidgetItem(f"{symbol.display_code}  ·  {symbol.market_label}")
        item.setData(Qt.ItemDataRole.UserRole, symbol.code)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.stock_list.addItem(item)
        return True

    @Slot()
    def _add_stock_item(self) -> None:
        raw_symbol = self.stock_add_input.text().strip()
        if not raw_symbol:
            self.error_label.setText("请先输入股票代码。")
            return
        try:
            symbol = normalize_symbol(raw_symbol)
            existing = self._find_watchlist_item(symbol.provider_symbol)
            added = self._append_watchlist_item(raw_symbol)
        except SymbolError as exc:
            self.error_label.setText(str(exc))
            return
        if not added and existing is None and len(self._checked_watchlist_codes()) >= 20:
            self.error_label.setText("自选股最多保存 20 只，请先取消勾选或删除一项。")
            return
        self.stock_add_input.clear()
        self.error_label.clear()
        self.import_status_label.setText(
            f"已添加 {symbol.display_code}。" if added else f"{symbol.display_code} 已在列表中。"
        )
        self._update_watchlist_count()

    @Slot()
    def _remove_selected_stock_items(self) -> None:
        selected = self.stock_list.selectedItems()
        if not selected:
            self.error_label.setText("请先在列表中点选要删除的行。")
            return
        for item in selected:
            self.stock_list.takeItem(self.stock_list.row(item))
        self.error_label.clear()
        self.import_status_label.setText(f"已删除 {len(selected)} 项，点击保存后生效。")
        self._update_watchlist_count()

    def _update_watchlist_count(self, _item: QListWidgetItem | None = None) -> None:
        selected_count = len(self._checked_watchlist_codes())
        self.watchlist_count_label.setText(f"已勾选 {selected_count}/20")
        self.add_stock_button.setEnabled(selected_count < 20)

    @Slot()
    def _import_futu_watchlist(self) -> None:
        if self.provider is None:
            return
        current = self._checked_watchlist_codes()
        dialog = FutuWatchlistImportDialog(
            self.provider,
            current,
            theme=self.theme,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected_codes:
            return
        imported_count = 0
        for code in dialog.selected_codes:
            if self._append_watchlist_item(code):
                imported_count += 1
        self.error_label.clear()
        self.import_status_label.setText(
            f"已从富途合并 {imported_count} 只，点击保存后生效。"
        )
        self._update_watchlist_count()

    @Slot()
    def _validate_and_accept(self) -> None:
        values = self._checked_watchlist_codes()
        try:
            watchlist = normalize_watchlist(values)
        except SymbolError as exc:
            self.error_label.setText(str(exc))
            return
        self.saved_config = (
            watchlist,
            self.threshold.value() / 10.0,
            self.INTERVAL_OPTIONS[self.interval.value()],
            bool(self.alerts_enabled.isChecked()),
        )
        self.accept()


class SettingsDialog(QDialog):
    def __init__(
        self,
        settings: QSettings,
        provider: HybridQuoteProvider,
        theme: str = "dark",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        _load_ui_fonts()
        self.settings = settings
        self.provider = provider
        self._provider_status_task: ProviderStatusTask | None = None
        self._update_task: UpdateCheckTask | None = None
        self._release_url = f"{PROJECT_URL}/releases"
        self.setWindowTitle("设置")
        self.setModal(True)
        self.setFixedSize(430, 478)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(14)

        title = QLabel("设置")
        title.setObjectName("settingsTitle")
        subtitle = QLabel("行情源、应用信息与版本更新")
        subtitle.setObjectName("settingsSubtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        source_card = QFrame()
        source_card.setObjectName("sourceCard")
        source_layout = QVBoxLayout(source_card)
        source_layout.setContentsMargins(16, 13, 16, 13)
        source_layout.setSpacing(8)
        source_title = QLabel("行情数据源")
        source_title.setObjectName("cardTitle")
        source_layout.addWidget(source_title)

        source_row = QHBoxLayout()
        self.quote_source_combo = QComboBox()
        self.quote_source_combo.setObjectName("quoteSourceCombo")
        self.quote_source_combo.addItem("富途实时优先（推荐）", "auto")
        self.quote_source_combo.addItem("腾讯延迟行情", "tencent")
        saved_mode = str(settings.value("quote_source", "auto"))
        saved_index = self.quote_source_combo.findData(saved_mode)
        self.quote_source_combo.setCurrentIndex(max(0, saved_index))
        self.check_provider_button = QPushButton("检测 OpenD")
        self.check_provider_button.setObjectName("secondaryButton")
        self.check_provider_button.clicked.connect(self.check_provider_status)
        source_row.addWidget(self.quote_source_combo, 1)
        source_row.addWidget(self.check_provider_button)
        source_layout.addLayout(source_row)

        self.provider_status = QLabel(provider.status_text())
        self.provider_status.setObjectName("providerStatus")
        self.provider_status.setWordWrap(True)
        source_layout.addWidget(self.provider_status)
        self.quote_source_combo.currentIndexChanged.connect(self._on_quote_source_changed)
        layout.addWidget(source_card)

        version_card = QFrame()
        version_card.setObjectName("versionCard")
        version_layout = QVBoxLayout(version_card)
        version_layout.setContentsMargins(16, 14, 16, 14)
        version_layout.setSpacing(8)
        app_row = QHBoxLayout()
        app_name = QLabel("StockDeskPet 股票桌宠")
        app_name.setObjectName("appName")
        self.current_version_label = QLabel(f"当前版本  v{__version__}")
        self.current_version_label.setObjectName("versionValue")
        app_row.addWidget(app_name)
        app_row.addStretch()
        app_row.addWidget(self.current_version_label)
        version_layout.addLayout(app_row)

        self.update_status = QLabel("点击一次即可检查、下载、替换并重启")
        self.update_status.setObjectName("updateStatus")
        self.update_status.setWordWrap(True)
        version_layout.addWidget(self.update_status)

        self.update_progress = QProgressBar()
        self.update_progress.setObjectName("updateProgress")
        self.update_progress.setRange(0, 100)
        self.update_progress.setValue(0)
        self.update_progress.setTextVisible(True)
        self.update_progress.hide()
        version_layout.addWidget(self.update_progress)
        layout.addWidget(version_card)

        actions = QHBoxLayout()
        self.check_update_button = QPushButton("检查并更新")
        self.check_update_button.setObjectName("primaryButton")
        self.check_update_button.clicked.connect(self.check_for_updates)
        self.open_release_button = QPushButton("查看发布页")
        self.open_release_button.setObjectName("secondaryButton")
        self.open_release_button.clicked.connect(self.open_release_page)
        project_button = QPushButton("项目主页")
        project_button.setObjectName("secondaryButton")
        project_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(PROJECT_URL))
        )
        actions.addWidget(self.check_update_button)
        actions.addWidget(self.open_release_button)
        actions.addWidget(project_button)
        layout.addLayout(actions)
        layout.addStretch()

        close_button = QPushButton("关闭")
        close_button.setObjectName("closeSettingsButton")
        close_button.clicked.connect(self.accept)
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_row.addWidget(close_button)
        layout.addLayout(close_row)

        stylesheet = """
            QDialog { background: #0c1622; }
            QLabel { color: #c8d2df; font-family: "Noto Sans SC"; }
            QLabel#settingsTitle { color: #eef4fb; font: 600 20px "Microsoft YaHei UI"; }
            QLabel#settingsSubtitle { color: #7f90a5; font-size: 12px; }
            QFrame#sourceCard, QFrame#versionCard {
                background: #101b28; border: 1px solid #32445b; border-radius: 10px;
            }
            QLabel#cardTitle, QLabel#appName { color: #e8eff8; font: 600 14px "Noto Sans SC"; }
            QLabel#versionValue { color: #91a5bd; font-size: 12px; }
            QLabel#providerStatus, QLabel#updateStatus { color: #91a5bd; font-size: 12px; }
            QComboBox#quoteSourceCombo {
                color: #e4edf7; background: #0c1622; border: 1px solid #32445b;
                border-radius: 8px; padding: 7px 10px; min-height: 18px;
            }
            QComboBox#quoteSourceCombo:hover, QComboBox#quoteSourceCombo:focus { border-color: #4b83e3; }
            QComboBox#quoteSourceCombo::drop-down { border: 0; width: 24px; }
            QComboBox#quoteSourceCombo QAbstractItemView {
                color: #e4edf7; background: #101b28; border: 1px solid #32445b;
                selection-background-color: #2467d8;
            }
            QPushButton {
                border-radius: 8px; padding: 7px 12px; font: 600 12px "Noto Sans SC";
            }
            QPushButton#primaryButton { color: white; background: #2467d8; border: 1px solid #397bec; }
            QPushButton#primaryButton:hover { background: #3278e8; }
            QPushButton#primaryButton:disabled { background: #26364a; color: #74859b; }
            QProgressBar#updateProgress {
                color: #dce8f7; background: #0c1622; border: 1px solid #32445b;
                border-radius: 5px; height: 10px; text-align: center; font-size: 9px;
            }
            QProgressBar#updateProgress::chunk { background: #397bec; border-radius: 4px; }
            QPushButton#secondaryButton, QPushButton#closeSettingsButton {
                color: #a8b6c8; background: #111e2c; border: 1px solid #32445b;
            }
            QPushButton#secondaryButton:hover, QPushButton#closeSettingsButton:hover {
                color: #eef5ff; border-color: #4b83e3; background: #16263a;
            }
        """
        if theme == "beige":
            stylesheet += """
                QDialog { background: #f6f8fb; }
                QLabel { color: #243247; }
                QLabel#settingsTitle { color: #17253a; }
                QLabel#settingsSubtitle, QLabel#providerStatus, QLabel#versionValue, QLabel#updateStatus { color: #68778c; }
                QFrame#sourceCard, QFrame#versionCard { background: #ffffff; border-color: #c6d2e1; }
                QLabel#cardTitle, QLabel#appName { color: #1f2d42; }
                QComboBox#quoteSourceCombo {
                    color: #243247; background: #ffffff; border-color: #c6d2e1;
                }
                QComboBox#quoteSourceCombo:hover, QComboBox#quoteSourceCombo:focus { border-color: #6b97dc; }
                QComboBox#quoteSourceCombo QAbstractItemView {
                    color: #243247; background: #ffffff; border-color: #c6d2e1;
                    selection-background-color: #2d6ed8;
                }
                QProgressBar#updateProgress { color: #40516a; background: #e8eef6; border-color: #c6d2e1; }
                QProgressBar#updateProgress::chunk { background: #2d6ed8; }
                QPushButton#primaryButton { background: #2d6ed8; border-color: #2d6ed8; }
                QPushButton#primaryButton:hover { background: #3b7de7; }
                QPushButton#secondaryButton, QPushButton#closeSettingsButton {
                    color: #40516a; background: #f0f4f9; border-color: #c6d2e1;
                }
                QPushButton#secondaryButton:hover, QPushButton#closeSettingsButton:hover {
                    color: #1e56af; border-color: #6b97dc; background: #e7effb;
                }
            """
        self.setStyleSheet(stylesheet)

    @Slot(int)
    def _on_quote_source_changed(self, _index: int) -> None:
        mode = str(self.quote_source_combo.currentData())
        self.settings.setValue("quote_source", mode)
        self.settings.sync()
        self.provider.set_mode(mode)
        if mode == "tencent":
            self.provider_status.setText("当前使用腾讯公共行情；港股可能延迟约 15 分钟")
        else:
            self.provider_status.setText("富途实时优先；OpenD 不可用时自动切换腾讯备用行情")

    @Slot()
    def check_provider_status(self) -> None:
        if self._provider_status_task is not None:
            return
        self.check_provider_button.setDisabled(True)
        self.check_provider_button.setText("检测中…")
        self.provider_status.setText("正在连接本机 127.0.0.1:11111…")
        task = ProviderStatusTask(self.provider)
        task.signals.finished.connect(self._on_provider_status)
        task.signals.failed.connect(self._on_provider_status_error)
        self._provider_status_task = task
        QThreadPool.globalInstance().start(task)

    @Slot(object)
    def _on_provider_status(self, result: tuple[bool, str]) -> None:
        ok, message = result
        self._finish_provider_status_check()
        self.provider_status.setText(("✓ " if ok else "! ") + message)

    @Slot(str)
    def _on_provider_status_error(self, message: str) -> None:
        self._finish_provider_status_check()
        self.provider_status.setText(f"! {message}")

    def _finish_provider_status_check(self) -> None:
        self._provider_status_task = None
        self.check_provider_button.setDisabled(False)
        self.check_provider_button.setText("检测 OpenD")

    @Slot()
    def check_for_updates(self) -> None:
        if self._update_task is not None:
            return
        self.check_update_button.setDisabled(True)
        self.check_update_button.setText("检查中…")
        self.update_progress.setValue(0)
        self.update_progress.hide()
        self.update_status.setText("正在连接 GitHub Releases…")
        task = UpdateCheckTask(__version__)
        task.signals.finished.connect(self._on_update_info)
        task.signals.failed.connect(self._on_update_error)
        task.signals.phase.connect(self._on_update_phase)
        task.signals.progress.connect(self._on_update_progress)
        self._update_task = task
        QThreadPool.globalInstance().start(task)

    @Slot(object)
    def _on_update_info(self, info: AutomaticUpdateResult) -> None:
        self._release_url = info.release_url
        if info.status == "restart_pending":
            self.update_status.setText(
                f"v{info.latest_version} 已下载，正在退出并完成替换…"
            )
            self.update_progress.setValue(100)
            self.check_update_button.setText("正在重启…")
            QTimer.singleShot(600, QApplication.instance().quit)
            return
        self._finish_update_check()
        if info.status == "manual":
            self.update_status.setText(
                f"发现 v{info.latest_version}。当前是旧版单文件程序，需安装一次新版。"
            )
            self.open_release_button.setText("下载安装版")
        else:
            self.update_status.setText(f"已是最新版本 v{info.current_version}。")
            self.open_release_button.setText("查看发布页")

    @Slot(str)
    def _on_update_phase(self, message: str) -> None:
        self.update_status.setText(message)
        if "下载" in message:
            self.check_update_button.setText("下载中…")

    @Slot(int)
    def _on_update_progress(self, value: int) -> None:
        self.update_progress.show()
        self.update_progress.setValue(max(0, min(100, value)))

    @Slot(str)
    def _on_update_error(self, message: str) -> None:
        self._finish_update_check()
        self.update_status.setText(message)

    def _finish_update_check(self) -> None:
        self._update_task = None
        self.check_update_button.setDisabled(False)
        self.check_update_button.setText("检查并更新")

    @Slot()
    def open_release_page(self) -> None:
        QDesktopServices.openUrl(QUrl(self._release_url))


class QuotePanel(QWidget):
    quote_loaded = Signal(object)
    loading_changed = Signal(bool)
    watchlist_changed = Signal(object, float, int, bool)
    favorites_changed = Signal(object)
    page_refresh_requested = Signal(object)
    periodic_page_refresh_requested = Signal(object)
    theme_changed = Signal(str)

    def __init__(self, provider: TencentQuoteProvider, settings: QSettings) -> None:
        super().__init__()
        _load_ui_fonts()
        self.provider = provider
        self.settings = settings
        self.thread_pool = QThreadPool.globalInstance()
        self._active_task: FetchTask | None = None
        self._pending_fetch_symbol: str | None = None
        self._search_task: SearchTask | None = None
        self._pending_search_text = ""
        self._search_display_to_symbol: dict[str, str] = {}
        self._resolved_input_symbol: str | None = None
        self._open_tab_refresh_timer = QTimer(self)
        self._open_tab_refresh_timer.setInterval(OPEN_TAB_REFRESH_INTERVAL_MS)
        self._open_tab_refresh_timer.timeout.connect(self._refresh_open_tab)
        last_symbol = ""
        saved_watchlist = _settings_list(settings, "watchlist")
        if settings.value("market_defaults_v1_added") is None:
            saved_watchlist.extend(DEFAULT_HK_WATCHLIST)
            settings.setValue("market_defaults_v1_added", True)
        if settings.value("a_share_etf_defaults_v1_added") is None:
            saved_watchlist.extend(DEFAULT_A_SHARE_ETFS)
            settings.setValue("a_share_etf_defaults_v1_added", True)
        try:
            self.watchlist = normalize_watchlist(saved_watchlist)
        except SymbolError:
            self.watchlist = list(DEFAULT_WATCHLIST)
        settings.setValue("watchlist", self.watchlist)
        settings.sync()
        self.alert_threshold = float(settings.value("alert_threshold", 3.0))
        self.interval_seconds = int(settings.value("alert_interval_seconds", 60))
        self.alerts_enabled = _setting_bool(settings.value("alerts_enabled", True))
        self.favorite_symbols = _normalize_favorites(_settings_list(settings, "favorites"))
        self.panel_opacity = max(90, min(100, int(settings.value("panel_opacity", 100))))
        settings.setValue("favorites", self.favorite_symbols)
        settings.setValue("panel_opacity", self.panel_opacity)
        settings.sync()
        self.current_theme = str(settings.value("theme", "dark"))
        if self.current_theme not in {"dark", "beige"}:
            self.current_theme = "dark"
        self._quote_cache: dict[str, Quote] = {}

        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(448, 650)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        self.card = QFrame()
        self.card.setObjectName("card")
        outer.addWidget(self.card)

        layout = QVBoxLayout(self.card)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("股票桌宠")
        title.setObjectName("title")
        self.theme_switch = ThemeSwitch()
        self.theme_switch.setChecked(self.current_theme == "beige")
        self.theme_switch.toggled.connect(self._on_theme_switch_toggled)
        manage_button = QPushButton("编辑自选")
        manage_button.setObjectName("headerButton")
        manage_button.clicked.connect(self.manage_watchlist)
        settings_button = QPushButton("设置")
        settings_button.setObjectName("headerButton")
        settings_button.clicked.connect(self.open_settings)
        close_button = QPushButton("×")
        close_button.setObjectName("closeButton")
        close_button.setFixedSize(28, 28)
        close_button.clicked.connect(self.hide)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.theme_switch)
        header.addWidget(manage_button)
        header.addWidget(settings_button)
        header.addWidget(close_button)
        layout.addLayout(header)

        search = QHBoxLayout()
        self.symbol_input = QLineEdit()
        self.symbol_input.setText(last_symbol)
        self.symbol_input.setPlaceholderText("代码或名称：01810 / 小米 / HSI")
        self.symbol_input.setClearButtonEnabled(True)
        self._search_model = QStringListModel(self)
        self.search_completer = QCompleter(self._search_model, self)
        self.search_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.search_completer.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.search_completer.setMaxVisibleItems(8)
        self.search_completer.activated[str].connect(self._on_search_suggestion)
        self.symbol_input.setCompleter(self.search_completer)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(350)
        self._search_timer.timeout.connect(self._search_by_name)
        self.symbol_input.textChanged.connect(self._on_search_text_changed)
        self.symbol_input.textEdited.connect(self._schedule_name_search)
        self.symbol_input.returnPressed.connect(self.search_or_fetch)
        search.addWidget(self.symbol_input, 1)
        layout.addLayout(search)

        watchlist_row = QHBoxLayout()
        save_button = QPushButton("☆ 保存当前 ID")
        save_button.setObjectName("secondaryButton")
        save_button.clicked.connect(self.save_current_symbol)
        refresh_page_button = QPushButton("刷新当前页")
        refresh_page_button.setObjectName("secondaryButton")
        refresh_page_button.clicked.connect(self.refresh_current_tab)
        self.monitor_label = QLabel("")
        self.monitor_label.setObjectName("monitor")
        opacity_label = QLabel("透明度")
        opacity_label.setObjectName("opacityLabel")
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setObjectName("opacitySlider")
        self.opacity_slider.setRange(90, 100)
        self.opacity_slider.setValue(self.panel_opacity)
        self.opacity_slider.setFixedWidth(54)
        self.opacity_slider.setToolTip("调整行情卡透明度（90%–100%）")
        self.opacity_value_label = QLabel(f"{self.panel_opacity}%")
        self.opacity_value_label.setObjectName("opacityValue")
        self.opacity_value_label.setFixedWidth(30)
        self.opacity_slider.valueChanged.connect(self.set_panel_opacity)
        watchlist_row.addWidget(save_button)
        watchlist_row.addWidget(refresh_page_button)
        watchlist_row.addWidget(opacity_label)
        watchlist_row.addWidget(self.opacity_slider)
        watchlist_row.addWidget(self.opacity_value_label)
        watchlist_row.addStretch()
        watchlist_row.addWidget(self.monitor_label)
        layout.addLayout(watchlist_row)

        self.market_tabs = EqualWidthTabWidget()
        self.market_tabs.setObjectName("marketTabs")
        self.market_tabs.setFixedHeight(235)
        self.a_share_list = QListWidget()
        self.hk_share_list = QListWidget()
        self.favorite_list = QListWidget()
        self.index_list = QListWidget()
        for stock_list in (
            self.a_share_list,
            self.hk_share_list,
            self.favorite_list,
            self.index_list,
        ):
            stock_list.setObjectName("stockList")
            stock_list.setAlternatingRowColors(False)
            stock_list.setItemDelegate(QuoteItemDelegate(stock_list))
            stock_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            stock_list.itemClicked.connect(self._on_watchlist_item_clicked)
        self.index_list.setObjectName("indexList")
        self.market_tabs.addTab(self.a_share_list, "大A")
        self.market_tabs.addTab(self.hk_share_list, "港股")
        self.market_tabs.addTab(self.index_list, "指数")
        self.market_tabs.addTab(self.favorite_list, "收藏")
        layout.addWidget(self.market_tabs)

        self.name_label = QLabel("点击桌宠或输入代码拉取行情")
        self.name_label.setObjectName("stockName")
        self.price_label = QLabel("--")
        self.price_label.setObjectName("price")
        self.change_label = QLabel("--")
        self.change_label.setObjectName("change")
        price_row = QHBoxLayout()
        price_row.addWidget(self.price_label)
        price_row.addWidget(self.change_label)
        price_row.addStretch()
        layout.addWidget(self.name_label)
        layout.addLayout(price_row)

        self.details_label = QLabel("今开 --    最高 --    最低 --\n昨收 --    涨跌 --\n成交额 --    换手 --")
        self.details_label.setObjectName("details")
        layout.addWidget(self.details_label)
        layout.addStretch()

        self.market_summary_frame = QFrame()
        self.market_summary_frame.setObjectName("marketSummary")
        self.market_summary_frame.setToolTip("当前 Tab 的关键市场指数，随当前页刷新")
        summary_layout = QHBoxLayout(self.market_summary_frame)
        summary_layout.setContentsMargins(8, 6, 8, 6)
        summary_layout.setSpacing(0)
        self.market_summary_cells: list[QLabel] = []
        self.market_summary_dividers: list[QFrame] = []
        for index in range(3):
            cell = QLabel("")
            cell.setObjectName("marketSummaryCell")
            cell.setTextFormat(Qt.TextFormat.RichText)
            cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.setMinimumHeight(38)
            self.market_summary_cells.append(cell)
            summary_layout.addWidget(cell, 1)
            if index < 2:
                divider = QFrame()
                divider.setObjectName("summaryDivider")
                divider.setFixedWidth(1)
                self.market_summary_dividers.append(divider)
                summary_layout.addWidget(divider)
        layout.addWidget(self.market_summary_frame)

        provider_status = (
            provider.status_text()
            if hasattr(provider, "status_text")
            else "腾讯行情公共网页接口"
        )
        self.status_label = QLabel(f"行情源：{provider_status}；仅供参考")
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        self.footer_version_label = QLabel(f"v{__version__}")
        self.footer_version_label.setObjectName("footerVersion")
        self.footer_version_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom
        )
        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addWidget(self.status_label, 1)
        footer.addWidget(self.footer_version_label)
        layout.addLayout(footer)

        self._dark_stylesheet = (
            """
            QFrame#card {
                background: rgba(10, 18, 28, 252);
                border: 1px solid #2f3e51;
                border-radius: 18px;
            }
            QLabel { color: #c7d1de; font-family: "Noto Sans SC"; }
            QLabel#title {
                color: #edf3fa; font: 650 18px "Noto Sans SC";
            }
            QLabel#stockName {
                color: #c7d1de; font: 600 14px "Noto Sans SC";
            }
            QLabel#price { color: #eef4fb; font: 700 32px "Noto Sans SC"; }
            QLabel#change { font-size: 17px; font-weight: 600; }
            QLabel#details { color: #9aa9bd; font-size: 12px; line-height: 1.5; }
            QFrame#marketSummary {
                background: #0e1926;
                border-top: 1px solid #2f4055; border-bottom: 1px solid #2f4055;
            }
            QLabel#marketSummaryCell { color: #b7c4d5; font-size: 10px; }
            QFrame#summaryDivider { background: #2f4055; border: none; }
            QLabel#status { color: #73849a; font-size: 10px; }
            QLabel#footerVersion { color: #596b82; font-size: 10px; }
            QLabel#monitor { color: #7f90a5; font-size: 10px; }
            QLabel#opacityLabel, QLabel#opacityValue { color: #7f90a5; font-size: 10px; }
            QTabWidget#marketTabs::pane {
                background: #101b28; border: 1px solid #364a63; border-top: none;
                border-radius: 0; top: 0;
            }
            QTabBar::tab {
                color: #9aaabd; background: #0e1824; border: 1px solid #364a63;
                border-right: none; margin: 0;
                padding: 7px 12px; min-width: 60px;
                font: 600 12px "Noto Sans SC";
            }
            QTabBar::tab:first { border-top-left-radius: 8px; }
            QTabBar::tab:last { border-right: 1px solid #364a63; border-top-right-radius: 8px; }
            QTabBar::tab:selected {
                color: #ffffff; background: #2464d3; border-color: #3678e7;
                border-right: 1px solid #3678e7;
            }
            QListWidget#stockList, QListWidget#indexList {
                color: #cbd5e1; background: #101b28; border: none;
                border-radius: 0;
                outline: none; padding: 0; font: 12px "Noto Sans SC";
            }
            QListWidget#stockList::item {
                border: none; border-bottom: 1px solid #2f4055;
                border-radius: 0; margin: 0; padding: 9px 12px;
            }
            QListWidget#stockList::item:hover { background: #162538; }
            QListWidget#stockList::item:selected {
                background: #17283b;
            }
            QWidget#quoteRow { background: transparent; }
            QLabel#quoteRowText {
                background: transparent; border: none; font: 12px "Noto Sans SC";
            }
            QAbstractButton#favoriteButton { background: transparent; border: none; padding: 0; }
            QListWidget#indexList { font-size: 11px; }
            QListWidget#indexList::item {
                border: none; border-bottom: 1px solid #2f4055;
                border-radius: 0; margin: 0; padding: 1px 10px;
            }
            QListWidget#indexList::item:disabled {
                background: #0c1621; border: none;
                margin: 0; padding: 1px 10px;
            }
            QListWidget#indexList::item:hover { background: #162538; }
            QListWidget#indexList::item:selected {
                background: #17283b;
            }
            QLineEdit {
                color: #1f2d42; background: #ffffff; border: 1px solid #64748b;
                border-radius: 10px; padding: 8px 10px; font: 13px "Noto Sans SC";
            }
            QLineEdit:focus { border-color: #4b83e3; }
            QPushButton { font-family: "Noto Sans SC"; cursor: pointer; }
            QPushButton#secondaryButton, QPushButton#headerButton {
                color: #a8b6c8; background: #101b28; border: 1px solid #32445b;
                border-radius: 8px; padding: 5px 7px; font-size: 11px;
            }
            QPushButton#secondaryButton:hover, QPushButton#headerButton:hover {
                color: #eef5ff; border-color: #4b83e3; background: #16263a;
            }
            QPushButton#closeButton {
                color: #7f91a8; background: transparent; border: none; font-size: 22px;
            }
            QPushButton#closeButton:hover {
                color: #eef4fb; background: #16263a; border-radius: 8px;
            }
            QSlider#opacitySlider::groove:horizontal {
                height: 4px; background: #29394c; border-radius: 2px;
            }
            QSlider#opacitySlider::sub-page:horizontal {
                background: #3f7fe2; border-radius: 2px;
            }
            QSlider#opacitySlider::handle:horizontal {
                width: 12px; margin: -4px 0; background: #eef5ff;
                border: 1px solid #3f7fe2; border-radius: 6px;
            }
            """
        )
        self._beige_stylesheet = self._dark_stylesheet + """
            QFrame#card {
                background: rgba(247, 249, 252, 252);
                border-color: #b9c7d8;
            }
            QLabel { color: #243247; }
            QLabel#title { color: #17253a; }
            QLabel#stockName { color: #34455d; }
            QLabel#price { color: #17253a; }
            QLabel#details { color: #5f6f84; }
            QFrame#marketSummary {
                background: #f1f5fa;
                border-top-color: #cbd6e3; border-bottom-color: #cbd6e3;
            }
            QLabel#marketSummaryCell { color: #40516a; }
            QFrame#summaryDivider { background: #cbd6e3; }
            QLabel#status, QLabel#monitor, QLabel#opacityLabel, QLabel#opacityValue { color: #6d7d91; }
            QLabel#footerVersion { color: #8896a8; }
            QTabWidget#marketTabs::pane {
                background: #ffffff; border-color: #aebed2;
            }
            QTabBar::tab {
                color: #4f6178; background: #edf2f7; border-color: #b8c7d8;
            }
            QTabBar::tab:last { border-right-color: #b8c7d8; }
            QTabBar::tab:selected {
                color: #ffffff; background: #2d6ed8; border-color: #2d6ed8;
            }
            QListWidget#stockList, QListWidget#indexList {
                color: #26364b; background: #ffffff;
            }
            QListWidget#stockList::item, QListWidget#indexList::item {
                border-bottom-color: #d5dee9;
            }
            QListWidget#indexList::item:disabled { background: #eef3f8; }
            QListWidget#stockList::item:hover, QListWidget#indexList::item:hover {
                background: #edf3fb;
            }
            QListWidget#stockList::item:selected, QListWidget#indexList::item:selected {
                background: #e3edfc;
            }
            QLineEdit {
                color: #1f2d42; background: #ffffff; border-color: #c4d0df;
            }
            QLineEdit:focus { border-color: #5c8dde; }
            QPushButton#secondaryButton, QPushButton#headerButton {
                color: #40516a; background: #f2f6fa; border-color: #c4d0df;
            }
            QPushButton#secondaryButton:hover, QPushButton#headerButton:hover {
                color: #1e56af; border-color: #6b97dc; background: #e7effb;
            }
            QPushButton#closeButton { color: #6d7d91; }
            QPushButton#closeButton:hover {
                color: #1f2d42; background: #e7eef8;
            }
            QSlider#opacitySlider::groove:horizontal { background: #d3dde9; }
            QSlider#opacitySlider::sub-page:horizontal { background: #2d6ed8; }
            QSlider#opacitySlider::handle:horizontal {
                background: #ffffff; border-color: #2d6ed8;
            }
            """
        self._apply_theme()
        self.market_tabs.currentChanged.connect(self._on_market_tab_changed)
        self._refresh_watchlist_lists()
        self._update_market_summary()
        self._select_first_current_tab_item()
        self._update_monitor_label()
        self.setWindowOpacity(self.panel_opacity / 100.0)

    def _flat_color(self) -> str:
        return "#40516a" if self.current_theme == "beige" else "#c7d1de"

    def _up_color(self) -> str:
        return "#d63f45" if self.current_theme == "beige" else "#f05a5f"

    def _down_color(self) -> str:
        return "#248b57" if self.current_theme == "beige" else "#3dbc73"

    def _apply_theme(self) -> None:
        is_beige = self.current_theme == "beige"
        self.setStyleSheet(self._beige_stylesheet if is_beige else self._dark_stylesheet)
        if is_beige:
            self.search_completer.popup().setStyleSheet(
                "QListView { color:#243247; background:#ffffff; border:1px solid #b9c7d8; "
                "padding:4px; font:12px 'Noto Sans SC'; } "
                "QListView::item { padding:7px 8px; border-radius:5px; } "
                "QListView::item:selected { color:#ffffff; background:#2d6ed8; }"
            )
        else:
            self.search_completer.popup().setStyleSheet(
                "QListView { color:#c7d1de; background:#101b28; border:1px solid #32445b; "
                "padding:4px; font:12px 'Noto Sans SC'; } "
                "QListView::item { padding:7px 8px; border-radius:5px; } "
                "QListView::item:selected { color:#ffffff; background:#2464d3; }"
            )
        self.symbol_input.setProperty("lightTheme", is_beige)
        self.symbol_input.update()
        was_blocked = self.theme_switch.blockSignals(True)
        self.theme_switch.setChecked(is_beige)
        self.theme_switch.blockSignals(was_blocked)
        self.theme_switch.setToolTip("当前浅色主题，点击切换到深色" if is_beige else "当前深色主题，点击切换到浅色")
        self.theme_switch.update()

    @Slot(bool)
    def _on_theme_switch_toggled(self, is_beige: bool) -> None:
        self._set_theme("beige" if is_beige else "dark")

    def _set_theme(self, theme: str) -> None:
        if theme == self.current_theme:
            return
        self.current_theme = theme
        self.settings.setValue("theme", self.current_theme)
        self.settings.sync()
        self._apply_theme()
        self.update_watchlist_quotes([])
        theme_name = "浅色" if self.current_theme == "beige" else "深色"
        self.status_label.setText(f"已切换为{theme_name}主题")
        self.theme_changed.emit(self.current_theme)

    @Slot()
    def toggle_theme(self) -> None:
        self.theme_switch.toggle()

    @staticmethod
    def _extract_symbol_from_text(text: str) -> str | None:
        candidates = [text, *(part.strip() for part in text.split("·"))]
        for candidate in candidates:
            try:
                return normalize_symbol(candidate).code
            except SymbolError:
                continue
        return None

    def _current_input_symbol(self) -> str:
        if self._resolved_input_symbol:
            return self._resolved_input_symbol
        text = self.symbol_input.text().strip()
        return self._extract_symbol_from_text(text) or text

    def _set_symbol_input_display(self, raw_symbol: str, name: str = "") -> None:
        symbol = normalize_symbol(raw_symbol)
        self._resolved_input_symbol = symbol.code
        parts = [name.strip(), symbol.display_code, symbol.market_label]
        self.symbol_input.setText(" · ".join(part for part in parts if part))

    @Slot(str)
    def _on_search_text_changed(self, text: str) -> None:
        if text.strip():
            return
        self._search_timer.stop()
        self._pending_search_text = ""
        self._resolved_input_symbol = None
        self._search_display_to_symbol.clear()
        self._search_model.setStringList([])
        self.search_completer.popup().hide()

    @Slot(str)
    def _schedule_name_search(self, text: str) -> None:
        self._resolved_input_symbol = None
        keyword = text.strip()
        if not keyword:
            self._search_timer.stop()
            self.search_completer.popup().hide()
            return
        try:
            normalize_symbol(keyword)
        except SymbolError:
            self._search_timer.start()
        else:
            self._search_timer.stop()
            self.search_completer.popup().hide()

    @Slot()
    def _search_by_name(self) -> None:
        self._start_name_search(self.symbol_input.text().strip())

    def _start_name_search(self, keyword: str) -> None:
        if not keyword:
            return
        if self._search_task is not None:
            self._pending_search_text = keyword
            return
        self.status_label.setText(f"正在搜索“{keyword}”…")
        task = SearchTask(self.provider, keyword)
        task.signals.finished.connect(self._on_search_results)
        self._search_task = task
        self.thread_pool.start(task)

    @Slot(object)
    def _on_search_results(
        self,
        payload: tuple[str, list[StockSearchResult], str],
    ) -> None:
        keyword, results, error = payload
        self._search_task = None
        current_text = self.symbol_input.text().strip()
        if current_text == keyword:
            self._search_display_to_symbol = {
                f"{result.name} · {result.symbol.display_code} · {result.symbol.market_label}": result.symbol.code
                for result in results
            }
            self._search_model.setStringList(list(self._search_display_to_symbol))
            if results:
                self.status_label.setText(f"找到 {len(results)} 个结果，请选择一项")
                self.search_completer.setCompletionPrefix("")
                self.search_completer.complete()
            else:
                self.search_completer.popup().hide()
                self.status_label.setText(error or f"没有找到“{keyword}”对应的大A或港股")

        pending = self._pending_search_text
        self._pending_search_text = ""
        if pending and pending != keyword:
            self._start_name_search(pending)

    @Slot(str)
    def _on_search_suggestion(self, display_text: str) -> None:
        symbol = self._search_display_to_symbol.get(display_text)
        if not symbol:
            return
        self._search_timer.stop()
        self._resolved_input_symbol = symbol
        self.symbol_input.setText(display_text)
        self.fetch_quote()

    @Slot()
    def search_or_fetch(self) -> None:
        keyword = self._current_input_symbol()
        if not keyword:
            self._show_error("请输入股票代码或名称。")
            return
        try:
            normalize_symbol(keyword)
        except SymbolError:
            self._search_timer.stop()
            self._start_name_search(keyword)
            return
        self.fetch_quote()

    def fetch_symbol(self, symbol: str) -> None:
        normalized = normalize_symbol(symbol)
        quote = self._quote_cache.get(normalized.provider_symbol)
        self._set_symbol_input_display(symbol, quote.name if quote else "")
        if quote is not None:
            self._display_quote(quote)
        self.fetch_quote()

    def _on_watchlist_item_clicked(self, item: QListWidgetItem) -> None:
        symbol = item.data(Qt.ItemDataRole.UserRole)
        if symbol:
            self.fetch_symbol(str(symbol))

    def _on_market_tab_changed(self, index: int) -> None:
        self._update_market_summary()
        self._select_first_current_tab_item()
        symbols = self._current_tab_refresh_symbols()
        if symbols:
            self.periodic_page_refresh_requested.emit(symbols)
        if symbols and not _automatic_refresh_symbols(symbols):
            self.status_label.setText("当前市场已休市，显示最近一次行情")

    def _current_tab_list_symbols(self) -> list[str]:
        a_shares, hk_shares = partition_watchlist(self.watchlist)
        return {
            0: a_shares,
            1: hk_shares,
            2: list(INDEX_SYMBOLS),
            3: list(self.favorite_symbols),
        }.get(self.market_tabs.currentIndex(), [])

    def _current_tab_refresh_symbols(self) -> list[str]:
        summary_symbols = [
            symbol
            for _label, symbol in TAB_MARKET_SUMMARIES.get(
                self.market_tabs.currentIndex(), ()
            )
        ]
        return list(dict.fromkeys([*self._current_tab_list_symbols(), *summary_symbols]))

    @Slot()
    def refresh_current_tab(self) -> None:
        list_symbols = self._current_tab_list_symbols()
        symbols = self._current_tab_refresh_symbols()
        if not list_symbols:
            self._clear_quote_display()
        if not symbols:
            self.status_label.setText("当前页暂无可刷新行情")
            return
        tab_name = self.market_tabs.tabText(self.market_tabs.currentIndex()).split(" ", 1)[0]
        self.status_label.setText(f"正在刷新{tab_name}的 {len(symbols)} 项行情…")
        self.page_refresh_requested.emit(symbols)

    @Slot()
    def _refresh_open_tab(self) -> None:
        if not self.isVisible():
            return
        symbols = self._current_tab_refresh_symbols()
        if symbols:
            self.periodic_page_refresh_requested.emit(symbols)

    def _clear_quote_display(self) -> None:
        self._resolved_input_symbol = None
        self.symbol_input.clear()
        self.name_label.setText("未选择行情")
        self.price_label.setText("--")
        self.price_label.setStyleSheet("")
        self.change_label.setText("")
        self.change_label.setStyleSheet("")
        self.details_label.setText("今开 --    最高 --    最低 --\n昨收 --    涨跌 --\n成交额 --    换手 --")
        self.status_label.setText("点击自选列表或输入代码/名称拉取行情；行情可能延迟，仅供参考")

    def _current_stock_list(self) -> QListWidget | None:
        return {
            0: self.a_share_list,
            1: self.hk_share_list,
            2: self.index_list,
            3: self.favorite_list,
        }.get(self.market_tabs.currentIndex())

    def _select_first_current_tab_item(self) -> None:
        stock_list = self._current_stock_list()
        if stock_list is None:
            return
        for row in range(stock_list.count()):
            item = stock_list.item(row)
            symbol = item.data(Qt.ItemDataRole.UserRole)
            if not symbol:
                continue
            stock_list.setCurrentItem(item)
            normalized = normalize_symbol(str(symbol))
            quote = self._quote_cache.get(normalized.provider_symbol)
            self._set_symbol_input_display(str(symbol), quote.name if quote else "")
            if quote is not None:
                self._display_quote(quote)
            else:
                self.name_label.setText(f"{normalized.display_code} · 等待刷新")
                self.price_label.setText("--")
                self.price_label.setStyleSheet("")
                self.change_label.setText("")
                self.change_label.setStyleSheet("")
                self.details_label.setText("今开 --    最高 --    最低 --\n昨收 --    涨跌 --\n成交额 --    换手 --")
            return
        self._clear_quote_display()

    def _select_symbol_in_current_list(self, raw_symbol: str) -> None:
        if not raw_symbol:
            return
        try:
            target = normalize_symbol(raw_symbol).provider_symbol
        except SymbolError:
            return
        stock_list = self._current_stock_list()
        if stock_list is None:
            return
        for row in range(stock_list.count()):
            item = stock_list.item(row)
            symbol = item.data(Qt.ItemDataRole.UserRole)
            if symbol and normalize_symbol(str(symbol)).provider_symbol == target:
                stock_list.setCurrentItem(item)
                return

    def _update_market_summary(self) -> None:
        summary_items = TAB_MARKET_SUMMARIES.get(self.market_tabs.currentIndex(), ())
        self.market_summary_frame.setVisible(bool(summary_items))
        label_color = "#64758a" if self.current_theme == "beige" else "#8799af"
        missing_color = "#7a899b" if self.current_theme == "beige" else "#74869d"
        for index, cell in enumerate(self.market_summary_cells):
            visible = index < len(summary_items)
            cell.setVisible(visible)
            if not visible:
                cell.clear()
                continue
            label, raw_symbol = summary_items[index]
            symbol = normalize_symbol(raw_symbol)
            quote = self._quote_cache.get(symbol.provider_symbol)
            if quote is None:
                price_text = "--"
                change_text = ""
                color = missing_color
            else:
                arrow = "▲" if quote.direction > 0 else "▼" if quote.direction < 0 else "—"
                color = self._up_color() if quote.direction > 0 else self._down_color() if quote.direction < 0 else self._flat_color()
                price_text = f"{_currency_prefix(quote.symbol.currency)}{_price(quote.price)}"
                change_text = f"{arrow}{quote.change_percent:+.2f}%"
            cell.setText(
                f'<span style="color:{label_color}">{label}</span><br>'
                f'<span style="color:{color};font-weight:600">{price_text}</span> '
                f'<span style="color:{color}">{change_text}</span>'
            )
        for index, divider in enumerate(self.market_summary_dividers):
            divider.setVisible(index < len(summary_items) - 1)

    def _refresh_watchlist_lists(self) -> None:
        a_shares, hk_shares = partition_watchlist(self.watchlist)
        self._populate_market_list(self.a_share_list, a_shares, "暂无大A自选，点击“编辑自选”添加")
        self._populate_market_list(self.hk_share_list, hk_shares, "暂无港股自选，点击“编辑自选”添加")
        self._populate_market_list(
            self.favorite_list,
            self.favorite_symbols,
            "暂无收藏，点击行情右侧 ☆ 添加",
        )
        self._populate_index_list()
        self.market_tabs.setTabText(0, f"大A ({len(a_shares)})")
        self.market_tabs.setTabText(1, f"港股 ({len(hk_shares)})")
        self.market_tabs.setTabText(2, f"指数 ({len(INDEX_SYMBOLS)})")
        self.market_tabs.setTabText(3, f"收藏 ({len(self.favorite_symbols)})")

        active_keys = {
            normalize_symbol(code).provider_symbol
            for code in [*self.watchlist, *self.favorite_symbols, *INDEX_SYMBOLS]
        }
        current_symbol = self._current_input_symbol()
        if current_symbol:
            try:
                active_keys.add(normalize_symbol(current_symbol).provider_symbol)
            except SymbolError:
                pass
        for symbol_key in list(self._quote_cache):
            if symbol_key not in active_keys:
                self._quote_cache.pop(symbol_key, None)

    def _populate_index_list(self) -> None:
        self.index_list.clear()
        favorite_keys = self._favorite_keys()
        for group_name, symbols in INDEX_GROUPS:
            header = QListWidgetItem(group_name)
            header.setForeground(QColor("#64758a" if self.current_theme == "beige" else "#8799af"))
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setSizeHint(QSize(0, 18))
            self.index_list.addItem(header)
            for code in symbols:
                symbol = normalize_symbol(code)
                quote = self._quote_cache.get(symbol.provider_symbol)
                if quote is None:
                    text = f"  {symbol.display_code}  ·  {symbol.market_label}"
                    color = self._flat_color()
                else:
                    arrow = "▲" if quote.direction > 0 else "▼" if quote.direction < 0 else "—"
                    prefix = _currency_prefix(quote.symbol.currency)
                    text = (
                        f"  {quote.name}  ·  {quote.symbol.display_code}    "
                        f"{arrow} {quote.change_percent:+.2f}%    {prefix}{_price(quote.price)}"
                    )
                    color = (
                        self._up_color()
                        if quote.direction > 0
                        else self._down_color() if quote.direction < 0 else self._flat_color()
                    )
                item = QListWidgetItem("")
                item.setData(Qt.ItemDataRole.UserRole, code)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, text)
                item.setForeground(QColor(color))
                item.setSizeHint(QSize(0, 18))
                item.setToolTip("点击拉取该指数的详细行情")
                self.index_list.addItem(item)
                self._attach_quote_row(
                    self.index_list,
                    item,
                    text,
                    code,
                    color,
                    symbol.provider_symbol in favorite_keys,
                    compact=True,
                )

    def _populate_market_list(
        self,
        stock_list: QListWidget,
        symbols: list[str],
        empty_message: str,
    ) -> None:
        stock_list.clear()
        if not symbols:
            empty_item = QListWidgetItem(empty_message)
            empty_item.setForeground(QColor("#7a899b" if self.current_theme == "beige" else "#74869d"))
            empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
            stock_list.addItem(empty_item)
            return

        favorite_keys = self._favorite_keys()
        for code in symbols:
            symbol = normalize_symbol(code)
            quote = self._quote_cache.get(symbol.provider_symbol)
            if quote is None:
                text = f"{symbol.display_code}  ·  {symbol.market_label}"
                color = self._flat_color()
            else:
                arrow = "▲" if quote.direction > 0 else "▼" if quote.direction < 0 else "—"
                currency = _currency_prefix(quote.symbol.currency)
                text = (
                    f"{quote.name}  ·  {quote.symbol.display_code}    "
                    f"{arrow} {quote.change_percent:+.2f}%    {currency}{_price(quote.price)}"
                )
                color = self._up_color() if quote.direction > 0 else self._down_color() if quote.direction < 0 else self._flat_color()
            item = QListWidgetItem("")
            item.setData(Qt.ItemDataRole.UserRole, code)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, text)
            item.setForeground(QColor(color))
            item.setSizeHint(QSize(0, 36))
            item.setToolTip("点击拉取这只股票的详细行情")
            stock_list.addItem(item)
            self._attach_quote_row(
                stock_list,
                item,
                text,
                code,
                color,
                symbol.provider_symbol in favorite_keys,
            )

    def _favorite_keys(self) -> set[str]:
        return {
            normalize_symbol(code).provider_symbol for code in self.favorite_symbols
        }

    def _attach_quote_row(
        self,
        stock_list: QListWidget,
        item: QListWidgetItem,
        text: str,
        symbol: str,
        color: str,
        favorite: bool,
        *,
        compact: bool = False,
    ) -> None:
        row = QuoteRowWidget(
            text,
            symbol,
            color,
            favorite,
            compact=compact,
            theme=self.current_theme,
        )
        row.quote_requested.connect(
            lambda raw_symbol, target_list=stock_list, target_item=item: self._open_quote_row(
                target_list, target_item, raw_symbol
            )
        )
        row.favorite_toggled.connect(self.toggle_favorite)
        stock_list.setItemWidget(item, row)

    def _open_quote_row(
        self,
        stock_list: QListWidget,
        item: QListWidgetItem,
        raw_symbol: str,
    ) -> None:
        stock_list.setCurrentItem(item)
        self.fetch_symbol(raw_symbol)

    @Slot(str)
    def toggle_favorite(self, raw_symbol: str) -> None:
        try:
            symbol = normalize_symbol(raw_symbol)
        except SymbolError as exc:
            self.status_label.setText(str(exc))
            return

        favorite_keys = self._favorite_keys()
        if symbol.provider_symbol in favorite_keys:
            self.favorite_symbols = [
                code
                for code in self.favorite_symbols
                if normalize_symbol(code).provider_symbol != symbol.provider_symbol
            ]
            message = f"已取消收藏：{symbol.display_code}"
        else:
            self.favorite_symbols.append(symbol.code)
            message = f"已收藏：{symbol.display_code}，桌宠每 5 秒刷新"

        self.settings.setValue("favorites", self.favorite_symbols)
        self.settings.sync()
        selected_symbol = self._current_input_symbol()
        self._refresh_watchlist_lists()
        if self.market_tabs.currentIndex() == 3:
            self._select_first_current_tab_item()
        else:
            self._select_symbol_in_current_list(selected_symbol)
        self._update_monitor_label()
        self.status_label.setText(message)
        self.favorites_changed.emit(list(self.favorite_symbols))

    def update_watchlist_quotes(self, quotes: list[Quote]) -> None:
        selected_symbol = self._current_input_symbol()
        for quote in quotes:
            self._quote_cache[quote.symbol.provider_symbol] = quote
        self._refresh_watchlist_lists()
        self._select_symbol_in_current_list(selected_symbol)
        self._update_market_summary()
        if selected_symbol:
            try:
                selected_key = normalize_symbol(selected_symbol).provider_symbol
            except SymbolError:
                selected_key = ""
            selected_quote = self._quote_cache.get(selected_key)
            if selected_quote is not None:
                self._display_quote(selected_quote)

    @Slot()
    def save_current_symbol(self) -> None:
        raw_symbol = self._current_input_symbol()
        try:
            symbol = normalize_symbol(raw_symbol)
            watchlist = normalize_watchlist([*self.watchlist, symbol.code])
        except SymbolError as exc:
            self.status_label.setText(str(exc))
            return
        if watchlist == self.watchlist:
            self.status_label.setText(f"{symbol.code} 已在自选列表中。")
            return
        self._save_watchlist_config(
            watchlist,
            self.alert_threshold,
            self.interval_seconds,
            self.alerts_enabled,
        )
        self.status_label.setText(f"已保存股票 ID：{symbol.code}")

    @Slot()
    def manage_watchlist(self) -> None:
        dialog = WatchlistDialog(
            self.watchlist,
            self.alert_threshold,
            self.interval_seconds,
            self.alerts_enabled,
            provider=self.provider,
            theme=self.current_theme,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.saved_config is None:
            return
        self._save_watchlist_config(*dialog.saved_config)
        self.status_label.setText("自选股和提醒设置已保存。")

    @Slot()
    def open_settings(self) -> None:
        SettingsDialog(
            self.settings,
            self.provider,
            theme=self.current_theme,
            parent=self,
        ).exec()

    def _save_watchlist_config(
        self,
        watchlist: list[str],
        threshold: float,
        interval_seconds: int,
        alerts_enabled: bool,
    ) -> None:
        self.watchlist = watchlist
        self.alert_threshold = threshold
        self.interval_seconds = interval_seconds
        self.alerts_enabled = alerts_enabled
        self.settings.setValue("watchlist", watchlist)
        self.settings.setValue("alert_threshold", threshold)
        self.settings.setValue("alert_interval_seconds", interval_seconds)
        self.settings.setValue("alerts_enabled", alerts_enabled)
        self.settings.sync()
        self._refresh_watchlist_lists()
        self._update_monitor_label()
        self.watchlist_changed.emit(watchlist, threshold, interval_seconds, alerts_enabled)

    def _update_monitor_label(self) -> None:
        state = "提醒开" if self.alerts_enabled else "提醒关"
        self.monitor_label.setText(f"{len(self.watchlist)}只 · ★{len(self.favorite_symbols)}")
        self.monitor_label.setToolTip(
            f"自选 {len(self.watchlist)} 只 · 收藏 {len(self.favorite_symbols)} 只 · "
            f"提醒阈值 ±{self.alert_threshold:.1f}% · {state} · 每 {self.interval_seconds} 秒检查 · "
            "收藏每 5 秒刷新 · 行情卡打开时当前页每 10 秒刷新"
        )

    @Slot(int)
    def set_panel_opacity(self, value: int) -> None:
        self.panel_opacity = max(90, min(100, int(value)))
        if self.opacity_slider.value() != self.panel_opacity:
            self.opacity_slider.setValue(self.panel_opacity)
        self.opacity_value_label.setText(f"{self.panel_opacity}%")
        self.setWindowOpacity(self.panel_opacity / 100.0)
        self.settings.setValue("panel_opacity", self.panel_opacity)
        self.settings.sync()

    @Slot()
    def fetch_quote(self) -> None:
        symbol = self._current_input_symbol()
        if not symbol:
            self._show_error("请输入股票代码。")
            return

        try:
            symbol_key = normalize_symbol(symbol).provider_symbol
        except SymbolError as exc:
            self._show_error(str(exc))
            return

        cached_quote = self._quote_cache.get(symbol_key)
        if cached_quote is not None:
            self._display_quote(cached_quote)

        if self._active_task is not None:
            self._pending_fetch_symbol = symbol
            self.status_label.setText(
                "已显示最近行情，正在等待更新…"
                if cached_quote is not None
                else "已记录当前选择，正在等待行情请求…"
            )
            return

        self._start_quote_fetch(symbol, cached_quote is not None)

    def _start_quote_fetch(self, symbol: str, has_cached_quote: bool = False) -> None:
        self.status_label.setText(
            "已显示最近行情，正在后台更新…"
            if has_cached_quote
            else "正在请求最新可用行情…"
        )
        self.loading_changed.emit(True)

        task = FetchTask(self.provider, symbol)
        task.signals.finished.connect(
            lambda quote, active_task=task: self._on_quote(active_task, quote)
        )
        task.signals.failed.connect(
            lambda message, active_task=task: self._on_fetch_error(active_task, message)
        )
        self._active_task = task
        self.thread_pool.start(task)

    def _on_quote(self, task: FetchTask, quote: Quote) -> None:
        if task is not self._active_task:
            return
        self._active_task = None
        self.update_watchlist_quotes([quote])
        self._continue_pending_fetch(task.symbol)

    def _on_fetch_error(self, task: FetchTask, message: str) -> None:
        if task is not self._active_task:
            return
        self._active_task = None
        pending = self._pending_fetch_symbol
        if pending:
            self._continue_pending_fetch(task.symbol)
            return

        try:
            key = normalize_symbol(task.symbol).provider_symbol
        except SymbolError:
            key = ""
        if key and key in self._quote_cache:
            self.status_label.setText(f"最新行情更新失败，继续显示最近数据\n{message}")
            self.loading_changed.emit(False)
            return
        self._show_error(message)

    def _continue_pending_fetch(self, completed_symbol: str) -> None:
        pending = self._pending_fetch_symbol
        self._pending_fetch_symbol = None
        if pending:
            try:
                pending_key = normalize_symbol(pending).provider_symbol
                completed_key = normalize_symbol(completed_symbol).provider_symbol
            except SymbolError:
                pending_key = pending
                completed_key = completed_symbol
            if pending_key != completed_key:
                cached = self._quote_cache.get(pending_key)
                self._start_quote_fetch(pending, cached is not None)
                return
        self.loading_changed.emit(False)

    def _display_quote(self, quote: Quote) -> None:
        self._set_symbol_input_display(quote.symbol.code, quote.name)
        currency = _currency_prefix(quote.symbol.currency)
        color = self._up_color() if quote.direction > 0 else self._down_color() if quote.direction < 0 else self._flat_color()
        arrow = "▲" if quote.direction > 0 else "▼" if quote.direction < 0 else "—"

        self.name_label.setText(f"{quote.name}  ·  {quote.symbol.display_code}  ·  {quote.symbol.market_label}")
        self.price_label.setText(f"{currency}{_price(quote.price)}")
        self.change_label.setText(f"{arrow} {quote.change_percent:+.2f}%")
        self.price_label.setStyleSheet(f"color: {color};")
        self.change_label.setStyleSheet(f"color: {color};")
        if quote.symbol.market in {"hk_index", "cn_index", "gold"}:
            unit = "美元/盎司" if quote.symbol.market == "gold" else "指数点"
            self.details_label.setText(
                f"今开 {_price(quote.open_price)}    最高 {_price(quote.high)}    最低 {_price(quote.low)}\n"
                f"昨收 {_price(quote.previous_close)}    涨跌 {quote.change:+.3f}    单位 {unit}"
            )
        else:
            turnover = "--" if quote.turnover_rate is None else f"{quote.turnover_rate:.2f}%"
            self.details_label.setText(
                f"今开 {_price(quote.open_price)}    最高 {_price(quote.high)}    最低 {_price(quote.low)}\n"
                f"昨收 {_price(quote.previous_close)}    涨跌 {quote.change:+.3f}\n"
                f"成交额 {_human_money(quote.amount)}    换手 {turnover}"
            )
        self.status_label.setText(f"{quote.source} · {quote.quote_time}\n行情可能延迟，仅供参考，不作为交易依据")
        self.quote_loaded.emit(quote)

    @Slot(str)
    def _show_error(self, message: str) -> None:
        self._finish_loading()
        self.name_label.setText("行情拉取失败")
        self.price_label.setText("--")
        self.price_label.setStyleSheet("")
        self.change_label.setText("")
        self.change_label.setStyleSheet("")
        self.status_label.setText(message)

    def _finish_loading(self) -> None:
        self._active_task = None
        self._pending_fetch_symbol = None
        self.loading_changed.emit(False)

    def place_near(self, pet: QWidget) -> None:
        screen = QApplication.screenAt(pet.frameGeometry().center()) or QApplication.primaryScreen()
        available = screen.availableGeometry()
        x = pet.x() - self.width() - 10
        if x < available.left():
            x = pet.x() + pet.width() + 10
        x = max(available.left(), min(x, available.right() - self.width() + 1))
        y = pet.y() + pet.height() - self.height()
        y = max(available.top(), min(y, available.bottom() - self.height() + 1))
        self.move(x, y)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._open_tab_refresh_timer.start()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        self._open_tab_refresh_timer.stop()
        super().hideEvent(event)


class StockPetWidget(QWidget):
    quit_requested = Signal()
    alert_requested = Signal(str, str)

    def __init__(self, settings: QSettings | None = None) -> None:
        super().__init__()
        self.settings = settings if settings is not None else QSettings()
        quote_source = str(self.settings.value("quote_source", "auto"))
        if quote_source not in HybridQuoteProvider.MODES:
            quote_source = "auto"
        self.settings.setValue("quote_source", quote_source)
        self.provider = HybridQuoteProvider(mode=quote_source)
        self.panel = QuotePanel(self.provider, self.settings)
        self.panel.quote_loaded.connect(self._apply_quote)
        self.panel.loading_changed.connect(self._loading_changed)
        self.panel.watchlist_changed.connect(self._apply_watchlist_config)
        self.panel.favorites_changed.connect(self._apply_favorites)
        self.panel.page_refresh_requested.connect(self.refresh_market_page)
        self.panel.periodic_page_refresh_requested.connect(self.refresh_market_page_silent)
        self.panel.theme_changed.connect(self._on_bubble_theme_changed)
        self.watchlist = list(self.panel.watchlist)
        self.favorite_symbols = list(self.panel.favorite_symbols)
        self.alert_threshold = self.panel.alert_threshold
        self.interval_seconds = self.panel.interval_seconds
        self.alerts_enabled = self.panel.alerts_enabled
        self._watch_task: WatchlistTask | None = None
        self._watch_manual = False
        self._watch_animate = False
        self._pending_page_symbols: list[str] | None = None
        self._page_refresh_initialized_keys: set[str] = set()
        (
            self._alert_states,
            self._alert_last_percent,
            self._alert_quote_dates,
        ) = self._load_alert_cache()
        self._watch_timer = QTimer(self)
        self._watch_timer.timeout.connect(self.scan_watchlist)
        self._configure_watch_timer()
        self._favorite_task: WatchlistTask | None = None
        self._favorite_refresh_pending = False
        self._favorite_refresh_initialized_keys: set[str] = set()
        self._favorite_quotes: list[Quote] = []
        self._favorite_index = 0
        self._favorite_refresh_timer = QTimer(self)
        self._favorite_refresh_timer.timeout.connect(self.refresh_favorites)
        self._favorite_carousel_timer = QTimer(self)
        self._favorite_carousel_timer.timeout.connect(self._show_next_favorite)
        self._bubble_showing_favorites = False

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(300, 234)

        self.bubble = QLabel("", self)
        self.bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.bubble.setWordWrap(True)
        self.bubble.setTextFormat(Qt.TextFormat.RichText)
        self.bubble.setGeometry(6, 4, 288, 64)
        self.bubble.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._bubble_direction = 0
        self._apply_bubble_theme(self.panel.current_theme)

        self.pet_label = QLabel(self)
        self.pet_label.setAlignment(Qt.AlignCenter)
        self._pet_image_x = 72
        self._pet_image_y = 72
        self.pet_label.setGeometry(self._pet_image_x, self._pet_image_y, 156, 150)
        self.pet_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.current_skin = "default"
        self._skin_sheet: QPixmap | None = None
        self._skin_animation = "idle"
        self._skin_animation_after_once: str | None = None
        self._skin_frame = 0
        self._refresh_activity_sources: set[str] = set()
        self.set_skin(str(self.settings.value("skin", "default")), persist=False)

        self._press_global: QPoint | None = None
        self._press_window: QPoint | None = None
        self._dragging = False
        self._animation_tick = 0
        self._animation = QTimer(self)
        self._animation.timeout.connect(self._animate)
        self._animation.start(50)

        self._configure_favorite_timers()
        self._show_favorite_prompt()

        saved_pos = self.settings.value("pet_position")
        if isinstance(saved_pos, QPoint):
            self.move(saved_pos)
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.right() - self.width() - 24, screen.bottom() - self.height() - 24)

        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setToolTip("单击显示或隐藏行情卡；拖动可移动；右键切换皮肤")
        if self.favorite_symbols:
            QTimer.singleShot(1_000, self.refresh_favorites)
        QTimer.singleShot(5_000, self.scan_watchlist)

    @Slot()
    def close_provider(self) -> None:
        self.provider.close()

    def _set_pet_image(self, filename: str) -> None:
        pixmap = QPixmap(str(asset_path(filename)))
        self.pet_label.setPixmap(pixmap.scaled(146, 146, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def set_skin(self, skin_key: str, persist: bool = True) -> None:
        if skin_key not in SKINS:
            skin_key = "default"
        display_name, filename, animated = SKINS[skin_key]
        self.current_skin = skin_key
        self._skin_animation = "refresh" if self._refresh_activity_sources else "idle"
        self._skin_animation_after_once = None
        self._skin_frame = 0
        if animated:
            sheet = QPixmap(str(asset_path(filename)))
            expected_width = SPRITE_CELL_WIDTH * 8
            expected_height = SPRITE_CELL_HEIGHT * 9
            if sheet.isNull() or sheet.width() != expected_width or sheet.height() != expected_height:
                self.current_skin = "default"
                self._skin_sheet = None
                self._set_pet_image(SKINS["default"][1])
                display_name = SKINS["default"][0]
            else:
                self._skin_sheet = sheet
                self._render_skin_frame()
        else:
            self._skin_sheet = None
            self._set_pet_image(filename)
        if persist:
            self.settings.setValue("skin", self.current_skin)
            self.settings.sync()
            if self._favorite_quotes:
                current_page = (self._favorite_index - 1) % self._favorite_page_count()
                self._render_favorite_page(current_page)
            else:
                self._show_favorite_prompt()

    def _render_skin_frame(self) -> None:
        if self._skin_sheet is None:
            return
        row, frame_count, _loops = SPRITE_ANIMATIONS[self._skin_animation]
        frame_index = min(self._skin_frame, frame_count - 1)
        frame = self._skin_sheet.copy(
            frame_index * SPRITE_CELL_WIDTH,
            row * SPRITE_CELL_HEIGHT,
            SPRITE_CELL_WIDTH,
            SPRITE_CELL_HEIGHT,
        )
        self.pet_label.setPixmap(
            frame.scaled(150, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def _play_skin_animation(
        self,
        animation: str,
        *,
        restart: bool = False,
        after_once: str | None = None,
    ) -> None:
        if animation not in SPRITE_ANIMATIONS:
            animation = "idle"
        if animation == self._skin_animation and not restart:
            if after_once is not None:
                self._skin_animation_after_once = after_once
            return
        self._skin_animation = animation
        self._skin_animation_after_once = after_once
        self._skin_frame = 0
        self._render_skin_frame()

    def _advance_skin_frame(self) -> None:
        if self._skin_sheet is None:
            return
        _row, frame_count, loops = SPRITE_ANIMATIONS[self._skin_animation]
        next_frame = self._skin_frame + 1
        if next_frame < frame_count:
            self._skin_frame = next_frame
            self._render_skin_frame()
            return
        if loops:
            self._skin_frame = 0
            self._render_skin_frame()
            return
        next_animation = self._skin_animation_after_once
        if next_animation is None:
            next_animation = "refresh" if self._refresh_activity_sources else "idle"
        self._play_skin_animation(next_animation, restart=True)

    def _set_refresh_activity(self, source: str, active: bool) -> None:
        if active:
            self._refresh_activity_sources.add(source)
        else:
            self._refresh_activity_sources.discard(source)
        target = "refresh" if self._refresh_activity_sources else "idle"
        if self._skin_animation == "click":
            self._skin_animation_after_once = target
        elif self._skin_animation not in {"drag_left", "drag_right"}:
            self._play_skin_animation(target)

    def _configure_favorite_timers(self) -> None:
        self._favorite_refresh_timer.stop()
        if self.favorite_symbols:
            self._favorite_refresh_timer.start(FAVORITE_REFRESH_INTERVAL_MS)

    def _resize_for_bubble(self, line_count: int) -> None:
        pet_global = self.mapToGlobal(self.pet_label.pos())
        if line_count <= 0:
            self.bubble.hide()
            self._pet_image_x = 8
            self._pet_image_y = 4
            self.setFixedSize(172, 158)
            self.pet_label.setGeometry(self._pet_image_x, self._pet_image_y, 156, 150)
            if self.isVisible():
                self.move(
                    pet_global.x() - self._pet_image_x,
                    pet_global.y() - self._pet_image_y,
                )
                self._clamp_to_screen()
            return

        bubble_height = 64 if line_count <= 2 else 20 + line_count * 18
        self.bubble.show()
        self._pet_image_x = 72
        self._pet_image_y = bubble_height + 8
        self.setFixedSize(300, self._pet_image_y + 158)
        self.bubble.setGeometry(6, 4, 288, bubble_height)
        self.pet_label.setGeometry(self._pet_image_x, self._pet_image_y, 156, 150)
        if self.isVisible():
            self.move(
                pet_global.x() - self._pet_image_x,
                pet_global.y() - self._pet_image_y,
            )
            self._clamp_to_screen()

    def _show_favorite_prompt(self) -> None:
        self._bubble_showing_favorites = False
        self._bubble_direction = 0
        if not self.favorite_symbols or not any(
            _is_visible_in_idle_bubble(symbol) for symbol in self.favorite_symbols
        ):
            self._hide_favorite_bubble()
            return
        self.bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._resize_for_bubble(2)
        self.bubble.setText("正在加载收藏行情…")
        self._apply_bubble_theme(self.panel.current_theme)

    def _hide_favorite_bubble(self) -> None:
        self._bubble_showing_favorites = False
        self._bubble_direction = 0
        self.bubble.clear()
        self._resize_for_bubble(0)

    def _visible_favorite_quotes(self) -> list[Quote]:
        return [
            quote
            for quote in self._favorite_quotes
            if _is_visible_in_idle_bubble(quote.symbol.provider_symbol)
        ]

    def _set_bubble_quote(self, quote: Quote, *, favorite: bool = False) -> None:
        arrow = "▲" if quote.direction > 0 else "▼" if quote.direction < 0 else "—"
        prefix = _currency_prefix(quote.symbol.currency)
        suffix = " 点" if quote.symbol.currency == "PTS" else ""
        marker = "★ " if favorite else ""
        self._bubble_showing_favorites = favorite
        self._bubble_direction = quote.direction
        self.bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._resize_for_bubble(2)
        self.bubble.setText(
            f"{escape(marker + quote.name)}&nbsp;&nbsp;{arrow} {quote.change_percent:+.2f}%<br>"
            f"{escape(quote.symbol.display_code)}&nbsp;&nbsp;{escape(prefix + _price(quote.price) + suffix)}"
        )
        self._apply_bubble_theme(self.panel.current_theme)

    def _favorite_page_count(self) -> int:
        return max(
            1,
            math.ceil(len(self._visible_favorite_quotes()) / FAVORITE_BUBBLE_PAGE_SIZE),
        )

    def _render_favorite_page(self, page_index: int) -> None:
        visible_quotes = self._visible_favorite_quotes()
        start = page_index * FAVORITE_BUBBLE_PAGE_SIZE
        quotes = visible_quotes[start : start + FAVORITE_BUBBLE_PAGE_SIZE]
        if not quotes:
            self._favorite_carousel_timer.stop()
            self._hide_favorite_bubble()
            return

        is_light = self.panel.current_theme == "beige"
        lines: list[str] = []
        for quote in quotes:
            arrow = "▲" if quote.direction > 0 else "▼" if quote.direction < 0 else "—"
            if quote.direction > 0:
                color = "#d63f45" if is_light else "#f05a5f"
            elif quote.direction < 0:
                color = "#248b57" if is_light else "#3dbc73"
            else:
                color = "#40516a" if is_light else "#c7d1de"
            name = quote.name if len(quote.name) <= 11 else f"{quote.name[:10]}…"
            prefix = _currency_prefix(quote.symbol.currency)
            suffix = "点" if quote.symbol.currency == "PTS" else ""
            lines.append(
                f'<div style="color:{color};white-space:nowrap">'
                f"★ {escape(name)}&nbsp;&nbsp;{arrow} {quote.change_percent:+.2f}%&nbsp;&nbsp;"
                f"{escape(prefix + _price(quote.price) + suffix)}</div>"
            )

        self._bubble_showing_favorites = True
        self._bubble_direction = 0
        self.bubble.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._resize_for_bubble(len(quotes))
        self.bubble.setText("".join(lines))
        self._apply_bubble_theme(self.panel.current_theme)

    @Slot()
    def _show_next_favorite(self) -> None:
        if not self._favorite_quotes:
            self._show_favorite_prompt()
            return
        if not self._visible_favorite_quotes():
            self._favorite_carousel_timer.stop()
            self._favorite_index = 0
            self._hide_favorite_bubble()
            return
        page_count = self._favorite_page_count()
        page_index = self._favorite_index % page_count
        self._render_favorite_page(page_index)
        self._favorite_index = (page_index + 1) % page_count

    @Slot(object)
    def _apply_favorites(self, favorites: list[str]) -> None:
        self.favorite_symbols = _normalize_favorites([str(item) for item in favorites])
        favorite_keys = {
            normalize_symbol(code).provider_symbol for code in self.favorite_symbols
        }
        self._favorite_refresh_initialized_keys.intersection_update(favorite_keys)
        self._favorite_quotes = [
            quote
            for quote in self._favorite_quotes
            if quote.symbol.provider_symbol in favorite_keys
        ]
        self._favorite_index = 0
        self._favorite_carousel_timer.stop()
        self._configure_favorite_timers()
        if not self.favorite_symbols:
            self._show_favorite_prompt()
            return
        if self._favorite_quotes:
            self._show_next_favorite()
        else:
            self._show_favorite_prompt()
        if self._favorite_task is not None:
            self._favorite_refresh_pending = True
        else:
            self.refresh_favorites()

    @Slot()
    def refresh_favorites(self) -> None:
        if not self.favorite_symbols:
            return
        if self._favorite_quotes:
            if self._visible_favorite_quotes():
                current_page = (self._favorite_index - 1) % self._favorite_page_count()
                self._render_favorite_page(current_page)
                self._favorite_index = (current_page + 1) % self._favorite_page_count()
            else:
                self._favorite_carousel_timer.stop()
                self._favorite_index = 0
                self._hide_favorite_bubble()
        if self._favorite_task is not None:
            return
        symbols = self._automatic_symbols_with_initialization(
            self.favorite_symbols,
            self._favorite_refresh_initialized_keys,
        )
        if not symbols:
            return
        task = WatchlistTask(self.provider, symbols)
        task.signals.finished.connect(self._on_favorite_result)
        self._favorite_task = task
        QThreadPool.globalInstance().start(task)

    @Slot(object)
    def _on_favorite_result(self, result: tuple[list[Quote], list[str]]) -> None:
        quotes, errors = result
        self._favorite_task = None
        favorite_keys = {
            normalize_symbol(code).provider_symbol for code in self.favorite_symbols
        }
        refreshed_quotes = [
            quote for quote in quotes if quote.symbol.provider_symbol in favorite_keys
        ]
        if refreshed_quotes:
            previous_by_key = {
                quote.symbol.provider_symbol: quote for quote in self._favorite_quotes
            }
            refreshed_by_key = {
                quote.symbol.provider_symbol: quote for quote in refreshed_quotes
            }
            self._favorite_quotes = [
                quote
                for code in self.favorite_symbols
                if (
                    quote := refreshed_by_key.get(
                        normalize_symbol(code).provider_symbol,
                        previous_by_key.get(normalize_symbol(code).provider_symbol),
                    )
                )
                is not None
            ]
            page_count = self._favorite_page_count()
            self._favorite_index %= page_count
            self.panel.update_watchlist_quotes(refreshed_quotes)
            if self._bubble_showing_favorites:
                current_page = (self._favorite_index - 1) % page_count
                self._render_favorite_page(current_page)
                self._favorite_index = (current_page + 1) % page_count
            else:
                self._show_next_favorite()
            if page_count > 1:
                if not self._favorite_carousel_timer.isActive():
                    self._favorite_carousel_timer.start(FAVORITE_BUBBLE_PAGE_INTERVAL_MS)
            else:
                self._favorite_carousel_timer.stop()
        elif not self._favorite_quotes:
            self._bubble_showing_favorites = False
            self._bubble_direction = 0
            self.bubble.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._resize_for_bubble(2)
            self.bubble.setText("收藏行情刷新失败<br>稍后自动重试")
            self._apply_bubble_theme(self.panel.current_theme)
        elif errors:
            self._show_next_favorite()

        if self._favorite_refresh_pending:
            self._favorite_refresh_pending = False
            QTimer.singleShot(0, self.refresh_favorites)

    @Slot(object)
    def _apply_quote(self, quote: Quote) -> None:
        self._page_refresh_initialized_keys.add(quote.symbol.provider_symbol)

    @Slot(str)
    def _apply_bubble_theme(self, theme: str) -> None:
        is_light = theme == "beige"
        if self._bubble_direction > 0:
            foreground = "#d63f45" if is_light else "#f05a5f"
        elif self._bubble_direction < 0:
            foreground = "#248b57" if is_light else "#3dbc73"
        else:
            foreground = "#40516a" if is_light else "#c7d1de"
        background = "rgba(247, 249, 252, 246)" if is_light else "rgba(10, 18, 28, 242)"
        border = "rgba(98, 125, 160, 110)" if is_light else "rgba(67, 91, 122, 190)"
        self.bubble.setStyleSheet(
            f"color: {foreground}; background: {background}; border: 1px solid {border};"
            "border-radius: 13px; font: 700 11px 'Noto Sans SC'; padding: 2px;"
        )

    @Slot(str)
    def _on_bubble_theme_changed(self, theme: str) -> None:
        self._apply_bubble_theme(theme)
        if self._bubble_showing_favorites and self._favorite_quotes:
            current_page = (self._favorite_index - 1) % self._favorite_page_count()
            self._render_favorite_page(current_page)

    @Slot(bool)
    def _loading_changed(self, loading: bool) -> None:
        self._set_refresh_activity("quote", loading)

    @Slot(object, float, int, bool)
    def _apply_watchlist_config(
        self,
        watchlist: list[str],
        threshold: float,
        interval_seconds: int,
        alerts_enabled: bool,
    ) -> None:
        self.watchlist = list(watchlist)
        self.alert_threshold = threshold
        self.interval_seconds = interval_seconds
        self.alerts_enabled = alerts_enabled
        active_keys = {
            normalize_symbol(code).provider_symbol
            for code in self.watchlist
        }
        self._alert_states = {
            key: direction
            for key, direction in self._alert_states.items()
            if key in active_keys
        }
        self._alert_last_percent = {
            key: percent
            for key, percent in self._alert_last_percent.items()
            if key in active_keys
        }
        self._alert_quote_dates = {
            key: quote_date
            for key, quote_date in self._alert_quote_dates.items()
            if key in active_keys
        }
        self._save_alert_cache()
        refresh_keys = {
            normalize_symbol(code).provider_symbol
            for code in [*self.watchlist, *INDEX_SYMBOLS]
        }
        self._page_refresh_initialized_keys.intersection_update(refresh_keys)
        self._configure_watch_timer()
        if alerts_enabled and watchlist:
            self.scan_watchlist()

    def _configure_watch_timer(self) -> None:
        self._watch_timer.stop()
        if self.alerts_enabled and self.watchlist:
            self._watch_timer.start(max(30, self.interval_seconds) * 1_000)

    @Slot(object)
    def refresh_market_page(self, symbols: list[str]) -> None:
        page_symbols = [str(symbol) for symbol in symbols]
        if not page_symbols:
            return
        if self._watch_task is not None:
            self._pending_page_symbols = page_symbols
            self.panel.status_label.setText("已有刷新任务进行中，稍后刷新当前页…")
            return
        self._start_quote_batch(page_symbols, manual=True, animate=True)

    @Slot(object)
    def refresh_market_page_silent(self, symbols: list[str]) -> None:
        page_symbols = [str(symbol) for symbol in symbols]
        if not page_symbols or self._watch_task is not None:
            return
        self._start_quote_batch(page_symbols, manual=False, animate=False)

    def scan_watchlist(self) -> None:
        if self._watch_task is not None:
            return
        symbols = [*self.watchlist, *INDEX_SYMBOLS]
        self._start_quote_batch(symbols, manual=False, animate=False)

    def _start_quote_batch(self, symbols: list[str], manual: bool, animate: bool) -> None:
        request_symbols = list(dict.fromkeys(str(symbol) for symbol in symbols))
        if not manual:
            request_symbols = self._automatic_symbols_with_initialization(
                request_symbols,
                self._page_refresh_initialized_keys,
            )
        if not request_symbols:
            return
        task = WatchlistTask(self.provider, request_symbols)
        task.signals.finished.connect(self._on_watchlist_result)
        self._watch_task = task
        self._watch_manual = manual
        self._watch_animate = animate
        if animate:
            self._set_refresh_activity("page", True)
        if manual:
            self.panel.status_label.setText(f"正在刷新当前页的 {len(request_symbols)} 项行情…")
        QThreadPool.globalInstance().start(task)

    @staticmethod
    def _automatic_symbols_with_initialization(
        symbols: list[str] | tuple[str, ...],
        initialized_keys: set[str],
    ) -> list[str]:
        request_symbols: list[str] = []
        for raw_symbol in dict.fromkeys(str(symbol) for symbol in symbols):
            try:
                key = normalize_symbol(raw_symbol).provider_symbol
            except SymbolError:
                continue
            if key in initialized_keys and not _is_open_for_automatic_refresh(raw_symbol):
                continue
            initialized_keys.add(key)
            request_symbols.append(raw_symbol)
        return request_symbols

    @Slot(object)
    def _on_watchlist_result(self, result: tuple[list[Quote], list[str]]) -> None:
        quotes, errors = result
        manual = self._watch_manual
        animate = self._watch_animate
        self._watch_task = None
        self._watch_manual = False
        self._watch_animate = False
        if animate:
            self._set_refresh_activity("page", False)
        self._page_refresh_initialized_keys.update(
            quote.symbol.provider_symbol for quote in quotes
        )
        self.panel.update_watchlist_quotes(quotes)

        if manual:
            message = f"当前页刷新完成：成功 {len(quotes)} 项"
            if errors:
                message += f"，失败 {len(errors)} 项"
            self.panel.status_label.setText(message)

        pending_symbols = self._pending_page_symbols
        self._pending_page_symbols = None
        if pending_symbols:
            QTimer.singleShot(
                0,
                lambda symbols=pending_symbols: self.refresh_market_page(symbols),
            )

        if not self.alerts_enabled:
            return
        watchlist_keys = {
            normalize_symbol(symbol).provider_symbol for symbol in self.watchlist
        }
        stock_quotes = [
            quote for quote in quotes if quote.symbol.provider_symbol in watchlist_keys
        ]
        for old_symbol in list(self._alert_states):
            if old_symbol not in watchlist_keys:
                self._alert_states.pop(old_symbol, None)

        for quote in stock_quotes:
            symbol_key = quote.symbol.provider_symbol
            self._process_alert_quote(symbol_key, quote)
        self._save_alert_cache()

    def _process_alert_quote(self, symbol_key: str, quote: Quote) -> None:
        quote_date = quote.quote_time.strip().split(" ", 1)[0]
        if len(quote_date) != 10 or quote_date[4:5] != "-" or quote_date[7:8] != "-":
            quote_date = datetime.now(MARKET_TIMEZONE).date().isoformat()

        if self._alert_quote_dates.get(symbol_key) != quote_date:
            self._alert_states.pop(symbol_key, None)
            self._alert_last_percent.pop(symbol_key, None)

        percent = quote.change_percent
        previous = self._alert_last_percent.get(symbol_key)
        active_direction = self._alert_states.get(symbol_key)
        rearm_threshold = max(0.0, self.alert_threshold - ALERT_REARM_MARGIN_PERCENT)

        if active_direction == 1 and percent <= rearm_threshold:
            self._alert_states.pop(symbol_key, None)
            active_direction = None
        elif active_direction == -1 and percent >= -rearm_threshold:
            self._alert_states.pop(symbol_key, None)
            active_direction = None

        direction = (
            1
            if percent >= self.alert_threshold
            else -1
            if percent <= -self.alert_threshold
            else 0
        )
        crossed_threshold = (
            previous is None
            or (direction == 1 and previous < self.alert_threshold)
            or (direction == -1 and previous > -self.alert_threshold)
        )
        if direction and active_direction != direction and crossed_threshold:
            self._alert_states[symbol_key] = direction
            action = "上涨" if direction > 0 else "下跌"
            self.alert_requested.emit(
                "股票桌宠提醒",
                f"{quote.name} {quote.symbol.display_code} {action} {abs(percent):.2f}%\n"
                f"首次突破提醒阈值 ±{self.alert_threshold:.1f}% · {quote.quote_time}",
            )

        self._alert_last_percent[symbol_key] = percent
        self._alert_quote_dates[symbol_key] = quote_date

    def _load_alert_cache(self) -> tuple[dict[str, int], dict[str, float], dict[str, str]]:
        raw = str(self.settings.value(ALERT_CACHE_SETTINGS_KEY, "") or "")
        try:
            payload = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            payload = {}

        states: dict[str, int] = {}
        percents: dict[str, float] = {}
        quote_dates: dict[str, str] = {}
        if not isinstance(payload, dict):
            return states, percents, quote_dates

        for key, snapshot in payload.items():
            if not isinstance(key, str) or not isinstance(snapshot, dict):
                continue
            try:
                percent = float(snapshot.get("percent"))
                direction = int(snapshot.get("direction", 0))
            except (TypeError, ValueError):
                continue
            quote_date = str(snapshot.get("quote_date", ""))
            if not math.isfinite(percent) or direction not in {-1, 0, 1}:
                continue
            percents[key] = percent
            quote_dates[key] = quote_date
            if direction:
                states[key] = direction
        return states, percents, quote_dates

    def _save_alert_cache(self) -> None:
        active_keys = {
            normalize_symbol(code).provider_symbol for code in self.watchlist
        }
        payload = {
            key: {
                "percent": self._alert_last_percent[key],
                "direction": self._alert_states.get(key, 0),
                "quote_date": self._alert_quote_dates.get(key, ""),
            }
            for key in active_keys
            if key in self._alert_last_percent
        }
        self.settings.setValue(
            ALERT_CACHE_SETTINGS_KEY,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        self.settings.sync()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._press_window = self.pos()
            self._dragging = False
            event.accept()
        elif event.button() == Qt.RightButton:
            self._show_menu(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._press_global is None or self._press_window is None:
            return
        delta = event.globalPosition().toPoint() - self._press_global
        if delta.manhattanLength() >= QApplication.startDragDistance():
            self._dragging = True
        if self._dragging:
            drag_animation = "drag_left" if delta.x() < 0 else "drag_right"
            self._play_skin_animation(drag_animation)
            self.move(self._press_window + delta)
            if self.panel.isVisible():
                self.panel.place_near(self)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            if self._dragging:
                self._clamp_to_screen()
                self.settings.setValue("pet_position", self.pos())
                next_animation = "refresh" if self._refresh_activity_sources else "idle"
                self._play_skin_animation(next_animation, restart=True)
            else:
                next_animation = "refresh" if self._refresh_activity_sources else "idle"
                self._play_skin_animation("click", restart=True, after_once=next_animation)
                self.toggle_panel()
            self._press_global = None
            self._press_window = None
            self._dragging = False
            event.accept()

    def _show_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        open_panel = QAction("打开行情卡", menu)
        skin_menu = QMenu("切换皮肤", menu)
        hide = QAction("隐藏桌宠", menu)
        quit_action = QAction("退出", menu)
        open_panel.triggered.connect(self.show_panel)
        hide.triggered.connect(self.hide_all)
        quit_action.triggered.connect(self.quit_requested)
        menu.addAction(open_panel)
        for skin_key, (display_name, _filename, _animated) in SKINS.items():
            skin_action = QAction(display_name, skin_menu)
            skin_action.setCheckable(True)
            skin_action.setChecked(skin_key == self.current_skin)
            skin_action.triggered.connect(
                lambda checked=False, key=skin_key: self.set_skin(key)
            )
            skin_menu.addAction(skin_action)
        menu.addMenu(skin_menu)
        menu.addSeparator()
        menu.addAction(hide)
        menu.addAction(quit_action)
        menu.exec(global_pos)

    @Slot()
    def show_panel(self) -> None:
        self.show_and_raise()
        self.panel.place_near(self)
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    @Slot()
    def toggle_panel(self) -> None:
        if self.panel.isVisible():
            self.panel.hide()
        else:
            self.show_panel()

    @Slot()
    def show_and_refresh(self) -> None:
        self.show_panel()
        self.panel.refresh_current_tab()

    @Slot()
    def show_and_raise(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    @Slot()
    def hide_all(self) -> None:
        self.panel.hide()
        self.hide()

    def _clamp_to_screen(self) -> None:
        screen = QApplication.screenAt(self.frameGeometry().center()) or QApplication.primaryScreen()
        available = screen.availableGeometry()
        x = max(available.left(), min(self.x(), available.right() - self.width() + 1))
        y = max(available.top(), min(self.y(), available.bottom() - self.height() + 1))
        self.move(x, y)

    def _animate(self) -> None:
        self._animation_tick += 1
        if self._skin_sheet is not None and self._animation_tick % SPRITE_FRAME_TICKS == 0:
            self._advance_skin_frame()
        offset = round(math.sin(self._animation_tick / 8.0) * 3) if self._skin_animation == "idle" else 0
        self.pet_label.move(self._pet_image_x, self._pet_image_y + offset)


def _price(value: float) -> str:
    if abs(value) >= 100:
        return f"{value:.2f}"
    if abs(value) >= 1:
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _currency_prefix(currency: str) -> str:
    return {"HKD": "HK$", "CNY": "¥", "USD": "US$", "PTS": ""}.get(currency, "")


def _human(value: float) -> str:
    absolute = abs(value)
    if absolute >= 100_000_000:
        return f"{value / 100_000_000:.2f}亿"
    if absolute >= 10_000:
        return f"{value / 10_000:.2f}万"
    return f"{value:.0f}"


def _human_money(value: float) -> str:
    return _human(value)
