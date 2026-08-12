from __future__ import annotations

import unittest

from stock_pet.hybrid_quote_provider import (
    HybridQuoteProvider,
    futu_code_for,
    quote_from_futu_row,
)
from stock_pet.models import Quote
from stock_pet.symbols import normalize_symbol


class HybridQuoteProviderTests(unittest.TestCase):
    def test_futu_code_mapping_uses_official_market_prefixes(self) -> None:
        self.assertEqual(futu_code_for(normalize_symbol("00700")), "HK.00700")
        self.assertEqual(futu_code_for(normalize_symbol("600519")), "SH.600519")
        self.assertEqual(futu_code_for(normalize_symbol("159516")), "SZ.159516")
        self.assertEqual(futu_code_for(normalize_symbol("HSI")), "HK.800000")
        self.assertIsNone(futu_code_for(normalize_symbol("HSTECH")))
        self.assertIsNone(futu_code_for(normalize_symbol("GOLD")))

    def test_futu_row_is_converted_to_existing_quote_model(self) -> None:
        symbol = normalize_symbol("01810")
        quote = quote_from_futu_row(
            {
                "name": "小米集团-W",
                "last_price": 28.40,
                "prev_close_price": 28.00,
                "open_price": 28.10,
                "high_price": 28.60,
                "low_price": 27.90,
                "volume": 12_300_000,
                "turnover": 345_000_000,
                "turnover_rate": 0.42,
                "data_date": "2026-08-11",
                "data_time": "14:05:06",
            },
            symbol,
        )

        self.assertEqual(quote.name, "小米集团-W")
        self.assertAlmostEqual(quote.price, 28.40)
        self.assertAlmostEqual(quote.change, 0.40)
        self.assertAlmostEqual(quote.change_percent, 1.428571, places=5)
        self.assertEqual(quote.turnover_rate, 0.42)
        self.assertEqual(quote.quote_time, "2026-08-11 14:05:06")
        self.assertIn("OpenD", quote.source)

    def test_auto_mode_falls_back_to_tencent_when_opend_is_unavailable(self) -> None:
        symbol = normalize_symbol("01810")
        fallback_quote = Quote(
            symbol=symbol,
            name="小米集团-W",
            price=28.0,
            previous_close=27.5,
            open_price=27.8,
            high=28.2,
            low=27.6,
            change=0.5,
            change_percent=1.81,
            volume=1.0,
            volume_unit="手",
            amount=1.0,
            quote_time="2026-08-11 14:00:00",
            source="腾讯行情",
        )

        class OfflineFutu:
            def supports(self, _symbol) -> bool:
                return True

            def fetch(self, _raw_symbol: str) -> Quote:
                raise RuntimeError("connection refused 127.0.0.1:11111")

            def close(self) -> None:
                pass

            def check_status(self) -> tuple[bool, str]:
                return False, "OpenD 未连接"

        class TencentFallback:
            def fetch(self, _raw_symbol: str) -> Quote:
                return fallback_quote

            def search(self, _query: str, limit: int = 8) -> list:
                return []

        provider = HybridQuoteProvider(
            mode="auto",
            tencent=TencentFallback(),  # type: ignore[arg-type]
            futu=OfflineFutu(),  # type: ignore[arg-type]
        )
        result = provider.fetch("01810")

        self.assertEqual(result.price, fallback_quote.price)
        self.assertIn("富途不可用", result.source)
        self.assertIn("OpenD 未连接", provider.status_text())

    def test_interactive_fetch_falls_back_immediately_when_futu_is_busy(self) -> None:
        symbol = normalize_symbol("01810")
        fallback_quote = Quote(
            symbol=symbol,
            name="小米集团-W",
            price=28.0,
            previous_close=27.5,
            open_price=27.8,
            high=28.2,
            low=27.6,
            change=0.5,
            change_percent=1.81,
            volume=1.0,
            volume_unit="手",
            amount=1.0,
            quote_time="2026-08-11 14:00:00",
            source="腾讯行情",
        )

        class BusyFutu:
            def supports(self, _symbol) -> bool:
                return True

            def fetch(self, _raw_symbol: str, *, lock_timeout=None) -> Quote:
                from stock_pet.hybrid_quote_provider import FutuBusyError

                self.lock_timeout = lock_timeout
                raise FutuBusyError("富途正在后台刷新")

            def close(self) -> None:
                pass

        class TencentFallback:
            def fetch(self, _raw_symbol: str) -> Quote:
                return fallback_quote

            def search(self, _query: str, limit: int = 8) -> list:
                return []

        futu = BusyFutu()
        provider = HybridQuoteProvider(
            mode="auto",
            tencent=TencentFallback(),  # type: ignore[arg-type]
            futu=futu,  # type: ignore[arg-type]
        )

        result = provider.fetch_interactive("01810")

        self.assertEqual(result.price, fallback_quote.price)
        self.assertIn("富途后台刷新中", result.source)
        self.assertGreater(futu.lock_timeout, 0)


if __name__ == "__main__":
    unittest.main()
