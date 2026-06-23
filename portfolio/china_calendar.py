"""China (Greater-China) equity market calendar — when the China book may trade.

The all-China book holds mainland A-shares (Shanghai/Shenzhen), Hong Kong names, and
US-listed China ADRs. Those three venues keep different hours and holidays; rather than
model all three, this module is anchored to the **mainland A-share session** (the binding,
earliest-closing venue, Asia/Shanghai) because the book runs ONCE per Asia trading day
after that close. HK overlaps it; the ADR leg trades the US session and is marked at the
US close — both are close enough for a once-daily paper rebalance.

Mirrors the API of ``portfolio.market_calendar`` (is_trading_day / is_open / next_open /
status / trading-day arithmetic) so the builder and the pending-order lifecycle can use it
interchangeably, scoped by portfolio.

Pure-stdlib (zoneinfo), offline, deterministic. The A-share session is two halves
(09:30–11:30 and 13:00–15:00 China Standard Time, Mon–Fri). Holidays are a best-effort
SSE/SZSE closure set — the Lunar-calendar holidays SHIFT each year and the exchanges publish
the official calendar only ~a year ahead, so the forward years are approximate; refresh them
from the official SSE/SZSE notice as each year is published. Weekends are always closed, and
the set never *opens* a session that should be shut.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")          # China Standard Time (UTC+8, no DST)

# A-share regular cash session, two halves (China Standard Time).
_AM_OPEN = time(9, 30)
_AM_CLOSE = time(11, 30)
_PM_OPEN = time(13, 0)
_PM_CLOSE = time(15, 0)

# SSE/SZSE full-day closures (best-effort; Lunar holidays are approximate in forward years —
# refresh from the official exchange calendar yearly). Covers the live track's near lifetime.
_HOLIDAYS: frozenset[date] = frozenset(
    date.fromisoformat(s)
    for s in (
        # 2025 (official SSE/SZSE)
        "2025-01-01",                                                   # New Year
        "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31",         # Spring Festival
        "2025-02-03", "2025-02-04",
        "2025-04-04",                                                   # Qingming
        "2025-05-01", "2025-05-02", "2025-05-05",                       # Labour Day
        "2025-06-02",                                                   # Dragon Boat
        "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06",         # Nat'l Day + Mid-Autumn
        "2025-10-07", "2025-10-08",
        # 2026 (approximate — Lunar holidays projected; refresh when SSE publishes)
        "2026-01-01",                                                   # New Year
        "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",         # Spring Festival (CNY 2026-02-17)
        "2026-02-20", "2026-02-23", "2026-02-24",
        "2026-04-06",                                                   # Qingming (obs)
        "2026-05-01", "2026-05-04", "2026-05-05",                       # Labour Day
        "2026-06-19",                                                   # Dragon Boat
        "2026-09-25",                                                   # Mid-Autumn
        "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",         # National Day
        "2026-10-07", "2026-10-08",
        # 2027 (approximate)
        "2027-01-01",                                                   # New Year
        "2027-02-08", "2027-02-09", "2027-02-10", "2027-02-11",         # Spring Festival (CNY 2027-02-06)
        "2027-02-12", "2027-02-15", "2027-02-16",
        "2027-04-05",                                                   # Qingming
        "2027-05-03", "2027-05-04", "2027-05-05",                       # Labour Day
        "2027-06-09",                                                   # Dragon Boat
        "2027-09-15",                                                   # Mid-Autumn
        "2027-10-01", "2027-10-04", "2027-10-05", "2027-10-06",         # National Day
        "2027-10-07", "2027-10-08",
    )
)


def is_holiday(d: date) -> bool:
    return d in _HOLIDAYS


def is_trading_day(d: date) -> bool:
    """True for a regular A-share session day (weekday, not an exchange holiday)."""
    return d.weekday() < 5 and d not in _HOLIDAYS


def _now_cst(now: datetime | None) -> datetime:
    """Coerce `now` to a China-time aware datetime (naive is assumed CST)."""
    if now is None:
        return datetime.now(CST)
    if now.tzinfo is None:
        return now.replace(tzinfo=CST)
    return now.astimezone(CST)


def is_open(now: datetime | None = None) -> bool:
    """True iff a mainland A-share session half is open right now (CST)."""
    cst = _now_cst(now)
    if not is_trading_day(cst.date()):
        return False
    t = cst.time()
    return (_AM_OPEN <= t < _AM_CLOSE) or (_PM_OPEN <= t < _PM_CLOSE)


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
    """The next moment a session opens: today's morning open if still pre-market, the same-day
    13:00 afternoon reopen during the 11:30–13:00 lunch break, else the next trading day's 09:30."""
    cst = _now_cst(now)
    today = cst.date()
    if is_trading_day(today):
        t = cst.time()
        if t < _AM_OPEN:
            return datetime.combine(today, _AM_OPEN, tzinfo=CST)
        if _AM_CLOSE <= t < _PM_OPEN:                 # lunch break — the next open is this afternoon
            return datetime.combine(today, _PM_OPEN, tzinfo=CST)
    nxt = next_trading_day(today)
    return datetime.combine(nxt, _AM_OPEN, tzinfo=CST)


def next_open_day(now: datetime | None = None) -> date:
    """Calendar date of the next session open (for display: 'fills at <date>')."""
    return next_open(now).date()


def status(now: datetime | None = None) -> dict:
    """A compact, display-ready snapshot of where we are in the China calendar."""
    cst = _now_cst(now)
    open_now = is_open(cst)
    nxt = next_open(cst)
    return {
        "open": open_now,
        "venue": "A-share (Shanghai/Shenzhen)",
        "asof": cst.isoformat(),        # stable key matching no-suffix consumers
        "asof_cst": cst.isoformat(),
        "trading_day": is_trading_day(cst.date()),
        "holiday": is_holiday(cst.date()),
        "next_open": nxt.isoformat(),
        "next_open_day": nxt.date().isoformat(),
        "prev_trading_day": previous_trading_day(cst.date()).isoformat(),
    }
