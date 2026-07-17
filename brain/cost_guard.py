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

SCHEMA (data/brain/cost/<asof>.json) — NEW shape (written after the first new-API record call):
  {
    "<book>": {
      "usd": float,
      "input_tokens": int,
      "output_tokens": int,
      "cache_read_tokens": int,
      "cache_creation_tokens": int,
      "calls": int,
      "seats": {
        "<seat>": {
          "usd": float,
          "input_tokens": int,
          "output_tokens": int,
          "calls": int
        }
      }
    }
  }

LEGACY shape (float-only per-book): ``{"<book>": <float>}`` — read transparently, upgraded
to the new shape on first new-API ``record()`` write.
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
# Per-MTok pricing table (USD) — used by brain/client.py to estimate cost from
# resp.usage when the Messages API does not return a cost field.
# Format: model_id -> {"input": $/MTok, "output": $/MTok,
#                      "cache_read": $/MTok, "cache_write": $/MTok}
# cache_read / cache_write are relative factors applied to the INPUT price.
# ---------------------------------------------------------------------------
PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-8":    {"input": 5.00,  "output": 25.00, "cache_read": 0.50,  "cache_write": 6.25},
    "claude-sonnet-4-6":  {"input": 3.00,  "output": 15.00, "cache_read": 0.30,  "cache_write": 3.75},
    "claude-haiku-4-5":   {"input": 1.00,  "output":  5.00, "cache_read": 0.10,  "cache_write": 1.25},
}
# cache_read = 0.10x input price; cache_write = 1.25x input price (per-MTok)

# Model alias normalisation — map common partial names to the canonical key above.
_MODEL_ALIASES: dict[str, str] = {
    "opus":   "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku":  "claude-haiku-4-5",
}


def _normalize_model(raw: str) -> str:
    """Normalize a raw model string for PRICING lookup.

    Steps:
      1. Strip leading provider prefix (e.g. 'us.anthropic.' or 'anthropic.').
      2. Strip a trailing bracket suffix like '[1m]' or a colon-variant suffix
         like ':thinking'.
      3. Lowercase + strip whitespace.

    Returns the normalized string ready for PRICING / _MODEL_ALIASES lookup.
    Does not raise.
    """
    try:
        m = str(raw or "").strip().lower()
        # strip provider prefix (e.g. 'us.anthropic.', 'anthropic.')
        for pfx in ("us.anthropic.", "anthropic."):
            if m.startswith(pfx):
                m = m[len(pfx):]
                break
        # strip bracket suffix e.g. '[1m]', '[3]' and colon-variant e.g. ':thinking'
        for sep in ("[", ":"):
            if sep in m:
                m = m[:m.index(sep)]
        return m.strip()
    except Exception:  # noqa: BLE001
        return ""


def estimate_cost(model: str | None, input_tokens: int, output_tokens: int,
                  cache_read_tokens: int = 0, cache_creation_tokens: int = 0) -> float:
    """Estimate USD cost from token counts using the PRICING table.

    Falls back to 0.0 when the model is unknown or None/empty (never raises).

    Model string is normalized before lookup:
      - provider prefixes stripped ('us.anthropic.', 'anthropic.')
      - bracket/colon variant suffixes stripped ('[1m]', ':thinking', etc.)
      - longest-prefix match against PRICING keys as final fallback
      - unknown model (including empty / None) returns 0.0
    """
    try:
        if not model and model != 0:
            return 0.0
        m = _normalize_model(model)
        if not m:
            return 0.0
        # exact lookup
        if m not in PRICING:
            m = _MODEL_ALIASES.get(m, m)
        if m not in PRICING:
            # longest-prefix match — e.g. 'claude-sonnet-4-6-20990101' -> 'claude-sonnet-4-6'
            best_key = None
            best_len = 0
            for key in PRICING:
                if m.startswith(key) and len(key) > best_len:
                    best_key = key
                    best_len = len(key)
            if best_key:
                m = best_key
        p = PRICING.get(m)
        if p is None:
            return 0.0
        cost = (
            (int(input_tokens or 0) * p["input"]
             + int(output_tokens or 0) * p["output"]
             + int(cache_read_tokens or 0) * p["cache_read"]
             + int(cache_creation_tokens or 0) * p["cache_write"])
            / 1_000_000
        )
        return round(cost, 8)
    except Exception:  # noqa: BLE001
        return 0.0


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
    """The per-book map for a date — {} on any miss. Never raises.

    Returns the raw dict from disk; values may be legacy floats or new-schema dicts.
    """
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


def _seat(seat: str | None) -> str:
    return str(seat or "").strip().lower() or "unknown"


