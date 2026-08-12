from __future__ import annotations

import unittest

from stock_pet.symbols import (
    MAX_WATCHLIST_ITEMS,
    SymbolError,
    normalize_symbol,
    normalize_watchlist,
    partition_watchlist,
)


class NormalizeSymbolTests(unittest.TestCase):
    def test_hk_short_code_is_padded(self) -> None:
        symbol = normalize_symbol("700")
        self.assertEqual(symbol.provider_symbol, "hk00700")
        self.assertEqual(symbol.currency, "HKD")

    def test_hk_suffix(self) -> None:
        self.assertEqual(normalize_symbol("0700.HK").provider_symbol, "hk00700")

    def test_shanghai(self) -> None:
        self.assertEqual(normalize_symbol("600519").provider_symbol, "sh600519")
        self.assertEqual(normalize_symbol("600519.SS").provider_symbol, "sh600519")

    def test_shenzhen(self) -> None:
        self.assertEqual(normalize_symbol("000001").provider_symbol, "sz000001")

    def test_shenzhen_etf(self) -> None:
        self.assertEqual(normalize_symbol("159516").provider_symbol, "sz159516")

    def test_beijing(self) -> None:
        self.assertEqual(normalize_symbol("920019").provider_symbol, "bj920019")

    def test_invalid_symbol(self) -> None:
        with self.assertRaises(SymbolError):
            normalize_symbol("腾讯")

    def test_watchlist_is_normalized_and_deduplicated(self) -> None:
        self.assertEqual(
            normalize_watchlist(["700", "HK00700", "600519", "600519.SS", "000001"]),
            ["00700", "600519", "000001"],
        )

    def test_watchlist_limit(self) -> None:
        with self.assertRaises(SymbolError):
            normalize_watchlist(["00700", "00941"], max_items=1)
        self.assertEqual(MAX_WATCHLIST_ITEMS, 100)

    def test_watchlist_is_partitioned_by_market(self) -> None:
        self.assertEqual(
            partition_watchlist(["00700", "600519", "000001", "00941"]),
            (["600519", "000001"], ["00700", "00941"]),
        )

    def test_market_index_aliases(self) -> None:
        self.assertEqual(normalize_symbol("HSI").provider_symbol, "hkHSI")
        self.assertEqual(normalize_symbol("HSTECH").market_label, "港股指数")
        self.assertEqual(normalize_symbol("CSI300").provider_symbol, "sh000300")
        self.assertEqual(normalize_symbol("CHINEXT").display_code, "CHINEXT")

    def test_gold_alias(self) -> None:
        symbol = normalize_symbol("gold")
        self.assertEqual(symbol.provider_symbol, "hf_XAU")
        self.assertEqual(symbol.currency, "USD")

    def test_index_cannot_be_saved_to_stock_watchlist(self) -> None:
        with self.assertRaises(SymbolError):
            normalize_watchlist(["HSI"])


if __name__ == "__main__":
    unittest.main()
