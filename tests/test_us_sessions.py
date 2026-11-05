from __future__ import annotations

from dataclasses import replace
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings
from PySide6.QtWidgets import QApplication

from stock_pet.hybrid_quote_provider import FutuQuoteProvider, quote_from_futu_row
from stock_pet.models import quote_is_older, quote_session_label, us_quote_is_current
from stock_pet.symbols import normalize_symbol
from stock_pet.ui import (
    QuotePanel, SettingsDialog, StockPetWidget, US_DEFAULTS_SETTINGS_KEY,
    US_OVERNIGHT_BUBBLE_SETTINGS_KEY,
    _is_open_for_automatic_refresh, _is_visible_in_idle_bubble,
)
from stock_pet.quote_provider import TencentQuoteProvider
from stock_pet.us_sessions import US_TIMEZONE, parse_us_time, us_session_at


def et(text):
    return datetime.fromisoformat(text).replace(tzinfo=US_TIMEZONE)


class Rows:
    def __init__(self, *rows):
        self.rows = list(rows)
        self.empty = not rows
        self.iloc = self.rows

    def iterrows(self):
        return enumerate(self.rows)


def snapshot(kind="overnight"):
    return {
        "code": "US.NVDA", "name": "英伟达", "last_price": 200,
        "prev_close_price": 195, "open_price": 196, "high_price": 201,
        "low_price": 194, "volume": 10000, "turnover": 2000000,
        "turnover_rate": .5, "data_date": "2026-11-03", "data_time": "16:00:00.229",
        f"{kind}_price": 206, f"{kind}_change_val": 6,
        f"{kind}_high_price": 206, f"{kind}_low_price": 202,
        f"{kind}_volume": 100, f"{kind}_turnover": 20500,
    }


def tick(stamp="2026-11-04 01:01:00.500", price=207, code="US.NVDA"):
    return {"code": code, "time": stamp, "price": price}


def night_quote(stamp="2026-11-04 01:01:00.500"):
    return quote_from_futu_row(snapshot(), normalize_symbol("NVDA"),
                               ticks=Rows(tick(stamp)), at=parse_us_time(stamp))


class USSessionTests(unittest.TestCase):
    def test_boundaries_and_overnight_trading_date(self):
        cases = [
            ("2026-11-01 19:59:59", None, None),
            ("2026-11-01 20:00:00", "overnight", "2026-11-02"),
            ("2026-11-02 00:00:00", "overnight", "2026-11-02"),
            ("2026-11-02 03:59:59", "overnight", "2026-11-02"),
            ("2026-11-02 04:00:00", "pre", "2026-11-02"),
            ("2026-11-02 09:29:59", "pre", "2026-11-02"),
            ("2026-11-02 09:30:00", "regular", "2026-11-02"),
            ("2026-11-02 15:59:59", "regular", "2026-11-02"),
            ("2026-11-02 16:00:00", "after", "2026-11-02"),
            ("2026-11-02 19:59:59", "after", "2026-11-02"),
            ("2026-11-02 20:00:00", "overnight", "2026-11-03"),
            ("2026-11-06 19:59:59", "after", "2026-11-06"),
            ("2026-11-06 20:00:00", None, None),
            ("2026-11-07 01:00:00", None, None),
        ]
        for when, kind, trade_date in cases:
            with self.subTest(when=when):
                session = us_session_at(et(when))
                self.assertEqual(session.kind if session else None, kind)
                self.assertEqual(session.trading_date.isoformat() if session else None, trade_date)
                if session:
                    self.assertTrue(session.contains(et(when)))
                    self.assertFalse(session.contains(session.end))

    def test_friday_grace_polls_but_never_accepts_a_weekend_tick(self):
        session = us_session_at(et("2026-11-06 20:29:59"), close_grace=True)
        self.assertEqual(session.kind, "after")
        self.assertFalse(session.contains(et("2026-11-06 20:01:00")))
        self.assertIsNone(us_session_at(et("2026-11-06 20:30:00"), close_grace=True))
        for when, expected in (("2026-11-06 20:29:59", True), ("2026-11-06 20:30:00", False),
                               ("2026-11-08 19:59:59", False), ("2026-11-08 20:00:00", True)):
            for method in (_is_open_for_automatic_refresh, _is_visible_in_idle_bubble):
                self.assertEqual(method("NVDA", et(when)), expected)
        self.assertFalse(_is_open_for_automatic_refresh("SPX", et("2026-11-04 01:00:00")))

    def test_dst_and_china_cross_day(self):
        china = timezone(timedelta(hours=8))
        for when, china_hour in (("2026-07-05 20:00:00", 8), ("2026-11-01 20:00:00", 9)):
            current = et(when).astimezone(china)
            self.assertEqual(current.hour, china_hour)
            self.assertEqual(current.weekday(), 0)
            self.assertEqual(us_session_at(current).kind, "overnight")
            self.assertTrue(_is_open_for_automatic_refresh("NVDA", current))
        self.assertEqual(parse_us_time("2026-11-04 01:02:03:456").microsecond, 456000)
        self.assertIsNone(parse_us_time("bad date"))


