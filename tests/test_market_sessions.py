"""Unified live-mark session scheduling is exchange- and holiday-aware."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from portfolio import market_sessions


def test_us_open_polls_every_two_minutes() -> None:
    now = datetime(2026, 6, 22, 10, 0, tzinfo=ZoneInfo("America/New_York"))
    status = market_sessions.status_for_portfolio("flagship", now)
    assert status["market"] == "NYSE"
    assert status["timezone"] == "America/New_York"
    assert status["is_open"] is True
    assert status["state"] == "open"
    assert status["poll_after_seconds"] == 120


def test_us_holiday_sleeps_until_next_valid_open() -> None:
    now = datetime(2026, 6, 19, 12, 0, tzinfo=ZoneInfo("America/New_York"))
    status = market_sessions.status_for_portfolio("self_directed", now)
    assert status["holiday"] is True
    assert status["state"] == "holiday"
    assert status["next_open"].startswith("2026-06-22T09:30:00")
    assert status["poll_after_seconds"] > 2 * 24 * 60 * 60


def test_china_lunch_sleeps_until_afternoon_reopen() -> None:
    now = datetime(2026, 6, 22, 12, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
    status = market_sessions.status_for_portfolio("china", now)
    assert status["market"] == "SSE/SZSE"
    assert status["state"] == "lunch_break"
    assert status["next_open"].startswith("2026-06-22T13:00:00")
    assert 44 * 60 <= status["poll_after_seconds"] <= 46 * 60


def test_hk_uses_hkex_holiday_not_mainland_calendar() -> None:
    now = datetime(2026, 7, 1, 10, 0, tzinfo=ZoneInfo("Asia/Hong_Kong"))
    status = market_sessions.status_for_portfolio("hk", now)
    assert status["market"] == "HKEX"
    assert status["timezone"] == "Asia/Hong_Kong"
    assert status["holiday"] is True
    assert status["is_open"] is False
    assert status["state"] == "holiday"


def test_us_book_is_open_at_same_instant_china_is_closed() -> None:
    instant = datetime(2026, 6, 22, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    assert market_sessions.status_for_portfolio("autonomous", instant)["is_open"] is True
    assert market_sessions.status_for_portfolio("china", instant)["is_open"] is False


def test_nyse_early_close_stops_polling_after_one_pm() -> None:
    now = datetime(2026, 11, 27, 13, 30, tzinfo=ZoneInfo("America/New_York"))
    status = market_sessions.status_for_portfolio("flagship", now)
    assert status["early_close"] is True
    assert status["session_close"].startswith("2026-11-27T13:00:00")
    assert status["is_open"] is False
    assert status["state"] == "early_close"
    assert status["next_open"].startswith("2026-11-30T09:30:00")


def test_hkex_half_day_has_no_afternoon_polling() -> None:
    zone = ZoneInfo("Asia/Hong_Kong")
    morning = market_sessions.status_for_portfolio(
        "hk", datetime(2026, 12, 24, 11, 0, tzinfo=zone))
    assert morning["is_open"] is True
    assert morning["early_close"] is True
    assert morning["session_close"].startswith("2026-12-24T12:00:00")

    closed = market_sessions.status_for_portfolio(
        "hk", datetime(2026, 12, 24, 12, 30, tzinfo=zone))
    assert closed["is_open"] is False
    assert closed["state"] == "early_close"
    assert closed["next_open"].startswith("2026-12-28T09:30:00")


def test_published_2026_china_and_hk_holiday_corrections() -> None:
    from portfolio import china_calendar

    # SSE's published 2026 schedule reopens after these holiday blocks.
    assert china_calendar.is_trading_day(
        datetime(2026, 2, 24).date(), venue="CN") is True
    assert china_calendar.is_trading_day(
        datetime(2026, 10, 8).date(), venue="CN") is True
    # HKEX observes Chung Yeung on Oct 19, not the earlier projected Oct 5.
    assert china_calendar.is_trading_day(
        datetime(2026, 10, 5).date(), venue="HK") is True
    assert china_calendar.is_trading_day(
        datetime(2026, 10, 19).date(), venue="HK") is False
