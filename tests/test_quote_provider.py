from __future__ import annotations

import unittest

from stock_pet.quote_provider import QuoteError, TencentQuoteProvider, parse_tencent_payload
from stock_pet.symbols import normalize_symbol


def payload(fields: list[str], variable: str) -> str:
    return f'v_{variable}="{"~".join(fields)}";'


class QuoteParserTests(unittest.TestCase):
    def test_fetch_many_returns_names_for_multiple_markets_in_one_request(self) -> None:
        hk_fields = [""] * 78
        hk_fields[1] = "小米集团-W"
        hk_fields[3] = hk_fields[4] = "28.0"
        a_fields = [""] * 88
        a_fields[1] = "半导体设备ETF国泰"
        a_fields[3] = a_fields[4] = "0.7"
        requested: list[str] = []

        def transport(url: str) -> bytes:
            requested.append(url)
            return (
                payload(hk_fields, "hk01810")
                + "\n"
                + payload(a_fields, "sz159516")
            ).encode("gb18030")

        quotes = TencentQuoteProvider(transport=transport).fetch_many(["01810", "159516"])

        self.assertEqual([quote.name for quote in quotes], ["小米集团-W", "半导体设备ETF国泰"])
        self.assertIn("hk01810,sz159516", requested[0])

    def test_parse_hk_quote(self) -> None:
        fields = [""] * 78
        fields[1] = "腾讯控股"
        fields[2] = "00700"
        fields[3] = "494.800"
        fields[4] = "487.600"
        fields[5] = "493.400"
        fields[30] = "2026/08/05 10:35:20"
        fields[31] = "7.200"
        fields[32] = "1.48"
        fields[33] = "496.000"
        fields[34] = "482.200"
        fields[36] = "9558538"
        fields[37] = "4686033857.909"
        fields[59] = "0.28"

        quote = parse_tencent_payload(payload(fields, "hk00700"), normalize_symbol("00700"))
        self.assertEqual(quote.name, "腾讯控股")
        self.assertEqual(quote.price, 494.8)
        self.assertEqual(quote.change_percent, 1.48)
        self.assertEqual(quote.volume_unit, "股")
        self.assertEqual(quote.turnover_rate, 0.28)
        self.assertEqual(quote.quote_time, "2026-08-05 10:35:20")

    def test_parse_a_share_quote(self) -> None:
        fields = [""] * 88
        fields[1] = "贵州茅台"
        fields[2] = "600519"
        fields[3] = "1310.63"
        fields[4] = "1328.36"
        fields[5] = "1328.36"
        fields[30] = "20260805105026"
        fields[31] = "-17.73"
        fields[32] = "-1.33"
        fields[33] = "1333.80"
        fields[34] = "1309.00"
        fields[36] = "19201"
        fields[38] = "0.34"
        fields[57] = "252959.5252"

        quote = parse_tencent_payload(payload(fields, "sh600519"), normalize_symbol("600519"))
        self.assertEqual(quote.volume_unit, "手")
        self.assertAlmostEqual(quote.amount, 2_529_595_252.0)
        self.assertEqual(quote.turnover_rate, 0.34)
        self.assertEqual(quote.quote_time, "2026-08-05 10:50:26")
        self.assertEqual(quote.direction, -1)

    def test_empty_payload_fails(self) -> None:
        with self.assertRaises(QuoteError):
            parse_tencent_payload('v_hk99999="";', normalize_symbol("99999"))

    def test_parse_spot_gold_quote(self) -> None:
        raw = (
            'v_hf_XAU="4130.63,1.31,4130.63,4130.98,4136.41,4065.42,'
            '11:29:00,4077.21,4077.96,0,0,0,2026-08-05,伦敦金（现货黄金）";'
        )
        quote = parse_tencent_payload(raw, normalize_symbol("GOLD"))
        self.assertEqual(quote.name, "伦敦金（现货黄金）")
        self.assertEqual(quote.price, 4130.63)
        self.assertAlmostEqual(quote.change_percent, 1.31)
        self.assertEqual(quote.symbol.currency, "USD")
        self.assertIsNone(quote.turnover_rate)
        self.assertEqual(quote.quote_time, "2026-08-05 11:29:00")


if __name__ == "__main__":
    unittest.main()
