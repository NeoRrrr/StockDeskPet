from __future__ import annotations

import math

from PySide6.QtCore import QObject, QPoint, QRunnable, QSettings, QSize, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QAction, QColor, QCursor, QMouseEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .models import Quote
from .quote_provider import TencentQuoteProvider
from .resources import asset_path
from .symbols import SymbolError, normalize_symbol, normalize_watchlist, partition_watchlist


UP_COLOR = "#ff5b5b"
DOWN_COLOR = "#22c55e"
FLAT_COLOR = "#e5e7eb"

SPRITE_CELL_WIDTH = 192
SPRITE_CELL_HEIGHT = 208
SPRITE_IDLE_FRAMES = 6
SKINS: dict[str, tuple[str, str, bool]] = {
    "default": ("默认牛", "ox_3d.png", False),
    "ella-wave": ("Ella Wave", "skins/ella-wave.webp", True),
    "endminguga": ("GUGUGAGA", "skins/endminguga.webp", True),
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
            quote = self.provider.fetch(self.symbol)
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


class WatchlistDialog(QDialog):
    def __init__(
        self,
        watchlist: list[str],
        threshold: float,
        interval_seconds: int,
        alerts_enabled: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.saved_config: tuple[list[str], float, int, bool] | None = None
        self.setWindowTitle("编辑自选股")
        self.setModal(True)
        self.setMinimumSize(430, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        title = QLabel("编辑并保存股票 ID")
        title.setObjectName("dialogTitle")
        help_label = QLabel(
            "每行一个，最多 20 只。支持 00700、HK00700、600519、SH600519、000001。"
        )
        help_label.setWordWrap(True)
        help_label.setObjectName("dialogHelp")
        self.stock_ids = QPlainTextEdit("\n".join(watchlist))
        self.stock_ids.setPlaceholderText("00700\n600519\n000001")
        self.stock_ids.setMinimumHeight(180)
        layout.addWidget(title)
        layout.addWidget(help_label)
        layout.addWidget(self.stock_ids)

        form = QFormLayout()
        self.alerts_enabled = QCheckBox("启用系统通知")
        self.alerts_enabled.setChecked(alerts_enabled)
        self.threshold = QDoubleSpinBox()
        self.threshold.setRange(0.5, 20.0)
        self.threshold.setDecimals(1)
        self.threshold.setSingleStep(0.5)
        self.threshold.setSuffix(" %")
        self.threshold.setValue(threshold)
        self.interval = QSpinBox()
        self.interval.setRange(30, 3600)
        self.interval.setSingleStep(30)
        self.interval.setSuffix(" 秒")
        self.interval.setValue(interval_seconds)
        form.addRow("提醒", self.alerts_enabled)
        form.addRow("涨跌幅达到", self.threshold)
        form.addRow("检查间隔", self.interval)
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
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setStyleSheet(
            """
            QDialog { background: #151a24; color: #e5e7eb; }
            QLabel, QCheckBox { color: #e5e7eb; font-family: "Microsoft YaHei UI"; }
            QLabel#dialogTitle { color: white; font-size: 18px; font-weight: 700; }
            QLabel#dialogHelp { color: #94a3b8; font-size: 12px; }
            QLabel#dialogError { color: #ff6b6b; font-size: 12px; }
            QPlainTextEdit, QDoubleSpinBox, QSpinBox {
                color: #f8fafc; background: #252b3a; border: 1px solid #394156;
                border-radius: 8px; padding: 7px; font: 13px "Microsoft YaHei UI";
            }
            QPushButton {
                color: white; background: #34405a; border: none; border-radius: 8px;
                padding: 7px 18px; font: 600 12px "Microsoft YaHei UI";
            }
            QPushButton:hover { background: #4776e6; }
            """
        )

    @Slot()
    def _validate_and_accept(self) -> None:
        values = self.stock_ids.toPlainText().splitlines()
        try:
            watchlist = normalize_watchlist(values)
        except SymbolError as exc:
            self.error_label.setText(str(exc))
            return
        self.saved_config = (
            watchlist,
            float(self.threshold.value()),
            int(self.interval.value()),
            bool(self.alerts_enabled.isChecked()),
        )
        self.accept()


class QuotePanel(QWidget):
    quote_loaded = Signal(object)
    loading_changed = Signal(bool)
    watchlist_changed = Signal(object, float, int, bool)
    page_refresh_requested = Signal(object)

    def __init__(self, provider: TencentQuoteProvider, settings: QSettings) -> None:
        super().__init__()
        self.provider = provider
        self.settings = settings
        self.thread_pool = QThreadPool.globalInstance()
        self._active_task: FetchTask | None = None
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
        title = QLabel("股票桌宠 · 行情卡")
        title.setObjectName("title")
        manage_button = QPushButton("编辑自选")
        manage_button.setObjectName("headerButton")
        manage_button.clicked.connect(self.manage_watchlist)
        close_button = QPushButton("×")
        close_button.setObjectName("closeButton")
        close_button.setFixedSize(28, 28)
        close_button.clicked.connect(self.hide)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(manage_button)
        header.addWidget(close_button)
        layout.addLayout(header)

        search = QHBoxLayout()
        self.symbol_input = QComboBox()
        self.symbol_input.setEditable(True)
        self.symbol_input.addItems(self.watchlist)
        self.symbol_input.setCurrentText(last_symbol)
        self.symbol_input.lineEdit().setPlaceholderText("00700 / 600519 / HSI / GOLD")
        self.symbol_input.lineEdit().returnPressed.connect(self.fetch_quote)
        self.symbol_input.activated.connect(lambda index: self.fetch_quote())
        self.fetch_button = QPushButton("拉取行情")
        self.fetch_button.setObjectName("fetchButton")
        self.fetch_button.clicked.connect(self.fetch_quote)
        search.addWidget(self.symbol_input, 1)
        search.addWidget(self.fetch_button)
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
        watchlist_row.addWidget(save_button)
        watchlist_row.addWidget(refresh_page_button)
        watchlist_row.addStretch()
        watchlist_row.addWidget(self.monitor_label)
        layout.addLayout(watchlist_row)

        self.market_tabs = QTabWidget()
        self.market_tabs.setObjectName("marketTabs")
        self.market_tabs.setMinimumHeight(282)
        self.a_share_list = QListWidget()
        self.hk_share_list = QListWidget()
        self.index_list = QListWidget()
        for stock_list in (self.a_share_list, self.hk_share_list, self.index_list):
            stock_list.setObjectName("stockList")
            stock_list.setAlternatingRowColors(False)
            stock_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            stock_list.itemClicked.connect(self._on_watchlist_item_clicked)
        self.index_list.setObjectName("indexList")
        self.market_tabs.addTab(self.a_share_list, "大A")
        self.market_tabs.addTab(self.hk_share_list, "港股")
        self.market_tabs.addTab(self.index_list, "指数")
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

        self.details_label = QLabel("今开 --    最高 --    最低 --\n昨收 --    成交额 --")
        self.details_label.setObjectName("details")
        layout.addWidget(self.details_label)
        layout.addStretch()

        self.market_summary_label = QLabel("")
        self.market_summary_label.setObjectName("marketSummary")
        self.market_summary_label.setTextFormat(Qt.TextFormat.RichText)
        self.market_summary_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.market_summary_label.setMinimumHeight(35)
        self.market_summary_label.setToolTip("当前 Tab 的关键市场指数，随当前页刷新")
        layout.addWidget(self.market_summary_label)

        self.status_label = QLabel("数据源：腾讯行情公共网页接口；行情可能延迟，仅供参考")
        self.status_label.setObjectName("status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.setStyleSheet(
            """
            QFrame#card {
                background: rgba(18, 22, 32, 244);
                border: 1px solid rgba(255, 255, 255, 32);
                border-radius: 18px;
            }
            QLabel { color: #e5e7eb; font-family: "Microsoft YaHei UI"; }
            QLabel#title { color: #ffffff; font-size: 16px; font-weight: 700; }
            QLabel#stockName { color: #cbd5e1; font-size: 14px; font-weight: 600; }
            QLabel#price { color: #ffffff; font-size: 30px; font-weight: 800; }
            QLabel#change { font-size: 17px; font-weight: 700; }
            QLabel#details { color: #cbd5e1; font-size: 13px; line-height: 1.5; }
            QLabel#marketSummary {
                color: #cbd5e1; background: #171e2a;
                border-top: 1px solid #354158; border-bottom: 1px solid #354158;
                padding: 6px 9px; font-size: 10px;
            }
            QLabel#status { color: #7f8ca3; font-size: 11px; }
            QLabel#monitor { color: #7f8ca3; font-size: 10px; }
            QTabWidget#marketTabs::pane {
                background: #1d2330; border: 1px solid #343d50;
                border-radius: 9px; top: -1px;
            }
            QTabBar::tab {
                color: #8f9db4; background: #202735; border: 1px solid #343d50;
                padding: 7px 22px; min-width: 72px;
                font: 600 12px "Microsoft YaHei UI";
            }
            QTabBar::tab:first { border-top-left-radius: 8px; }
            QTabBar::tab:last { border-top-right-radius: 8px; }
            QTabBar::tab:selected { color: white; background: #4776e6; }
            QListWidget#stockList, QListWidget#indexList {
                color: #f1f5f9; background: #252e3e; border: none;
                outline: none; padding: 0; font: 12px "Microsoft YaHei UI";
            }
            QListWidget#stockList::item {
                border: none; border-bottom: 1px solid #46546d;
                border-radius: 0; margin: 0; padding: 9px 12px;
            }
            QListWidget#stockList::item:hover { background: #40506c; }
            QListWidget#stockList::item:selected { background: #3f5f9b; color: white; }
            QListWidget#indexList { font-size: 11px; }
            QListWidget#indexList::item {
                border: none; border-bottom: 1px solid #46546d;
                border-radius: 0; margin: 0; padding: 3px 10px;
            }
            QListWidget#indexList::item:disabled {
                background: #171e2a; border: none;
                margin: 0; padding: 3px 10px;
            }
            QListWidget#indexList::item:hover { background: #40506c; }
            QListWidget#indexList::item:selected { background: #3f5f9b; color: white; }
            QComboBox, QLineEdit {
                color: #f8fafc; background: #252b3a; border: 1px solid #394156;
                border-radius: 9px; padding: 8px 10px; font-size: 14px;
            }
            QComboBox:focus, QLineEdit:focus { border-color: #5c8dff; }
            QComboBox QAbstractItemView {
                color: #f8fafc; background: #252b3a; border: 1px solid #394156;
                selection-background-color: #4776e6; padding: 5px;
            }
            QPushButton { font-family: "Microsoft YaHei UI"; cursor: pointer; }
            QPushButton#fetchButton {
                color: white; background: #4776e6; border: none; border-radius: 9px;
                padding: 8px 13px; font-weight: 700;
            }
            QPushButton#fetchButton:hover { background: #5c8dff; }
            QPushButton#fetchButton:disabled { background: #44506a; color: #9ca3af; }
            QPushButton#secondaryButton, QPushButton#headerButton {
                color: #aebbd0; background: #252b3a; border: 1px solid #343d50;
                border-radius: 8px; padding: 5px 7px; font-size: 11px;
            }
            QPushButton#secondaryButton:hover, QPushButton#headerButton:hover {
                color: white; border-color: #5c8dff;
            }
            QPushButton#closeButton {
                color: #94a3b8; background: transparent; border: none; font-size: 22px;
            }
            QPushButton#closeButton:hover { color: white; background: #2b3344; border-radius: 8px; }
            """
        )
        self.market_tabs.currentChanged.connect(self._on_market_tab_changed)
        self._refresh_watchlist_lists()
        self._update_market_summary()
        self._select_first_current_tab_item()
        self._update_monitor_label()

    def fetch_symbol(self, symbol: str) -> None:
        self.symbol_input.setCurrentText(symbol)
        self.fetch_quote()

    def _on_watchlist_item_clicked(self, item: QListWidgetItem) -> None:
        symbol = item.data(Qt.ItemDataRole.UserRole)
        if symbol:
            self.fetch_symbol(str(symbol))

    def _on_market_tab_changed(self, index: int) -> None:
        self._update_market_summary()
        self._select_first_current_tab_item()
        self.refresh_current_tab()

    def _current_tab_list_symbols(self) -> list[str]:
        a_shares, hk_shares = partition_watchlist(self.watchlist)
        return {
            0: a_shares,
            1: hk_shares,
            2: list(INDEX_SYMBOLS),
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

    def _clear_quote_display(self) -> None:
        self.symbol_input.setCurrentText("")
        self.name_label.setText("未选择行情")
        self.price_label.setText("--")
        self.change_label.setText("")
        self.details_label.setText("今开 --    最高 --    最低 --\n昨收 --    成交额 --")
        self.status_label.setText("点击自选列表或输入代码拉取行情；行情可能延迟，仅供参考")

    def _current_stock_list(self) -> QListWidget | None:
        return {
            0: self.a_share_list,
            1: self.hk_share_list,
            2: self.index_list,
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
            self.symbol_input.setCurrentText(str(symbol))
            normalized = normalize_symbol(str(symbol))
            quote = self._quote_cache.get(normalized.provider_symbol)
            if quote is not None:
                self._display_quote(quote)
            else:
                self.name_label.setText(f"{normalized.display_code} · 等待刷新")
                self.price_label.setText("--")
                self.change_label.setText("")
                self.details_label.setText("今开 --    最高 --    最低 --\n昨收 --    成交额 --")
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
        parts: list[str] = []
        summary_items = TAB_MARKET_SUMMARIES.get(self.market_tabs.currentIndex(), ())
        self.market_summary_label.setVisible(bool(summary_items))
        for label, raw_symbol in summary_items:
            symbol = normalize_symbol(raw_symbol)
            quote = self._quote_cache.get(symbol.provider_symbol)
            if quote is None:
                value = "--"
                color = "#9aa8bc"
            else:
                arrow = "▲" if quote.direction > 0 else "▼" if quote.direction < 0 else "—"
                color = UP_COLOR if quote.direction > 0 else DOWN_COLOR if quote.direction < 0 else FLAT_COLOR
                value = (
                    f"{_currency_prefix(quote.symbol.currency)}{_price(quote.price)} "
                    f"{arrow}{quote.change_percent:+.2f}%"
                )
            parts.append(
                f'<span style="color:#9fb0c9">{label}</span> '
                f'<span style="color:{color};font-weight:600">{value}</span>'
            )
        self.market_summary_label.setText("&nbsp;&nbsp;&nbsp;".join(parts))

    def _refresh_watchlist_lists(self) -> None:
        a_shares, hk_shares = partition_watchlist(self.watchlist)
        self._populate_market_list(self.a_share_list, a_shares, "暂无大A自选，点击“编辑自选”添加")
        self._populate_market_list(self.hk_share_list, hk_shares, "暂无港股自选，点击“编辑自选”添加")
        self._populate_index_list()
        self.market_tabs.setTabText(0, f"大A ({len(a_shares)})")
        self.market_tabs.setTabText(1, f"港股 ({len(hk_shares)})")
        self.market_tabs.setTabText(2, f"指数 ({len(INDEX_SYMBOLS)})")

        active_keys = {
            normalize_symbol(code).provider_symbol
            for code in [*self.watchlist, *INDEX_SYMBOLS]
        }
        for symbol_key in list(self._quote_cache):
            if symbol_key not in active_keys:
                self._quote_cache.pop(symbol_key, None)

    def _populate_index_list(self) -> None:
        self.index_list.clear()
        for group_name, symbols in INDEX_GROUPS:
            header = QListWidgetItem(group_name)
            header.setForeground(QColor("#8fa2c2"))
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setSizeHint(QSize(0, 21))
            self.index_list.addItem(header)
            for code in symbols:
                symbol = normalize_symbol(code)
                quote = self._quote_cache.get(symbol.provider_symbol)
                if quote is None:
                    text = f"  {symbol.display_code}  ·  {symbol.market_label}"
                    color = FLAT_COLOR
                else:
                    arrow = "▲" if quote.direction > 0 else "▼" if quote.direction < 0 else "—"
                    prefix = _currency_prefix(quote.symbol.currency)
                    text = (
                        f"  {quote.name}  ·  {quote.symbol.display_code}    "
                        f"{arrow} {quote.change_percent:+.2f}%    {prefix}{_price(quote.price)}"
                    )
                    color = (
                        UP_COLOR
                        if quote.direction > 0
                        else DOWN_COLOR if quote.direction < 0 else FLAT_COLOR
                    )
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, code)
                item.setForeground(QColor(color))
                item.setSizeHint(QSize(0, 21))
                item.setToolTip("点击拉取该指数的详细行情")
                self.index_list.addItem(item)

    def _populate_market_list(
        self,
        stock_list: QListWidget,
        symbols: list[str],
        empty_message: str,
    ) -> None:
        stock_list.clear()
        if not symbols:
            empty_item = QListWidgetItem(empty_message)
            empty_item.setForeground(QColor("#6f7c91"))
            empty_item.setFlags(Qt.ItemFlag.NoItemFlags)
            stock_list.addItem(empty_item)
            return

        for code in symbols:
            symbol = normalize_symbol(code)
            quote = self._quote_cache.get(symbol.provider_symbol)
            if quote is None:
                text = f"{symbol.display_code}  ·  {symbol.market_label}"
                color = FLAT_COLOR
            else:
                arrow = "▲" if quote.direction > 0 else "▼" if quote.direction < 0 else "—"
                currency = _currency_prefix(quote.symbol.currency)
                text = (
                    f"{quote.name}  ·  {quote.symbol.display_code}    "
                    f"{arrow} {quote.change_percent:+.2f}%    {currency}{_price(quote.price)}"
                )
                color = UP_COLOR if quote.direction > 0 else DOWN_COLOR if quote.direction < 0 else FLAT_COLOR
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, code)
            item.setForeground(QColor(color))
            item.setToolTip("点击拉取这只股票的详细行情")
            stock_list.addItem(item)

    def update_watchlist_quotes(self, quotes: list[Quote]) -> None:
        selected_symbol = self.symbol_input.currentText().strip()
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
        raw_symbol = self.symbol_input.currentText().strip()
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
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or dialog.saved_config is None:
            return
        self._save_watchlist_config(*dialog.saved_config)
        self.status_label.setText("自选股和提醒设置已保存。")

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
        self._refresh_watchlist_choices()
        self._refresh_watchlist_lists()
        self._update_monitor_label()
        self.watchlist_changed.emit(watchlist, threshold, interval_seconds, alerts_enabled)

    def _refresh_watchlist_choices(self) -> None:
        current = self.symbol_input.currentText().strip()
        self.symbol_input.blockSignals(True)
        self.symbol_input.clear()
        self.symbol_input.addItems(self.watchlist)
        self.symbol_input.setCurrentText(current)
        self.symbol_input.blockSignals(False)

    def _update_monitor_label(self) -> None:
        state = "提醒开" if self.alerts_enabled else "提醒关"
        self.monitor_label.setText(f"{len(self.watchlist)}只 · ±{self.alert_threshold:.1f}% · {state}")

    @Slot()
    def fetch_quote(self) -> None:
        if self._active_task is not None:
            return
        symbol = self.symbol_input.currentText().strip()
        if not symbol:
            self._show_error("请输入股票代码。")
            return

        self.fetch_button.setDisabled(True)
        self.fetch_button.setText("拉取中…")
        self.status_label.setText("正在请求最新可用行情…")
        self.loading_changed.emit(True)

        task = FetchTask(self.provider, symbol)
        task.signals.finished.connect(self._on_quote)
        task.signals.failed.connect(self._show_error)
        self._active_task = task
        self.thread_pool.start(task)

    @Slot(object)
    def _on_quote(self, quote: Quote) -> None:
        self._finish_loading()
        self.update_watchlist_quotes([quote])

    def _display_quote(self, quote: Quote) -> None:
        currency = _currency_prefix(quote.symbol.currency)
        color = UP_COLOR if quote.direction > 0 else DOWN_COLOR if quote.direction < 0 else FLAT_COLOR
        arrow = "▲" if quote.direction > 0 else "▼" if quote.direction < 0 else "—"

        self.name_label.setText(f"{quote.name}  ·  {quote.symbol.display_code}  ·  {quote.symbol.market_label}")
        self.price_label.setText(f"{currency}{_price(quote.price)}")
        self.change_label.setText(f"{arrow} {quote.change_percent:+.2f}%")
        self.change_label.setStyleSheet(f"color: {color};")
        if quote.symbol.market in {"hk_index", "cn_index", "gold"}:
            unit = "美元/盎司" if quote.symbol.market == "gold" else "指数点"
            self.details_label.setText(
                f"今开 {_price(quote.open_price)}    最高 {_price(quote.high)}    最低 {_price(quote.low)}\n"
                f"昨收 {_price(quote.previous_close)}    涨跌 {quote.change:+.3f}    单位 {unit}"
            )
        else:
            self.details_label.setText(
                f"今开 {_price(quote.open_price)}    最高 {_price(quote.high)}    最低 {_price(quote.low)}\n"
                f"昨收 {_price(quote.previous_close)}    涨跌 {quote.change:+.3f}    成交额 {_human_money(quote.amount)}"
            )
        self.status_label.setText(f"{quote.source} · {quote.quote_time}\n行情可能延迟，仅供参考，不作为交易依据")
        self.quote_loaded.emit(quote)

    @Slot(str)
    def _show_error(self, message: str) -> None:
        self._finish_loading()
        self.name_label.setText("行情拉取失败")
        self.price_label.setText("--")
        self.change_label.setText("")
        self.status_label.setText(message)

    def _finish_loading(self) -> None:
        self._active_task = None
        self.fetch_button.setDisabled(False)
        self.fetch_button.setText("拉取行情")
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


class StockPetWidget(QWidget):
    quit_requested = Signal()
    alert_requested = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings()
        self.provider = TencentQuoteProvider()
        self.panel = QuotePanel(self.provider, self.settings)
        self.panel.quote_loaded.connect(self._apply_quote)
        self.panel.loading_changed.connect(self._loading_changed)
        self.panel.watchlist_changed.connect(self._apply_watchlist_config)
        self.panel.page_refresh_requested.connect(self.refresh_market_page)
        self.watchlist = list(self.panel.watchlist)
        self.alert_threshold = self.panel.alert_threshold
        self.interval_seconds = self.panel.interval_seconds
        self.alerts_enabled = self.panel.alerts_enabled
        self._watch_task: WatchlistTask | None = None
        self._watch_manual = False
        self._pending_page_symbols: list[str] | None = None
        self._alert_states: dict[str, int] = {}
        self._watch_timer = QTimer(self)
        self._watch_timer.timeout.connect(self.scan_watchlist)
        self._configure_watch_timer()

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedSize(190, 210)

        self.bubble = QLabel("点我刷新行情", self)
        self.bubble.setAlignment(Qt.AlignCenter)
        self.bubble.setWordWrap(True)
        self.bubble.setGeometry(8, 4, 174, 48)
        self.bubble.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.bubble.setStyleSheet(
            "color: #f8fafc; background: rgba(17, 24, 39, 225);"
            "border: 1px solid rgba(255,255,255,35); border-radius: 13px;"
            "font: 600 12px 'Microsoft YaHei UI'; padding: 4px;"
        )

        self.pet_label = QLabel(self)
        self.pet_label.setAlignment(Qt.AlignCenter)
        self.pet_label.setGeometry(17, 52, 156, 150)
        self.pet_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.current_skin = "default"
        self._skin_sheet: QPixmap | None = None
        self._skin_frame = 0
        self.set_skin(str(self.settings.value("skin", "default")), persist=False)

        self._press_global: QPoint | None = None
        self._press_window: QPoint | None = None
        self._dragging = False
        self._animation_tick = 0
        self._animation = QTimer(self)
        self._animation.timeout.connect(self._animate)
        self._animation.start(50)

        saved_pos = self.settings.value("pet_position")
        if isinstance(saved_pos, QPoint):
            self.move(saved_pos)
        else:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(screen.right() - self.width() - 24, screen.bottom() - self.height() - 24)

        self.setCursor(QCursor(Qt.PointingHandCursor))
        self.setToolTip("单击刷新当前行情页；拖动可移动；右键切换皮肤")
        QTimer.singleShot(5_000, self.scan_watchlist)

    def _set_pet_image(self, filename: str) -> None:
        pixmap = QPixmap(str(asset_path(filename)))
        self.pet_label.setPixmap(pixmap.scaled(146, 146, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def set_skin(self, skin_key: str, persist: bool = True) -> None:
        if skin_key not in SKINS:
            skin_key = "default"
        display_name, filename, animated = SKINS[skin_key]
        self.current_skin = skin_key
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
            self.bubble.setText(f"已切换皮肤\n{display_name}")

    def _render_skin_frame(self) -> None:
        if self._skin_sheet is None:
            return
        frame = self._skin_sheet.copy(
            self._skin_frame * SPRITE_CELL_WIDTH,
            0,
            SPRITE_CELL_WIDTH,
            SPRITE_CELL_HEIGHT,
        )
        self.pet_label.setPixmap(
            frame.scaled(150, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    @Slot(object)
    def _apply_quote(self, quote: Quote) -> None:
        arrow = "▲" if quote.direction > 0 else "▼" if quote.direction < 0 else "—"
        color = UP_COLOR if quote.direction > 0 else DOWN_COLOR if quote.direction < 0 else FLAT_COLOR
        prefix = _currency_prefix(quote.symbol.currency)
        suffix = " 点" if quote.symbol.currency == "PTS" else ""
        self.bubble.setText(
            f"{quote.name}  {arrow} {quote.change_percent:+.2f}%\n"
            f"{prefix}{_price(quote.price)}{suffix}"
        )
        self.bubble.setStyleSheet(
            f"color: {color}; background: rgba(17, 24, 39, 232);"
            "border: 1px solid rgba(255,255,255,35); border-radius: 13px;"
            "font: 700 12px 'Microsoft YaHei UI'; padding: 4px;"
        )

    @Slot(bool)
    def _loading_changed(self, loading: bool) -> None:
        if loading:
            self.bubble.setText("正在拉取行情…")

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
        self._alert_states.clear()
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
        self._start_quote_batch(page_symbols, manual=True)

    def scan_watchlist(self) -> None:
        if self._watch_task is not None:
            return
        symbols = [*self.watchlist, *INDEX_SYMBOLS]
        self._start_quote_batch(symbols, manual=False)

    def _start_quote_batch(self, symbols: list[str], manual: bool) -> None:
        task = WatchlistTask(self.provider, symbols)
        task.signals.finished.connect(self._on_watchlist_result)
        self._watch_task = task
        self._watch_manual = manual
        if manual:
            self.panel.status_label.setText(f"正在刷新当前页的 {len(symbols)} 项行情…")
        QThreadPool.globalInstance().start(task)

    @Slot(object)
    def _on_watchlist_result(self, result: tuple[list[Quote], list[str]]) -> None:
        quotes, errors = result
        manual = self._watch_manual
        self._watch_task = None
        self._watch_manual = False
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
            if abs(quote.change_percent) < self.alert_threshold:
                self._alert_states.pop(symbol_key, None)
                continue
            direction = 1 if quote.change_percent > 0 else -1
            if self._alert_states.get(symbol_key) == direction:
                continue
            self._alert_states[symbol_key] = direction
            action = "上涨" if direction > 0 else "下跌"
            self.alert_requested.emit(
                "股票桌宠提醒",
                f"{quote.name} {quote.symbol.display_code} {action} {abs(quote.change_percent):.2f}%\n"
                f"达到提醒阈值 ±{self.alert_threshold:.1f}% · {quote.quote_time}",
            )

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
            self.move(self._press_window + delta)
            if self.panel.isVisible():
                self.panel.place_near(self)
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            if self._dragging:
                self._clamp_to_screen()
                self.settings.setValue("pet_position", self.pos())
            else:
                self.show_and_refresh()
            self._press_global = None
            self._press_window = None
            self._dragging = False
            event.accept()

    def _show_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)
        refresh = QAction("刷新当前页", menu)
        open_panel = QAction("打开行情卡", menu)
        skin_menu = QMenu("切换皮肤", menu)
        hide = QAction("隐藏桌宠", menu)
        quit_action = QAction("退出", menu)
        refresh.triggered.connect(self.show_and_refresh)
        open_panel.triggered.connect(self.show_panel)
        hide.triggered.connect(self.hide_all)
        quit_action.triggered.connect(self.quit_requested)
        menu.addAction(refresh)
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
        if self._skin_sheet is not None and self._animation_tick % 4 == 0:
            self._skin_frame = (self._skin_frame + 1) % SPRITE_IDLE_FRAMES
            self._render_skin_frame()
        offset = round(math.sin(self._animation_tick / 8.0) * 3)
        self.pet_label.move(17, 52 + offset)


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
