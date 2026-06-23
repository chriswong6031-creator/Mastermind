"""US equity market calendar — the single authority for *when* the bot may trade.

Doctrine: the paper book only transacts during regular US cash-session hours
(09:30–16:00 America/New_York, Mon–Fri, excluding exchange holidays), at the
live market price. Anything decided while the market is CLOSED is queued as a
PENDING order to be filled at the next open — never booked as an instantaneous
fill at a stale close. This module owns the open/closed test and the trading-day
arithmetic; `paper_account` owns the pending-order lifecycle that depends on it.

Pure-stdlib (zoneinfo), offline, deterministic. Holidays are a hard-coded NYSE
set for 2025–2027 (the live track's lifetime); extend as years roll forward.
Half-day early closes are treated as regular sessions — close enough for a
paper sim and never *opens* a session that should be shut.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Regular cash session, Eastern time.
_OPEN = time(9, 30)
_CLOSE = time(16, 0)

# NYSE full-day closures (observed dates). Juneteenth 2026-06-19 is the holiday
# that, with the weekend, keeps the desk shut Fri–Sun before the 2026-06-22 open.
_HOLIDAYS: frozenset[date] = frozenset(
    date.fromisoformat(s)
    for s in (
        # 2025
        "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
        "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
        # 2026
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
        "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
        # 2027
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
        "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
    )
)


def is_holiday(d: date) -> bool:
    return d in _HOLIDAYS


def is_trading_day(d: date) -> bool:
    """True for a regular NYSE session day (weekday, not a holiday)."""
    return d.weekday() < 5 and d not in _HOLIDAYS


def _now_et(now: datetime | None) -> datetime:
    """Coerce `now` to an Eastern-time aware datetime (naive is assumed ET)."""
    if now is None:
        return datetime.now(ET)
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def is_open(now: datetime | None = None) -> bool:
    """True iff the US cash session is open right now (regular hours, ET)."""
    et = _now_et(now)
    if not is_trading_day(et.date()):
        return False
    return _OPEN <= et.time() < _CLOSE


def previous_trading_day(d: date) -> date:
    """The most recent trading day strictly before `d`."""
    cur = d - timedelta(days=1)
    while not is_trading_day(cur):
        cur -= timedelta(days=1)
    return cur


def next_trading_day(d: date) -> date:
    """The next trading day strictly after `d`."""
    cur = d + timedelta(days=1)
    while not is_trading_day(cur):
        cur += timedelta(days=1)
    return cur


def next_open(now: datetime | None = None) -> datetime:
    """The next moment the session opens (today's open if still pre-market, else
    the next trading day's 09:30 ET)."""
    et = _now_et(now)
    today = et.date()
    if is_trading_day(today) and et.time() < _OPEN:
        return datetime.combine(today, _OPEN, tzinfo=ET)
    nxt = next_trading_day(today)
    return datetime.combine(nxt, _OPEN, tzinfo=ET)


def next_open_day(now: datetime | None = None) -> date:
    """Calendar date of the next session open (for display: 'fills at <date>')."""
    return next_open(now).date()


def status(now: datetime | None = None) -> dict:
    """A compact, display-ready snapshot of where we are in the calendar."""
    et = _now_et(now)
    open_now = is_open(et)
    nxt = next_open(et)
    return {
        "open": open_now,
        "asof_et": et.isoformat(),
        "trading_day": is_trading_day(et.date()),
        "holiday": is_holiday(et.date()),
        "next_open": nxt.isoformat(),
        "next_open_day": nxt.date().isoformat(),
        "prev_trading_day": previous_trading_day(et.date()).isoformat(),
    }