def _book_usd(v) -> float:
    """Extract the USD float from a book entry (legacy float OR new-schema dict)."""
    if isinstance(v, (int, float)):
        try:
            return float(v)
        except Exception:  # noqa: BLE001
            return 0.0
    if isinstance(v, dict):
        try:
            return float(v.get("usd") or 0.0)
        except Exception:  # noqa: BLE001
            return 0.0
    return 0.0


def _ensure_book_dict(cur_val) -> dict:
    """Upgrade a legacy float entry (or missing) to a new-schema dict. Never raises."""
    if isinstance(cur_val, dict):
        return cur_val
    usd = 0.0
    if isinstance(cur_val, (int, float)):
        try:
            usd = float(cur_val)
        except Exception:  # noqa: BLE001
            pass
    return {
        "usd": usd,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "calls": 0,
        "seats": {},
    }


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


def record(book: str, usd, asof: str | None = None, *,
           seat: str | None = None, model: str | None = None,
           input_tokens: int = 0, output_tokens: int = 0,
           cache_read_tokens: int = 0, cache_creation_tokens: int = 0) -> None:
    """Add ``usd`` to ``book``'s running spend for ``asof`` (default today).

    Optional keyword args enable per-seat, per-model, and per-token-type granularity:
      seat                  — the Brain seat name (e.g. 'strategist', 'gate_officer')
      model                 — the model id (e.g. 'claude-opus-4-8')
      input_tokens          — prompt token count for this call
      output_tokens         — completion token count for this call
      cache_read_tokens     — cache-read token count for this call
      cache_creation_tokens — cache-write token count for this call

    A None / non-numeric / non-positive cost is ignored (the USD accumulation is skipped, but
    token counts ARE recorded so the ledger stays accurate even when cost is unavailable).
    Never raises — the recorder must not be able to break a nightly run.
    """
    try:
        amt = float(usd or 0.0)
    except (TypeError, ValueError):
        amt = 0.0

    # Normalise token counts — non-integer or negative → 0
    def _tok(v) -> int:
        try:
            r = int(v or 0)
            return r if r >= 0 else 0
        except (TypeError, ValueError):
            return 0

    itok = _tok(input_tokens)
    otok = _tok(output_tokens)
    crtok = _tok(cache_read_tokens)
    cctok = _tok(cache_creation_tokens)
    has_tokens = itok + otok + crtok + cctok > 0

    if amt <= 0 and not has_tokens:
        return

    try:
        b = _book(book)
        cur = _load(asof)
        bk = _ensure_book_dict(cur.get(b))

        # accumulate book-level totals
        if amt > 0:
            bk["usd"] = round(float(bk.get("usd") or 0.0) + amt, 6)
        bk["input_tokens"] = int(bk.get("input_tokens") or 0) + itok
        bk["output_tokens"] = int(bk.get("output_tokens") or 0) + otok
        bk["cache_read_tokens"] = int(bk.get("cache_read_tokens") or 0) + crtok
        bk["cache_creation_tokens"] = int(bk.get("cache_creation_tokens") or 0) + cctok
        bk["calls"] = int(bk.get("calls") or 0) + 1

        # per-seat breakdown
        if seat:
            s = _seat(seat)
            seats = bk.setdefault("seats", {})
            if not isinstance(seats, dict):
                seats = {}
                bk["seats"] = seats
            se = seats.get(s) or {}
            if not isinstance(se, dict):
                se = {}
            if amt > 0:
                se["usd"] = round(float(se.get("usd") or 0.0) + amt, 6)
            se["input_tokens"] = int(se.get("input_tokens") or 0) + itok
            se["output_tokens"] = int(se.get("output_tokens") or 0) + otok
            se["calls"] = int(se.get("calls") or 0) + 1
            seats[s] = se

        cur[b] = bk
        _DIR.mkdir(parents=True, exist_ok=True)
        _path(asof).write_text(json.dumps(cur, sort_keys=True))
    except Exception:  # noqa: BLE001 — defensive; never raise
        pass


