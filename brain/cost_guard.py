"""NIGHTLY Opus-cost TRIPWIRE — a per-(book, date) cumulative LLM-cost ledger + budget gate.

The desk runs several armed Opus seats per night (the per-book Brains via ``cli_bridge.reason``,
the Flagship armed PM via ``pm_conviction.build_book``), each ~$1+. With no ceiling a runaway
night (retries, a stuck loop, an operator firing every book) can quietly burn real money. This
module is the spend governor:

  * RECORD — after every Opus call whose cost is known, ``record(book, usd, asof)`` accumulates
    that book's running total for the day, persisted to ``data/brain/cost/<asof>.json``.
  * READ — ``spent(book, asof)`` and ``summary(asof)`` expose the running spend for the dashboard.
  * GATE — BEFORE an expensive seat runs, ``over_budget(book, asof)`` says whether the book has
    hit the configured cap; if so the caller SKIPS the seat and falls back to the cheap/engine
    path (mirrors the feed-health abort pattern in bot/china.py).

DEFAULT OFF. The cap is env ``MASTERMIND_NIGHTLY_USD_CAP`` (default 0.0 = OFF = unlimited). When
OFF, ``over_budget`` is ALWAYS False → no seat is ever skipped → the desk is BYTE-IDENTICAL to
today. Recording still happens (it never gates anything on its own), so spend is observable the
moment the operator sets a cap.

Pure + defensive: every public function swallows its own errors and returns a safe default —
the cost ledger must NEVER be able to break a nightly run or change sizing/trading.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_DIR = _ROOT / "data" / "brain" / "cost"

_CAP_ENV = "MASTERMIND_NIGHTLY_USD_CAP"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _asof(asof: str | None) -> str:
    """Normalise an as-of key to an ISO date string (today when unset/garbage)."""
    if asof:
        return str(asof)[:10]
    try:
        return date.today().isoformat()
    except Exception:  # noqa: BLE001 — never raise from a defensive ledger
        return "unknown"


def _path(asof: str | None) -> Path:
    return _DIR / f"{_asof(asof)}.json"


def _load(asof: str | None) -> dict:
    """The {book: spent_usd} map for a date — {} on any miss. Never raises."""
    p = _path(asof)
    try:
        if p.exists():
            d = json.loads(p.read_text())
            return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _book(book: str | None) -> str:
    return str(book or "").strip().lower() or "unknown"


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def cap() -> float:
    """The nightly per-book USD cap from env ``MASTERMIND_NIGHTLY_USD_CAP``.

    <= 0 (the default, or unset/garbage) means OFF / unlimited."""
    try:
        return float(os.environ.get(_CAP_ENV, "0") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def record(book: str, usd, asof: str | None = None) -> None:
    """Add ``usd`` to ``book``'s running spend for ``asof`` (default today). Idempotent-safe
    accumulation: each call adds the cost of one completed Opus seat, persisted immediately so a
    crash mid-night doesn't lose the tally. A None / non-numeric / non-positive cost is ignored.
    Never raises — the recorder must not be able to break a nightly run."""
    try:
        amt = float(usd or 0.0)
    except (TypeError, ValueError):
        return
    if amt <= 0:
        return
    try:
        b = _book(book)
        cur = _load(asof)
        try:
            prev = float(cur.get(b) or 0.0)
        except (TypeError, ValueError):
            prev = 0.0
        cur[b] = round(prev + amt, 6)
        _DIR.mkdir(parents=True, exist_ok=True)
        _path(asof).write_text(json.dumps(cur, sort_keys=True))
    except Exception:  # noqa: BLE001 — defensive; never raise
        pass


def spent(book: str, asof: str | None = None) -> float:
    """The cumulative USD recorded for ``book`` on ``asof`` (default today). 0.0 on any miss."""
    try:
        v = _load(asof).get(_book(book))
        return float(v or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0


def over_budget(book: str, asof: str | None = None) -> bool:
    """True when ``book`` has reached/exceeded the nightly cap on ``asof``.

    ALWAYS False when the cap is OFF (<= 0 — the default), so with no cap configured this is a
    no-op and the desk behaves byte-identically to today. Never raises."""
    try:
        c = cap()
        if c <= 0:
            return False
        return spent(book, asof) >= c
    except Exception:  # noqa: BLE001
        return False


def summary(asof: str | None = None) -> dict:
    """The nightly spend picture for the dashboard:

        {"asof", "cap", "enabled", "total", "books": {book: {"spent", "over"}}}

    ``enabled`` reflects whether the cap is armed (cap > 0). Never raises."""
    a = _asof(asof)
    c = cap()
    enabled = c > 0
    books: dict[str, dict] = {}
    total = 0.0
    try:
        for b, v in (_load(asof) or {}).items():
            try:
                s = float(v or 0.0)
            except (TypeError, ValueError):
                s = 0.0
            total += s
            books[b] = {"spent": round(s, 4), "over": bool(enabled and s >= c)}
    except Exception:  # noqa: BLE001
        books = {}
        total = 0.0
    return {
        "asof": a,
        "cap": round(c, 4),
        "enabled": enabled,
        "total": round(total, 4),
        "books": books,
    }
