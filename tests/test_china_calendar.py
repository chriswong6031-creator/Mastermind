"""Tests for portfolio/china_calendar.py — mainland CN and HKEX venue support (A4b).

Existing CN calendar behaviour is preserved; new HK venue tests cover the novel
problem CN-HK-1 where the HK book ran on HKEX-only holidays (e.g. HK SAR
Establishment Day on 2026-07-01) because it was gated on the mainland A-share set.

All tests are offline, deterministic (pure-stdlib zoneinfo).
"""
from __future__ import annotations

from datetime import date, datetime

import pytest

from portfolio import china_calendar as cc


# ---------------------------------------------------------------------------
# CN calendar — existing behaviour must not regress
# ---------------------------------------------------------------------------

class TestCNCalendar:
    def test_weekends_not_trading_cn(self):
        assert cc.is_trading_day(date(2026, 6, 20)) is False   # Saturday
        assert cc.is_trading_day(date(2026, 6, 21)) is False   # Sunday

    def test_regular_weekday_is_cn_trading_day(self):
        assert cc.is_trading_day(date(2026, 6, 22)) is True    # Monday, not a holiday

    def test_cn_national_day_closed(self):
        assert cc.is_trading_day(date(2026, 10, 1)) is False

    def test_cn_spring_festival_closed(self):
        assert cc.is_trading_day(date(2026, 2, 17)) is False

    def test_cn_labour_day_closed(self):
        assert cc.is_trading_day(date(2026, 5, 1)) is False

    def test_default_venue_is_cn(self):
        """Calling is_trading_day without a venue argument defaults to CN."""
        # 2026-07-01 is a HKEX holiday (HK SAR Establishment Day) but NOT a CN holiday.
        assert cc.is_trading_day(date(2026, 7, 1)) is True            # CN open (default)
        assert cc.is_trading_day(date(2026, 7, 1), venue="CN") is True  # explicit CN also open

    def test_cn_holiday_not_in_hkex_set(self):
        """A purely CN holiday (Labour Day extension) is CN-closed but may be HK-open."""
        # 2026-05-04 and 05-05 are in the CN set (Labour Day extension) but not in HKEX.
        # Whether they are HK open or not, the CN gate must read them as closed.
        assert cc.is_trading_day(date(2026, 5, 4)) is False
        assert cc.is_trading_day(date(2026, 5, 5)) is False

    def test_is_holiday_still_works(self):
        assert cc.is_holiday(date(2026, 2, 17)) is True
        assert cc.is_holiday(date(2026, 6, 22)) is False

    def test_session_is_open_during_am(self):
        cst = cc.CST
        assert cc.is_open(datetime(2026, 6, 22, 10, 0, tzinfo=cst)) is True

    def test_session_closed_lunch(self):
        cst = cc.CST
        assert cc.is_open(datetime(2026, 6, 22, 12, 0, tzinfo=cst)) is False

    def test_previous_trading_day_cn(self):
        # 2026-02-17 (Spring Festival) is CN-closed; day before is 2026-02-13 (Fri)
        prev = cc.previous_trading_day(date(2026, 2, 18))
        assert prev == date(2026, 2, 13)   # skips Spring Festival + weekend


# ---------------------------------------------------------------------------
# HKEX venue — new behaviour (A4b fix for CN-HK-1)
# ---------------------------------------------------------------------------

