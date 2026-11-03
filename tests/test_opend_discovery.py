from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from stock_pet.hybrid_quote_provider import (
    FutuQuoteProvider,
    HybridQuoteProvider,
    OPEND_ABSENT_PROBE_SECONDS,
    OPEND_PROBE_TIMEOUT_SECONDS,
    is_opend_listening,
)
from stock_pet.models import Quote
from stock_pet.quote_provider import QuoteError, TencentQuoteProvider
from stock_pet.symbols import normalize_symbol


def quote(source):
    return Quote(normalize_symbol("01810"), "fixture", 40, 39, 39, 41, 38,
                 1, 2.56, 100, "股", 4000, "2026-11-03 14:00:00", source)


class OpenDDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        clock = patch("stock_pet.hybrid_quote_provider.time.monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.futu = FutuQuoteProvider()
        self.futu.fetch = Mock(return_value=quote("OpenD fixture"))
        self.futu.check_status = Mock(return_value=(True, "OpenD ready"))
        self.futu.close = Mock()
        self.futu.list_watchlist_groups = Mock(return_value=["全部"])
        self.tencent = Mock(spec=TencentQuoteProvider)
        self.tencent.fetch.return_value = quote("Tencent fixture")
        self.tencent.fetch_batch.return_value = ([quote("Tencent fixture")], [])
        self.probe = Mock(return_value=False)
        self.provider = HybridQuoteProvider(tencent=self.tencent, futu=self.futu, opend_probe=self.probe)

    def test_absent_opend_is_checked_once_across_detail_and_batches_without_sdk(self):
        with patch.object(self.futu, "_ensure_context", side_effect=AssertionError("SDK must not initialize")) as sdk:
            for _ in range(100):
                self.assertIn("腾讯", self.provider.fetch_interactive("01810").source)
                quotes, errors = self.provider.fetch_batch(["01810", "00700"])
                self.assertEqual(len(quotes), 1)
                self.assertEqual(errors, [])
        self.probe.assert_called_once_with("127.0.0.1", 11111)
        self.futu.fetch.assert_not_called()
        sdk.assert_not_called()
        self.assertIn("未检测到 OpenD", self.provider.status_text())
        self.assertEqual(self.provider.mode, "auto")

    def test_background_refresh_recovers_after_negative_cache_expires(self):
        self.provider.fetch("01810")
        self.probe.return_value = True
        self.now += OPEND_ABSENT_PROBE_SECONDS - 1
        self.assertIn("腾讯", self.provider.fetch("01810").source)
        self.futu.fetch.assert_not_called()
        self.now += 1
        self.assertEqual(self.provider.fetch("01810").source, "OpenD fixture")
        self.assertEqual(self.probe.call_count, 2)
        self.assertEqual(self.provider.mode, "auto")

    def test_manual_check_bypasses_cached_absence(self):
        self.provider.fetch("01810")
        self.probe.return_value = True
        self.assertEqual(self.provider.check_futu_status(), (True, "OpenD ready"))
        self.assertEqual(self.provider.fetch("01810").source, "OpenD fixture")
        self.assertEqual(self.probe.call_count, 2)
        self.futu.check_status.assert_called_once()

    def test_manual_check_and_import_fail_fast_when_service_is_absent(self):
        ok, message = self.provider.check_futu_status()
        self.assertFalse(ok)
        self.assertIn("未检测到 OpenD", message)
        self.futu.check_status.assert_not_called()
        with self.assertRaisesRegex(QuoteError, "未检测到 OpenD"):
            self.provider.list_futu_watchlist_groups()
        self.futu.list_watchlist_groups.assert_not_called()

    def test_positive_probe_is_cached_then_detects_service_exit(self):
        self.probe.return_value = True
        for _ in range(20):
            self.provider.fetch("01810")
        self.assertEqual(self.probe.call_count, 1)
        self.assertEqual(self.futu.fetch.call_count, 20)
        self.probe.return_value = False
        self.now += 30
        self.assertIn("腾讯", self.provider.fetch("01810").source)
        self.assertEqual(self.futu.fetch.call_count, 20)
        for _ in range(20):
            self.provider.fetch("01810")
        self.assertEqual(self.probe.call_count, 2)

    def test_open_port_does_not_imply_logged_in_or_authorized(self):
        self.probe.return_value = True
        self.futu.fetch.side_effect = QuoteError("缺少权限")
        self.assertIn("腾讯", self.provider.fetch("01810").source)
        for _ in range(10):
            self.provider.fetch("01810")
        self.assertEqual(self.futu.fetch.call_count, 1)
        self.assertEqual(self.probe.call_count, 1)
        # An SDK failure invalidates the probe; after cooldown a closed service
        # is negatively cached instead of attempting another expensive SDK call.
        self.probe.return_value = False
        self.now += 30
        self.provider.fetch("01810")
        self.assertEqual(self.futu.fetch.call_count, 1)
        self.assertEqual(self.probe.call_count, 2)

    def test_concurrent_requests_do_not_wait_for_an_in_progress_probe(self):
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        def slow_probe(_host, _port):
            entered.set()
            release.wait(2)
            return False
        self.probe.side_effect = slow_probe
        worker = threading.Thread(target=lambda: (self.provider.fetch("01810"), completed.set()))
        try:
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertIn("腾讯", self.provider.fetch_interactive("01810").source)
            self.assertFalse(completed.is_set())
            self.assertEqual(self.probe.call_count, 1)
        finally:
            release.set()
            worker.join(2)
        self.assertTrue(completed.is_set())
        self.futu.fetch.assert_not_called()

    def test_probe_result_from_old_source_cannot_poison_new_generation(self):
        entered, release = threading.Event(), threading.Event()
        def slow_probe(_host, _port):
            entered.set()
            release.wait(2)
            return False
        self.probe.side_effect = slow_probe
        worker = threading.Thread(target=lambda: self.provider.fetch("01810"))
        try:
            worker.start()
            self.assertTrue(entered.wait(1))
            self.provider.set_mode("tencent")
            self.provider.set_mode("auto")
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.provider._probe_after, 0)
        self.probe.side_effect = None
        self.probe.return_value = True
        with patch.object(self.provider.futu, "fetch", return_value=quote("new OpenD")):
            self.assertEqual(self.provider.fetch("01810").source, "new OpenD")

    def test_explicit_tencent_and_unsupported_symbols_do_not_probe(self):
        self.provider.set_mode("tencent")
        self.provider.fetch("01810")
        self.provider.fetch_batch(["01810", "00700"])
        self.provider.set_mode("auto")
        self.provider.fetch("GOLD")
        self.provider.fetch_batch(["GOLD", "HSTECH"])
        self.probe.assert_not_called()

    def test_explicit_import_can_recheck_after_opend_is_started(self):
        self.provider.fetch("01810")
        self.probe.return_value = True
        self.assertEqual(self.provider.list_futu_watchlist_groups(), ["全部"])
        self.futu.list_watchlist_groups.assert_called_once()


class EndpointProbeTests(unittest.TestCase):
    def test_socket_probe_is_bounded_and_closes_connection(self):
        with patch("stock_pet.hybrid_quote_provider.socket.create_connection") as connect:
            self.assertTrue(is_opend_listening("127.0.0.1", 11111))
            connect.assert_called_once_with(("127.0.0.1", 11111), timeout=OPEND_PROBE_TIMEOUT_SECONDS)
            connect.return_value.__exit__.assert_called_once()

    def test_refused_timed_out_and_unresolvable_endpoints_are_unavailable(self):
        for error in (ConnectionRefusedError(), TimeoutError(), OSError("unresolvable")):
            with self.subTest(error=type(error).__name__), \
                 patch("stock_pet.hybrid_quote_provider.socket.create_connection", side_effect=error):
                self.assertFalse(is_opend_listening("127.0.0.1", 11111))


if __name__ == "__main__":
    unittest.main()