def spent(book: str, asof: str | None = None) -> float:
    """The cumulative USD recorded for ``book`` on ``asof`` (default today). 0.0 on any miss."""
    try:
        return _book_usd(_load(asof).get(_book(book)))
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

    ``enabled`` reflects whether the cap is armed (cap > 0). Never raises.
    The return shape is IDENTICAL to the legacy contract (``books[book].spent`` / ``.over``)
    so /api/desk/scorecard callers are unaffected by the new schema on disk.
    """
    a = _asof(asof)
    c = cap()
    enabled = c > 0
    books: dict[str, dict] = {}
    total = 0.0
    try:
        for b, v in (_load(asof) or {}).items():
            try:
                s = _book_usd(v)
            except Exception:  # noqa: BLE001
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


# ---------------------------------------------------------------------------
# 30-day rolling cost summary (exported to vendor/macro)
# ---------------------------------------------------------------------------

def build_cost_summary(asof: str | None = None) -> dict:
    """Build a rolling 30-day cost summary from data/brain/cost/*.json.

    Schema: mastermind.cost_summary.v1
    {
      "schema": "mastermind.cost_summary.v1",
      "as_of": "YYYY-MM-DD",
      "days": {
        "YYYY-MM-DD": {
          "<book>": {
            "usd": float,
            "input_tokens": int,
            "output_tokens": int,
            "cache_read_tokens": int,
            "calls": int,
            "seats": { "<seat>": {"usd": float, "input_tokens": int, "output_tokens": int, "calls": int} }
          }
        }
      },
      "totals_30d": {
        "usd": float,
        "input_tokens": int,
        "output_tokens": int,
        "by_book": { "<book>": float },
        "by_seat": { "<seat>": float }
      }
    }

    Legacy float-only daily files appear as usd-only (all token fields = 0).
    Never raises — returns a minimal valid dict on any error.
    """
    a = _asof(asof)
    out: dict = {
        "schema": "mastermind.cost_summary.v1",
        "as_of": a,
        "days": {},
        "totals_30d": {
            "usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "by_book": {},
            "by_seat": {},
        },
    }
    try:
        from datetime import date as _date, timedelta as _td
        try:
            today = _date.fromisoformat(a)
        except Exception:  # noqa: BLE001
            today = _date.today()
        window = {(today - _td(days=i)).isoformat() for i in range(30)}

        cost_dir = _DIR
        if not cost_dir.exists():
            return out

        days_data: dict[str, dict] = {}
        for p in cost_dir.glob("*.json"):
            day = p.stem  # filename without .json = date
            if day not in window:
                continue
            try:
                raw = json.loads(p.read_text())
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(raw, dict):
                continue
            day_books: dict[str, dict] = {}
            for bk_name, bk_val in raw.items():
                if isinstance(bk_val, (int, float)):
                    # legacy flat float
                    try:
                        usd = float(bk_val)
                    except Exception:  # noqa: BLE001
                        usd = 0.0
                    day_books[bk_name] = {
                        "usd": usd,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_tokens": 0,
                        "calls": 0,
                        "seats": {},
                    }
                elif isinstance(bk_val, dict):
                    seats_raw = bk_val.get("seats") or {}
                    seats: dict[str, dict] = {}
                    if isinstance(seats_raw, dict):
                        for sn, sv in seats_raw.items():
                            if isinstance(sv, dict):
                                seats[sn] = {
                                    "usd": float(sv.get("usd") or 0.0),
                                    "input_tokens": int(sv.get("input_tokens") or 0),
                                    "output_tokens": int(sv.get("output_tokens") or 0),
                                    "calls": int(sv.get("calls") or 0),
                                }
                    day_books[bk_name] = {
                        "usd": float(bk_val.get("usd") or 0.0),
                        "input_tokens": int(bk_val.get("input_tokens") or 0),
                        "output_tokens": int(bk_val.get("output_tokens") or 0),
                        "cache_read_tokens": int(bk_val.get("cache_read_tokens") or 0),
                        "calls": int(bk_val.get("calls") or 0),
                        "seats": seats,
                    }
                else:
                    continue
            if day_books:
                days_data[day] = day_books

        out["days"] = days_data

        # totals
        tot_usd = 0.0
        tot_in = 0
        tot_out = 0
        by_book: dict[str, float] = {}
        by_seat: dict[str, float] = {}
        for day_books in days_data.values():
            for bk_name, bk in day_books.items():
                u = float(bk.get("usd") or 0.0)
                tot_usd += u
                tot_in += int(bk.get("input_tokens") or 0)
                tot_out += int(bk.get("output_tokens") or 0)
                by_book[bk_name] = round(by_book.get(bk_name, 0.0) + u, 6)
                for sn, sv in (bk.get("seats") or {}).items():
                    su = float(sv.get("usd") or 0.0) if isinstance(sv, dict) else 0.0
                    by_seat[sn] = round(by_seat.get(sn, 0.0) + su, 6)

        out["totals_30d"] = {
            "usd": round(tot_usd, 4),
            "input_tokens": tot_in,
            "output_tokens": tot_out,
            "by_book": by_book,
            "by_seat": by_seat,
        }
    except Exception:  # noqa: BLE001 — never raise; return safe partial
        pass
    return out


def write_cost_summary(dest: "Path | str | None" = None,
                       asof: str | None = None) -> "Path | None":
    """Write the 30-day cost summary to ``dest`` (default data/brain/cost_summary.json).

    Returns the written path, or None on failure. Never raises.
    """
    try:
        from pathlib import Path as _Path
        p = _Path(dest) if dest else _ROOT / "data" / "brain" / "cost_summary.json"
        payload = build_cost_summary(asof)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, sort_keys=True, indent=2))
        return p
    except Exception:  # noqa: BLE001
        return None