class TestHKCalendar:
    def test_weekends_not_trading_hk(self):
        assert cc.is_trading_day(date(2026, 6, 20), venue="HK") is False   # Sat
        assert cc.is_trading_day(date(2026, 6, 21), venue="HK") is False   # Sun

    def test_hk_sar_day_closed_hk_only(self):
        """HK SAR Establishment Day (2026-07-01) is HKEX-closed but A-share-OPEN.
        This is the canonical CN-HK-1 regression test: before the fix the HK book
        ran on this day because it used the mainland calendar."""
        hk_sar = date(2026, 7, 1)
        assert cc.is_trading_day(hk_sar, venue="HK") is False  # HKEX closed
        assert cc.is_trading_day(hk_sar, venue="CN") is True   # A-share open

    def test_hkex_christmas_closed(self):
        """HKEX is closed on Christmas; mainland A-shares are open on 2026-12-25."""
        xmas = date(2026, 12, 25)
        assert cc.is_trading_day(xmas, venue="HK") is False
        assert cc.is_trading_day(xmas, venue="CN") is True

    def test_hkex_goodfriday_2026_closed(self):
        """Good Friday 2026 (2026-04-06) is an HKEX holiday, not a CN holiday."""
        good_friday = date(2026, 4, 6)
        assert cc.is_trading_day(good_friday, venue="HK") is False

    def test_hkex_easter_monday_2026_closed(self):
        easter_mon = date(2026, 4, 7)
        assert cc.is_trading_day(easter_mon, venue="HK") is False

    def test_shared_holiday_both_closed(self):
        """A shared holiday (Lunar New Year 2026-02-17) closes BOTH exchanges."""
        lny = date(2026, 2, 17)
        assert cc.is_trading_day(lny, venue="HK") is False
        assert cc.is_trading_day(lny, venue="CN") is False

    def test_regular_weekday_hk_open(self):
        """A plain Monday is trading at both venues."""
        monday = date(2026, 6, 22)
        assert cc.is_trading_day(monday, venue="HK") is True
        assert cc.is_trading_day(monday, venue="CN") is True

    def test_unknown_venue_falls_back_to_cn(self):
        """An unrecognised venue label degrades to the CN gate (fail-safe: coarsen, never unlock)."""
        # HK SAR Day — CN open, HK closed. An unknown venue must NOT unlock HK's closure.
        hk_sar = date(2026, 7, 1)
        # Unknown venue → behaves like CN (open on HK SAR Day).
        assert cc.is_trading_day(hk_sar, venue="UNKNOWN") is True

    def test_is_hkex_holiday_utility(self):
        """is_hkex_holiday() exposes the set directly."""
        assert cc.is_hkex_holiday(date(2026, 7, 1)) is True    # SAR Day
        assert cc.is_hkex_holiday(date(2026, 12, 25)) is True  # Christmas
        assert cc.is_hkex_holiday(date(2026, 6, 22)) is False  # regular Monday

    def test_hkex_2026_holiday_count_plausible(self):
        """A sanity check: HKEX should have between 12 and 20 unique closure weekdays in 2026
        (excluding weekends). Historical averages ~14–17 days."""
        from portfolio.china_calendar import _HKEX_HOLIDAYS
        hk_2026_weekdays = {d for d in _HKEX_HOLIDAYS
                            if d.year == 2026 and d.weekday() < 5}
        assert 10 <= len(hk_2026_weekdays) <= 22, (
            f"Unexpected HKEX 2026 closure count: {len(hk_2026_weekdays)} — "
            "check the holiday set for fat-finger errors"
        )

    def test_hk_bot_line_60_semantics(self):
        """Confirm that bot/hk.py line 60 (is_trading_day(today, venue='HK')) now correctly
        returns False on 2026-07-01 (HK SAR Establishment Day), which was the trigger date
        for the CN-HK-1 novel problem."""
        # This is the day the novel problem was discovered: the HK book ran because
        # the mainland calendar said True.  Post-fix it must say False.
        assert cc.is_trading_day(date(2026, 7, 1), venue="HK") is False


# ---------------------------------------------------------------------------
# Backwards-compatibility: every caller that uses the two-arg form with venue='CN'
# or the single-arg form must see unchanged output.
# ---------------------------------------------------------------------------

class TestBackwardsCompat:
    @pytest.mark.parametrize("d,expected", [
        (date(2026, 2, 17), False),   # Spring Festival
        (date(2026, 10, 1), False),   # National Day
        (date(2026, 6, 22), True),    # regular trading day
        (date(2026, 6, 20), False),   # Saturday
    ])
    def test_single_arg_is_unchanged(self, d, expected):
        assert cc.is_trading_day(d) is expected

    @pytest.mark.parametrize("d,expected", [
        (date(2026, 2, 17), False),
        (date(2026, 10, 1), False),
        (date(2026, 6, 22), True),
        (date(2026, 6, 20), False),
    ])
    def test_explicit_cn_matches_single_arg(self, d, expected):
        assert cc.is_trading_day(d, venue="CN") is expected
