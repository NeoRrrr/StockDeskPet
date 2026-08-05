from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication, QPushButton

from stock_pet.quote_provider import TencentQuoteProvider
from stock_pet.ui import (
    DEFAULT_A_SHARE_ETFS,
    DEFAULT_HK_WATCHLIST,
    INDEX_SYMBOLS,
    TAB_MARKET_SUMMARIES,
    QuotePanel,
)


class QuotePanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_current_tab_refresh_emits_only_visible_market(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = QSettings(os.path.join(temp_dir, "settings.ini"), QSettings.Format.IniFormat)
            settings.setValue("market_defaults_v1_added", True)
            settings.setValue("a_share_etf_defaults_v1_added", True)
            settings.setValue("watchlist", [*DEFAULT_A_SHARE_ETFS, *DEFAULT_HK_WATCHLIST])
            panel = QuotePanel(TencentQuoteProvider(), settings)
            captured: list[list[str]] = []
            panel.page_refresh_requested.connect(lambda symbols: captured.append(list(symbols)))

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
            self.assertEqual(captured[1], [*DEFAULT_HK_WATCHLIST, *hk_summary])
            self.assertEqual(captured[2], list(INDEX_SYMBOLS))
            self.assertEqual(
                panel.index_list.currentItem().data(Qt.ItemDataRole.UserRole),
                INDEX_SYMBOLS[0],
            )
            self.assertEqual(panel.market_summary_label.text(), "")
            button_texts = {button.text() for button in panel.findChildren(QPushButton)}
            self.assertIn("刷新当前页", button_texts)
            self.assertNotIn("检查自选", button_texts)
            panel.close()


if __name__ == "__main__":
    unittest.main()
