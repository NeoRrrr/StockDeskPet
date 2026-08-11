from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from stock_pet.models import Quote
from stock_pet.symbols import normalize_symbol
from stock_pet.ui import QuotePanel, SettingsDialog, StockPetWidget


OUTPUT = ROOT / "docs" / "images"


class SampleProvider:
    mode = "auto"

    def status_text(self) -> str:
        return "OpenD 已连接，正在使用实时行情"

    def set_mode(self, mode: str) -> None:
        self.mode = mode

    def check_futu_status(self) -> tuple[bool, str]:
        return True, "OpenD 已连接 · 剩余订阅额度 100"

    def close(self) -> None:
        pass


def sample_quote(
    symbol: str,
    name: str,
    price: float,
    change_percent: float,
    *,
    turnover_rate: float | None = None,
    source: str = "富途 OpenD（实时行情）",
) -> Quote:
    normalized = normalize_symbol(symbol)
    previous_close = price / (1 + change_percent / 100.0)
    change = price - previous_close
    return Quote(
        symbol=normalized,
        name=name,
        price=price,
        previous_close=previous_close,
        open_price=previous_close * 1.003,
        high=max(price, previous_close) * 1.008,
        low=min(price, previous_close) * 0.994,
        change=change,
        change_percent=change_percent,
        volume=32_560_000,
        volume_unit="股",
        amount=6_297_000_000,
        quote_time="2026-08-11 10:43:43",
        source=source,
        turnover_rate=turnover_rate,
    )


def build_quotes() -> list[Quote]:
    return [
        sample_quote("159516", "半导体设备ETF国泰", 0.762, 9.53, turnover_rate=16.18),
        sample_quote("515880", "通信ETF国泰", 0.648, 0.78, turnover_rate=3.46),
        sample_quote("512200", "房地产ETF南方", 1.207, 1.77, turnover_rate=5.21),
        sample_quote("512800", "银行ETF华宝", 0.801, -1.23, turnover_rate=1.08),
        sample_quote("00700", "腾讯控股", 473.60, 1.44, turnover_rate=0.32),
        sample_quote("01810", "小米集团-W", 27.04, -2.10, turnover_rate=0.42),
        sample_quote("SHCOMP", "上证指数", 3878.43, 1.47, source="腾讯行情"),
        sample_quote("SZCOMP", "深证成指", 14144.20, 1.86, source="腾讯行情"),
        sample_quote("CHINEXT", "创业板指", 3535.14, 1.32, source="腾讯行情"),
        sample_quote("HSI", "恒生指数", 25884.49, 0.12),
        sample_quote("HSTECH", "恒生科技指数", 4942.60, 0.80, source="腾讯行情"),
    ]


def save_widget(widget, filename: str, app: QApplication) -> None:
    widget.show()
    app.processEvents()
    pixmap = widget.grab()
    target = OUTPUT / filename
    if not pixmap.save(str(target), "PNG"):
        raise RuntimeError(f"Could not save {target}")


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    provider = SampleProvider()
    quotes = build_quotes()
    by_code = {quote.symbol.code: quote for quote in quotes}

    with tempfile.TemporaryDirectory() as temp_dir:
        settings = QSettings(
            str(Path(temp_dir) / "screenshots.ini"),
            QSettings.Format.IniFormat,
        )
        settings.setValue("market_defaults_v1_added", True)
        settings.setValue("a_share_etf_defaults_v1_added", True)
        settings.setValue(
            "watchlist",
            ["159516", "515880", "512200", "512800", "00700", "01810"],
        )
        settings.setValue("favorites", ["159516", "00700", "01810"])
        settings.setValue("quote_source", "auto")
        settings.setValue("panel_opacity", 100)

        panel = QuotePanel(provider, settings)  # type: ignore[arg-type]
        panel.update_watchlist_quotes(quotes)
        panel.market_tabs.setCurrentIndex(0)
        panel._display_quote(by_code["159516"])
        save_widget(panel, "quote-panel-dark.png", app)

        panel._set_theme("beige")
        panel.market_tabs.setCurrentIndex(1)
        panel._display_quote(by_code["01810"])
        save_widget(panel, "quote-panel-light.png", app)
        panel.close()

        dialog = SettingsDialog(
            settings,
            provider,  # type: ignore[arg-type]
            theme="dark",
        )
        dialog.provider_status.setText("✓ OpenD 已连接 · 剩余订阅额度 100")
        save_widget(dialog, "settings-opend.png", app)
        dialog.close()

        settings.setValue("theme", "dark")
        settings.setValue("skin", "maid-deepseek-whale")
        pet = StockPetWidget(settings)
        pet._animation.stop()
        pet._watch_timer.stop()
        pet._favorite_refresh_timer.stop()
        pet._favorite_carousel_timer.stop()
        pet._favorite_quotes = [by_code["159516"], by_code["00700"], by_code["01810"]]
        pet._render_favorite_page(0)
        save_widget(pet, "pet-bubble.png", app)
        pet.panel.close()
        pet.close()

    for path in sorted(OUTPUT.glob("*.png")):
        print(f"{path.relative_to(ROOT)}\t{path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
