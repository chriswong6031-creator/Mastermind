"""Market-calendar unit tests — offline, deterministic, no network.

The decisive scenario for this build: it is Sunday 2026-06-21; Friday 2026-06-19
was the Juneteenth holiday; the desk has been shut since the Thursday 06-18
close and the next open is Monday 2026-06-22 09:30 ET.
"""
from __future__ import annotations

from datetime import date, datetime

from zoneinfo import ZoneInfo

from portfolio import market_calendar as mc

ET = ZoneInfo("America/New_York")


def test_weekend_is_closed() -> None:
    assert mc.is_open(datetime(2026, 6, 20, 12, 0, tzinfo=ET)) is False  # Sat
    assert mc.is_open(datetime(2026, 6, 21, 12, 0, tzinfo=ET)) is False  # Sun
    assert mc.is_trading_day(date(2026, 6, 20)) is False
    assert mc.is_trading_day(date(2026, 6, 21)) is False


def test_juneteenth_holiday_closed() -> None:
    assert mc.is_holiday(date(2026, 6, 19)) is True
    assert mc.is_trading_day(date(2026, 6, 19)) is False
    # noon on the holiday — still closed despite being a weekday at session time
    assert mc.is_open(datetime(2026, 6, 19, 12, 0, tzinfo=ET)) is False


def test_regular_session_hours() -> None:
    # Monday 2026-06-22 is a normal session
    assert mc.is_trading_day(date(2026, 6, 22)) is True
    assert mc.is_open(datetime(2026, 6, 22, 9, 29, tzinfo=ET)) is False   # pre-open
    assert mc.is_open(datetime(2026, 6, 22, 9, 30, tzinfo=ET)) is True    # the open
    assert mc.is_open(datetime(2026, 6, 22, 12, 0, tzinfo=ET)) is True
    assert mc.is_open(datetime(2026, 6, 22, 15, 59, tzinfo=ET)) is True
    assert mc.is_open(datetime(2026, 6, 22, 16, 0, tzinfo=ET)) is False   # the close
    assert mc.is_open(datetime(2026, 6, 22, 18, 0, tzinfo=ET)) is False   # after hours


def test_next_open_from_weekend_after_holiday() -> None:
    # Sunday afternoon -> next open is Monday 06-22 09:30 ET
    nxt = mc.next_open(datetime(2026, 6, 21, 14, 0, tzinfo=ET))
    assert nxt == datetime(2026, 6, 22, 9, 30, tzinfo=ET)
    assert mc.next_open_day(datetime(2026, 6, 21, 14, 0, tzinfo=ET)) == date(2026, 6, 22)


def test_next_open_premarket_same_day() -> None:
    # 8am on a trading day -> today's 09:30 open
    nxt = mc.next_open(datetime(2026, 6, 22, 8, 0, tzinfo=ET))
    assert nxt == datetime(2026, 6, 22, 9, 30, tzinfo=ET)


def test_next_open_after_close_rolls_forward() -> None:
    # 5pm Thursday 06-18 -> skips Fri holiday + weekend -> Mon 06-22
    nxt = mc.next_open(datetime(2026, 6, 18, 17, 0, tzinfo=ET))
    assert nxt == datetime(2026, 6, 22, 9, 30, tzinfo=ET)


def test_previous_trading_day_skips_holiday_and_weekend() -> None:
    # Before Monday 06-22 the prior session is Thursday 06-18 (skip Fri/Sat/Sun)
    assert mc.previous_trading_day(date(2026, 6, 22)) == date(2026, 6, 18)


def test_naive_datetime_assumed_et() -> None:
    # naive input is treated as ET, not UTC
    assert mc.is_open(datetime(2026, 6, 22, 12, 0)) is True
    assert mc.is_open(datetime(2026, 6, 21, 12, 0)) is False