class ExtendedQuoteTests(unittest.TestCase):
    def test_each_extended_session_uses_actual_trade_price_and_time(self):
        for kind, stamp, day in (
            ("overnight", "2026-11-03 23:59:59.500", "2026-11-04"),
            ("pre", "2026-11-04 06:01:00.500", "2026-11-04"),
            ("after", "2026-11-03 17:01:00.500", "2026-11-03"),
        ):
            with self.subTest(kind=kind):
                quote = quote_from_futu_row(snapshot(kind), normalize_symbol("NVDA"),
                                           ticks=Rows(tick(stamp)), at=parse_us_time(stamp))
                self.assertEqual((quote.session, quote.session_date), (kind, day))
                self.assertEqual(quote.quote_time, stamp)
                self.assertEqual((quote.price, quote.previous_close), (207, 200))
                self.assertAlmostEqual(quote.change_percent, 3.5)
                self.assertEqual((quote.high, quote.low, quote.volume, quote.amount), (207, 202, 100, 20500))
                self.assertIsNone(quote.open_price)
                self.assertIsNone(quote.turnover_rate)
                self.assertTrue(us_quote_is_current(quote, parse_us_time(stamp)))

    def test_invalid_or_wrong_session_trade_falls_back_without_fake_timestamp(self):
        now = et("2026-11-04 01:01:00")
        for ticks in (None, Rows(), Rows(tick("2026-11-03 16:00:00")),
                      Rows(tick("2026-11-04 01:02:00")), Rows(tick(price=float("nan"))),
                      Rows(tick(price=0)), Rows(tick(code="US.TSLA")), Rows(tick("bad"))):
            quote = quote_from_futu_row(snapshot(), normalize_symbol("NVDA"), ticks=ticks, at=now)
            self.assertEqual((quote.session, quote.price), ("regular", 200))
            self.assertEqual(quote.quote_time, "2026-11-03 16:00:00.229")
            self.assertIn("显示常规行情", quote.session_note)
            self.assertFalse(us_quote_is_current(quote, now))
            self.assertEqual(quote_session_label(quote, now), "常规参考")

    def test_bad_snapshot_reference_or_permissions_fail_safely(self):
        for edits, error in (({"overnight_price": 0}, ""),
                             ({"overnight_change_val": None}, ""),
                             ({"overnight_change_val": 1000}, ""),
                             ({"overnight_change_val": 1}, ""),
                             ({}, "没有夜盘权限")):
            quote = quote_from_futu_row({**snapshot(), **edits}, normalize_symbol("NVDA"),
                                       ticks=Rows(tick()), at=et("2026-11-04 01:01:01"),
                                       extended_error=error)
            self.assertEqual(quote.session, "regular")
            self.assertIn("显示常规行情", quote.session_note)

    def test_latest_trade_and_missing_metrics(self):
        quote = quote_from_futu_row(
            {**snapshot(), "overnight_high_price": None, "overnight_low_price": None,
             "overnight_volume": None, "overnight_turnover": None}, normalize_symbol("NVDA"),
            ticks=Rows(tick("2026-11-04 01:01:01", 208), tick("2026-11-04 01:01:00", 207)),
            at=et("2026-11-04 01:01:02"),
        )
        self.assertEqual(quote.price, 208)
        self.assertIsNone(quote.high)
        self.assertIsNone(quote.low)
        self.assertIsNone(quote.amount)
        self.assertIsNone(quote.volume)

    def test_regular_and_hong_kong_keep_regular_fields(self):
        for code in ("NVDA", "01810"):
            quote = quote_from_futu_row(snapshot(), normalize_symbol(code),
                                       ticks=Rows(tick()), at=et("2026-11-04 10:00:00"))
            self.assertEqual(quote.price, 200)
            self.assertEqual(quote.open_price, 196)
            self.assertEqual(quote.turnover_rate, .5)

    def test_cache_order_and_labels_across_sessions(self):
        quote = night_quote()
        regular = quote_from_futu_row(snapshot(), normalize_symbol("NVDA"), at=et("2026-11-03 15:59:00"))
        self.assertTrue(quote_is_older(regular, quote))
        self.assertTrue(quote_is_older(replace(quote, quote_time="2026-11-04 01:01:00:499"), quote))
        self.assertEqual(quote_session_label(quote, et("2026-11-04 01:01:02")), "夜盘")
        self.assertEqual(quote_session_label(quote, et("2026-11-04 01:04:00")), "夜盘·旧")
        self.assertEqual(quote_session_label(quote, et("2026-11-04 04:00:00")), "夜盘·历史")
        new_regular = replace(regular, quote_time="2026-11-04 09:30:01")
        self.assertFalse(quote_is_older(new_regular, quote))
        self.assertEqual(quote_session_label(new_regular, et("2026-11-04 09:30:02")), "盘中")
        self.assertEqual(quote_session_label(regular, et("2026-11-07 10:00:00")), "常规·休市")


class ExtendedProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = FutuQuoteProvider()
        self.context = Mock()
        self.context.subscribe.return_value = (0, None)
        self.context.get_stock_quote.return_value = (0, Rows(snapshot()))
        self.context.get_rt_ticker.return_value = (0, Rows(tick()))
        self.provider._context = self.context
        self.provider._ret_ok = 0
        self.provider._sub_type = SimpleNamespace(QUOTE="QUOTE", TICKER="TICKER")
        self.provider._session_all = "ALL"

    def test_all_session_ticker_is_subscribed_only_once(self):
        self.provider._extended_ticks(self.context, "US.NVDA")
        self.provider._extended_ticks(self.context, "US.NVDA")
        self.context.subscribe.assert_called_once_with(
            ["US.NVDA"], ["TICKER"], is_first_push=False, subscribe_push=False, session="ALL")
        self.assertEqual(self.context.get_rt_ticker.call_count, 2)

    def test_night_permission_failure_backs_off_without_resetting_regular_connection(self):
        self.context.subscribe.return_value = (-1, "夜盘权限不足")
        self.provider._subscribed.add("HK.00700")
        with patch("stock_pet.hybrid_quote_provider.time.monotonic", return_value=100):
            _, error = self.provider._extended_ticks(self.context, "US.NVDA")
            self.assertIn("缺少对应实时行情权限", error)
        self.assertIs(self.provider._context, self.context)
        self.assertIn("HK.00700", self.provider._subscribed)
        self.context.close.assert_not_called()
        with patch("stock_pet.hybrid_quote_provider.time.monotonic", return_value=399):
            self.provider._extended_ticks(self.context, "US.NVDA")
        self.assertEqual(self.context.subscribe.call_count, 1)
        self.context.subscribe.return_value = (0, None)
        with patch("stock_pet.hybrid_quote_provider.time.monotonic", return_value=400):
            self.assertEqual(self.provider._extended_ticks(self.context, "US.NVDA")[1], "")
        self.assertEqual(self.context.subscribe.call_count, 2)
        self.assertFalse(self.provider._ticker_errors)

    def test_fetch_regular_and_hk_never_request_extra_ticker(self):
        session = us_session_at(et("2026-11-04 10:00:00"))
        with patch("stock_pet.hybrid_quote_provider.us_session_at", return_value=session):
            self.provider.fetch("NVDA")
            self.provider.fetch("00700")
        self.context.get_rt_ticker.assert_not_called()

    def test_fetch_uses_verified_overnight_data_and_reset_clears_bookkeeping(self):
        now = et("2026-11-04 01:01:01")
        with patch("stock_pet.hybrid_quote_provider.us_now", return_value=now), patch(
            "stock_pet.hybrid_quote_provider.us_session_at", return_value=us_session_at(now)
        ):
            quote = self.provider.fetch("NVDA")
        self.assertEqual(quote.session, "overnight")
        self.assertEqual(quote.price, 207)
        self.context.get_rt_ticker.return_value = (-1, "timeout")
        self.provider._extended_ticks(self.context, "US.NVDA")
        self.assertNotIn("US.NVDA", self.provider._ticker_subscribed)
        self.assertIn("US.NVDA", self.provider._ticker_errors)
        self.provider._reset_context()
        self.assertFalse(self.provider._ticker_retry_after)
        self.assertFalse(self.provider._ticker_errors)


class ExtendedUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.settings = QSettings(str(Path(directory.name) / "test.ini"), QSettings.IniFormat)
        for key, value in {
            "watchlist": ["US.NVDA"], "favorites": ["US.NVDA"], "skin": "default",
            "quote_source": "tencent", "alerts_enabled": True,
            "market_defaults_v1_added": True, "a_share_etf_defaults_v1_added": True,
            US_DEFAULTS_SETTINGS_KEY: True,
            US_OVERNIGHT_BUBBLE_SETTINGS_KEY: True,
        }.items():
            self.settings.setValue(key, value)
        for target in ("stock_pet.ui.QThreadPool.globalInstance", "stock_pet.ui.QTimer.singleShot"):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)

    def close_widget(self, widget):
        if isinstance(widget, StockPetWidget):
            widget._animation.stop()
            widget._watch_timer.stop()
            widget._favorite_refresh_timer.stop()
            widget._favorite_carousel_timer.stop()
            widget.close_provider()
            widget.panel.close()
            widget.panel.deleteLater()
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def at_session_time(self, text):
        now = et(text)
        stack = ExitStack()
        stack.enter_context(patch("stock_pet.ui.us_session_at", return_value=us_session_at(now, close_grace=True)))
        stack.enter_context(patch("stock_pet.models.us_now", return_value=now))
        stack.enter_context(patch("stock_pet.ui._is_visible_in_idle_bubble",
                                  side_effect=lambda code: _is_visible_in_idle_bubble(code, now)))
        return stack

    def make_pet(self):
        pet = StockPetWidget(self.settings)
        self.addCleanup(self.close_widget, pet)
        return pet

    def test_overnight_checkbox_defaults_off_and_remembers_explicit_choices(self):
        self.settings.remove(US_OVERNIGHT_BUBBLE_SETTINGS_KEY)
        pet = self.make_pet()
        self.assertFalse(pet.show_us_overnight_bubble)
        dialog = SettingsDialog(self.settings, pet.provider)
        self.addCleanup(self.close_widget, dialog)
        self.assertFalse(dialog.us_overnight_bubble_checkbox.isChecked())
        changes = []
        dialog.us_overnight_bubble_changed.connect(changes.append)
        dialog.us_overnight_bubble_checkbox.setChecked(True)
        self.assertEqual(changes, [True])
        # Exercise persisted INI values and a new application instance.
        reloaded = QSettings(self.settings.fileName(), QSettings.IniFormat)
        restored = SettingsDialog(reloaded, pet.provider, theme="beige")
        self.addCleanup(self.close_widget, restored)
        self.assertTrue(restored.us_overnight_bubble_checkbox.isChecked())
        self.assertTrue(self.make_pet().show_us_overnight_bubble)
        restored.us_overnight_bubble_checkbox.setChecked(False)
        self.assertFalse(self.make_pet().show_us_overnight_bubble)

    def test_settings_signal_hides_only_bubble_immediately_without_fetching(self):
        pet = self.make_pet()
        quote = night_quote()
        with self.at_session_time("2026-11-04 01:01:01"):
            pet.panel.update_watchlist_quotes([quote])
            pet.panel._display_quote(quote)
            pet._show_next_favorite()
            self.assertFalse(pet.bubble.isHidden())
            detail = pet.panel.price_label.text()
            timer_interval = pet._favorite_refresh_timer.interval()

            def toggle_in_dialog(dialog):
                self.addCleanup(self.close_widget, dialog)
                dialog.us_overnight_bubble_checkbox.setChecked(False)
                self.assertTrue(pet.bubble.isHidden())
                return 0

            with patch.object(SettingsDialog, "exec", toggle_in_dialog), patch.object(
                pet, "refresh_favorites"
            ) as fetch:
                pet.panel.open_settings()
                fetch.assert_not_called()
            self.assertFalse(pet.show_us_overnight_bubble)
            self.assertFalse(pet._favorite_carousel_timer.isActive())
            self.assertEqual(pet.panel.price_label.text(), detail)
            self.assertIn("夜盘", pet.panel.name_label.text())
            self.assertIs(pet.panel._quote_cache["usNVDA"], quote)
            self.assertEqual(pet._favorite_quotes, [quote])
            self.assertEqual(pet._favorite_refresh_timer.interval(), timer_interval)
            self.assertTrue(pet.alerts_enabled)
            pet._apply_us_overnight_bubble(True)
            self.assertFalse(pet.bubble.isHidden())
            self.assertIn("英伟达", pet.bubble.text())

    def test_hidden_overnight_does_not_reappear_on_late_result_or_error(self):
        self.settings.setValue(US_OVERNIGHT_BUBBLE_SETTINGS_KEY, "false")
        pet = self.make_pet()
        with self.at_session_time("2026-11-04 01:01:01"):
            pet._show_favorite_prompt()
            self.assertTrue(pet.bubble.isHidden())
            pet.refresh_favorites()
            self.assertEqual(pet._favorite_task.symbols, ["US.NVDA"])
            pet._on_favorite_result(([], ["timeout"]))
            self.assertTrue(pet.bubble.isHidden())
            pet._on_favorite_result(([night_quote()], []))
            self.assertTrue(pet.bubble.isHidden())
            self.assertIn("usNVDA", pet.panel._quote_cache)
            pet._on_favorite_result(([], ["timeout"]))
            self.assertTrue(pet.bubble.isHidden())
            # An old regular quote from Tencent must not bypass the preference.
            pet._favorite_quotes = [replace(night_quote(), session="regular")]
            pet._show_next_favorite()
            self.assertTrue(pet.bubble.isHidden())

    def test_overnight_filter_keeps_other_markets_and_repaginates(self):
        self.settings.setValue("favorites", ["US.NVDA", "01810", "GOLD"])
        pet = self.make_pet()
        night = night_quote()
        hk = replace(night, symbol=normalize_symbol("01810"), name="小米", session="regular")
        gold = replace(night, symbol=normalize_symbol("GOLD"), name="黄金", session="regular")
        with self.at_session_time("2026-11-04 01:01:01"):
            pet.panel.update_watchlist_quotes([night, hk, gold])
            pet._apply_bubble_settings(1, 10, 100)
            self.assertEqual(pet._favorite_page_count(), 3)
            pet._favorite_index = 2
            pet._apply_us_overnight_bubble(False)
            self.assertEqual(pet._visible_favorite_quotes(), [hk, gold])
            self.assertEqual(pet._favorite_page_count(), 2)
            self.assertTrue(pet._favorite_carousel_timer.isActive())
            self.assertIn("小米", pet.bubble.text())
            pet._show_next_favorite()
            self.assertIn("黄金", pet.bubble.text())
            self.assertEqual(len(pet._favorite_quotes), 3)

    def test_other_us_sessions_still_display_and_overnight_cache_stays_hidden(self):
        self.settings.setValue(US_OVERNIGHT_BUBBLE_SETTINGS_KEY, False)
        pet = self.make_pet()
        for stamp, kind in (("2026-11-04 04:00:00", "pre"),
                            ("2026-11-04 10:00:00", "regular"),
                            ("2026-11-04 17:00:00", "after"),
                            ("2026-11-06 20:15:00", "after")):
            with self.subTest(kind=kind, stamp=stamp), self.at_session_time(stamp):
                pet._favorite_quotes = [replace(night_quote(), session=kind, quote_time=stamp)]
                pet._show_next_favorite()
                self.assertFalse(pet.bubble.isHidden())
                if kind == "regular":
                    self.assertTrue(pet._is_symbol_visible_in_bubble("DJI"))
        with self.at_session_time("2026-11-04 04:00:00"):
            pet._favorite_quotes = [night_quote()]
            pet._show_next_favorite()
            self.assertTrue(pet.bubble.isHidden())

    def test_crossing_into_overnight_hides_bubble_on_periodic_refresh(self):
        self.settings.setValue(US_OVERNIGHT_BUBBLE_SETTINGS_KEY, False)
        pet = self.make_pet()
        pet._favorite_quotes = [replace(night_quote(), session="after")]
        with self.at_session_time("2026-11-03 19:59:59"):
            pet._show_next_favorite()
            self.assertFalse(pet.bubble.isHidden())
        with self.at_session_time("2026-11-03 20:00:00"):
            pet.refresh_favorites()
            self.assertTrue(pet.bubble.isHidden())
        with self.at_session_time("2026-11-04 04:01:00"):
            pet._on_favorite_result(([replace(night_quote(), session="pre")], []))
            self.assertFalse(pet.bubble.isHidden())

    def test_enabling_overnight_respects_zero_bubble_rows(self):
        pet = self.make_pet()
        with self.at_session_time("2026-11-04 01:01:01"):
            pet._favorite_quotes = [night_quote()]
            pet._apply_bubble_settings(0, 10, 100)
            pet._apply_us_overnight_bubble(False)
            pet._apply_us_overnight_bubble(True)
            self.assertTrue(pet.bubble.isHidden())
            self.assertFalse(pet._favorite_carousel_timer.isActive())

    def test_session_labels_and_metrics_render_in_place(self):
        panel = QuotePanel(TencentQuoteProvider(), self.settings)
        self.addCleanup(self.close_widget, panel)
        quote = replace(night_quote(), high=None, low=None, amount=None)
        with patch("stock_pet.models.us_now", return_value=et("2026-11-04 01:01:01")), patch(
            "stock_pet.models.us_session_at", return_value=us_session_at(et("2026-11-04 01:01:01"))
        ):
            panel.update_watchlist_quotes([quote])
            panel._display_quote(quote)
        self.assertIn("夜盘", panel.name_label.text())
        self.assertIn("基准收盘 200", panel.details_label.text())
        self.assertIn("时段最高 --", panel.details_label.text())
        self.assertIn("换手 --", panel.details_label.text())
        self.assertIn("01:01:00.500 美东", panel.status_label.text())
        rows = panel._quote_rows["usNVDA"]
        self.assertTrue(all("[夜盘]" in row.text_label.text() for _, row, _ in rows))
        with patch("stock_pet.models.us_now", return_value=et("2026-11-04 04:01:00")), patch(
            "stock_pet.models.us_session_at", return_value=us_session_at(et("2026-11-04 04:01:00"))
        ), patch.object(panel, "_refresh_watchlist_lists") as rebuild:
            panel._refresh_us_session_labels()
            rebuild.assert_not_called()
        self.assertIs(panel._quote_rows["usNVDA"], rows)
        self.assertTrue(all("[夜盘·历史]" in row.text_label.text() for _, row, _ in rows))

    def test_bubble_and_alert_do_not_rearm_at_midnight_or_alert_on_regular_fallback(self):
        pet = StockPetWidget(self.settings)
        self.addCleanup(self.close_widget, pet)
        notifications = []
        pet.alert_requested.connect(lambda _title, text: notifications.append(text))
        for stamp in ("2026-11-03 23:59:00.500", "2026-11-04 00:01:00.500"):
            quote = night_quote(stamp)
            now = parse_us_time(stamp)
            with patch("stock_pet.models.us_now", return_value=now), patch(
                "stock_pet.models.us_session_at", return_value=us_session_at(now)
            ), patch("stock_pet.ui._is_visible_in_idle_bubble", return_value=True):
                pet.panel.update_watchlist_quotes([quote])
                pet._process_alert_quote("usNVDA", quote)
                pet._show_next_favorite()
                self.assertIn("夜盘", pet.bubble.text())
        self.assertEqual(len(notifications), 1)
        self.assertEqual(pet._alert_quote_dates["usNVDA"], "2026-11-04:overnight")
        regular = replace(quote, session="regular", quote_time="2026-11-03 16:00:00", session_date="2026-11-03")
        with patch("stock_pet.models.us_now", return_value=now), patch(
            "stock_pet.models.us_session_at", return_value=us_session_at(now)
        ):
            pet._process_alert_quote("usNVDA", regular)
        self.assertEqual(len(notifications), 1)
        pre = replace(quote, session="pre", quote_time="2026-11-04 04:01:00.500")
        now = parse_us_time(pre.quote_time)
        with patch("stock_pet.models.us_now", return_value=now), patch(
            "stock_pet.models.us_session_at", return_value=us_session_at(now)
        ):
            pet._process_alert_quote("usNVDA", pre)
        self.assertEqual(len(notifications), 2)


if __name__ == "__main__":
    unittest.main()
