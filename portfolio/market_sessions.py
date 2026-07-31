"""Unified exchange-session contract for dashboard live-mark scheduling.

The portfolio registry currently contains three market clocks:
  * US books (including Self-Directed): NYSE, America/New_York
  * China book: SSE/SZSE, Asia/Shanghai
  * Hong Kong book: HKEX, Asia/Hong_Kong

The underlying holiday/session authorities remain ``market_calendar`` and
``china_calendar``.  This module only normalises their output for the web client
and calculates when it should ask for another quote.  Closed markets are not
periodically polled: the next request is scheduled for the next valid session
open (including the China/HK afternoon reopen after lunch).
"""
from __future__ import annotations

import math
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

_OPEN_POLL_SECONDS = 120
_WAKE_BUFFER_SECONDS = 5

# Published shortened cash sessions. These belong to the dashboard scheduling
# layer so stale quotes are not polled after an exchange's early close.
# Sources: NYSE Holidays & Trading Hours; HKEX securities-market circulars.
_US_EARLY_CLOSES = frozenset(
    date.fromisoformat(value)
    for value in (
        "2025-07-03", "2025-11-28", "2025-12-24",
        "2026-11-27", "2026-12-24",
        "2027-11-26",
    )
)
_HK_EARLY_CLOSES = frozenset(
    date.fromisoformat(value)
    for value in (
        "2025-01-28", "2025-12-24", "2025-12-31",
        "2026-02-16", "2026-12-24", "2026-12-31",
        "2027-02-05", "2027-12-24", "2027-12-31",
    )
)


def venue_for_portfolio(portfolio_id: str | None) -> str:
    """Return the calendar venue used by a portfolio: ``US``, ``CN``, or ``HK``."""
    pid = (portfolio_id or "flagship").lower()
    if pid == "china":
        return "CN"
    if pid == "hk":
        return "HK"
    return "US"


def _closed_state(*, local_now: datetime, trading_day: bool, holiday: bool,
                  venue: str) -> str:
    if holiday:
        return "holiday"
    if not trading_day:
        return "weekend"
    current = local_now.time()
    if venue == "US":
        return "pre_open" if current < time(9, 30) else "post_close"
    lunch_start = time(12, 0) if venue == "HK" else time(11, 30)
    if lunch_start <= current < time(13, 0):
        return "lunch_break"
    return "pre_open" if current < time(9, 30) else "post_close"


def status_for_portfolio(portfolio_id: str | None = None,
                         now: datetime | None = None) -> dict[str, Any]:
    """Return a display- and scheduling-ready session snapshot.

    ``poll_after_seconds`` is 120 while open.  While closed it is the exact
    interval to the next calendar open plus a small buffer, so the browser does
    not burn requests overnight or on holidays.
    """
    venue = venue_for_portfolio(portfolio_id)
    if venue == "US":
        from portfolio import market_calendar as calendar

        zone = ZoneInfo("America/New_York")
        raw = calendar.status(now)
        local_now = datetime.fromisoformat(raw["asof_et"])
        market_name = "NYSE"
        timezone_name = "America/New_York"
    else:
        from portfolio import china_calendar as calendar

        zone = ZoneInfo("Asia/Hong_Kong" if venue == "HK" else "Asia/Shanghai")
        raw = calendar.status(now, venue=venue)
        # HK and Shanghai currently share UTC+8, but expose the actual exchange
        # timezone in the public contract rather than relying on that coincidence.
        local_now = datetime.fromisoformat(raw["asof"]).astimezone(zone)
        market_name = "HKEX" if venue == "HK" else "SSE/SZSE"
        timezone_name = "Asia/Hong_Kong" if venue == "HK" else "Asia/Shanghai"

    next_open = datetime.fromisoformat(raw["next_open"]).astimezone(zone)
    early_close = (
        (venue == "US" and local_now.date() in _US_EARLY_CLOSES)
        or (venue == "HK" and local_now.date() in _HK_EARLY_CLOSES)
    )
    close_time = (
        (time(13, 0) if early_close else time(16, 0)) if venue == "US"
        else (time(12, 0) if early_close else time(16, 0)) if venue == "HK"
        else time(15, 0)
    )
    open_now = bool(raw["open"]) and not (
        early_close and local_now.time() >= close_time)
    if open_now:
        state = "open"
    elif early_close and bool(raw["trading_day"]) and local_now.time() >= close_time:
        state = "early_close"
        # HK's underlying two-session calendar sees 12:00–13:00 as lunch and
        # would otherwise schedule a wake at 13:00 on a no-afternoon-session day.
        if venue == "HK":
            following = calendar.next_trading_day(local_now.date(), venue="HK")
            next_open = datetime.combine(following, time(9, 30), tzinfo=zone)
    else:
        state = _closed_state(
            local_now=local_now,
            trading_day=bool(raw["trading_day"]),
            holiday=bool(raw["holiday"]),
            venue=venue,
        )
    if open_now:
        poll_after = _OPEN_POLL_SECONDS
    else:
        seconds = (next_open - local_now).total_seconds()
        poll_after = max(30, math.ceil(seconds) + _WAKE_BUFFER_SECONDS)

    return {
        "venue": venue,
        "market": market_name,
        "timezone": timezone_name,
        "is_open": open_now,
        "state": state,
        "trading_day": bool(raw["trading_day"]),
        "holiday": bool(raw["holiday"]),
        "early_close": early_close,
        "as_of": local_now.isoformat(),
        "session_close": datetime.combine(
            local_now.date(), close_time, tzinfo=zone).isoformat(),
        "next_open": next_open.isoformat(),
        "poll_after_seconds": poll_after,
    }
