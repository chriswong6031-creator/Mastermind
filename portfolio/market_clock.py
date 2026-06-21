"""US equity market clock — is the regular session open right now?

Drives the Self-Directed book's order routing: an order placed while the market is
OPEN fills immediately at the live price; one placed while it is CLOSED is queued
PENDING and fills at the next session's OPEN price.

Deliberately dependency-free: US-Eastern wall-clock is computed from UTC with a manual
DST rule (no `zoneinfo`/tzdata required, so it can't crash on a minimal host). Regular
session = 09:30–16:00 ET, Mon–Fri, excluding the NYSE full-day holidays below. Early-close
days (e.g. the day after Thanksgiving) are treated as normal sessions — the 1pm close is
immaterial to this paper book's EOD-grade marks. Every public fn accepts an optional `now`
(a naive ET datetime) so callers/tests can pin the clock.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

# NYSE full-day closures. Best-effort, extend as needed — beyond the listed years the
# weekend check still applies, so the only miss is a paper fill on a low-volume holiday.
_HOLIDAYS: set[str] = {
    # 2025
    "2025-01-01", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    # 2026
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25",
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    # 2027
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
    "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}

_OPEN = time(9, 30)
_CLOSE = time(16, 0)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th `weekday` (Mon=0 … Sun=6) of (year, month)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _dst_active(d: date) -> bool:
    """US DST: 2nd Sunday of March 02:00 through 1st Sunday of November 02:00."""
    start = _nth_weekday(d.year, 3, 6, 2)   # 2nd Sunday in March
    end = _nth_weekday(d.year, 11, 6, 1)    # 1st Sunday in November
    return start <= d < end


def now_et() -> datetime:
    """Current US-Eastern wall-clock as a naive datetime (no tz database needed)."""
    utc = datetime.now(timezone.utc)
    off = 4 if _dst_active(utc.date()) else 5   # EDT = UTC-4, EST = UTC-5
    return (utc - timedelta(hours=off)).replace(tzinfo=None)


def is_holiday(d: date) -> bool:
    return d.isoformat() in _HOLIDAYS


def is_session_day(d: date) -> bool:
    """A weekday that is not an NYSE holiday."""
    return d.weekday() < 5 and not is_holiday(d)


def is_open(now: datetime | None = None) -> bool:
    """True iff the regular session is open at `now` (defaults to live ET)."""
    now = now or now_et()
    return is_session_day(now.date()) and _OPEN <= now.time() < _CLOSE


def next_open(now: datetime | None = None) -> datetime:
    """The next regular-session open at/after `now` (09:30 ET on the next session day)."""
    now = now or now_et()
    today_open = datetime.combine(now.date(), _OPEN)
    if is_session_day(now.date()) and now < today_open:
        return today_open
    d = now.date() + timedelta(days=1)
    while not is_session_day(d):
        d += timedelta(days=1)
    return datetime.combine(d, _OPEN)


def status(now: datetime | None = None) -> dict:
    """Market-state contract for the UI / order router.

    session ∈ {'open','pre','post','closed'}:
      open   — regular session in progress (orders fill now)
      pre    — session day before 09:30 (orders queue → fill at this open)
      post   — session day after 16:00 (orders queue → next session open)
      closed — weekend / holiday (orders queue → next session open)
    """
    now = now or now_et()
    sd = is_session_day(now.date())
    if sd and _OPEN <= now.time() < _CLOSE:
        session = "open"
    elif sd and now.time() < _OPEN:
        session = "pre"
    elif sd:
        session = "post"
    else:
        session = "closed"
    return {
        "is_open": session == "open",
        "session": session,
        "now_et": now.isoformat(timespec="minutes"),
        "next_open": next_open(now).isoformat(timespec="minutes"),
    }
