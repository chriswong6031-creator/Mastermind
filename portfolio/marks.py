"""THE marking layer (charter P7 — one marking layer) — W-L / L1.

Before this module there were three ways a Mastermind book got a price:
  * ``paper_account._current_price`` (live Yahoo → vendored snapshot → parquet, USD),
  * ``self_directed._current_price`` (a SEPARATE Polygon/stockdata path that never reads the
    parquet store — so the Self-Directed book showed a *zero* return whenever its held names had
    no live quote and fell back to avg_cost), and
  * the shadow books' own ``_gather_prices`` (yet another accessor).
Three accessors → three answers for the same symbol on the same day = drift, and drift is death
(P7). This module is the SINGLE source of a mark. Every NAV writer routes through it.

Contract — one *logged* source precedence, evaluated per symbol, most-authoritative first:
  1. ``polygon-eod``    — the Polygon EOD/snapshot close (regular-session, EOD-grade).
  2. ``yahoo-parquet``  — the vendored dividend-adjusted daily-close parquet (``lib.store``),
                          last row ≤ asof (deterministic, offline, survivorship-clean).
  3. ``last-good-carry`` — the last mark we successfully logged for this symbol, carried forward
                          with a ``stale_days`` count so a book can never silently freeze on a
                          missing feed (P2: missing data degrades to a *carried* mark, it never
                          fabricates a move and it never touches avg_cost).

**NEVER avg_cost.** The old convention marked an unpriceable name at its cost basis, which quietly
zeroes that name's return — the exact bug that made Self-Directed read flat. A name with no source
and no carry is returned *unpriced* (absent from the dict); the NAV writer then holds it at its
prior mark rather than snapping it to cost.

Currency: marks are produced in USD. A non-USD book converts at its own NAV writer (the daily-mark
job already does ``fx.usd_to``), exactly as before — this layer does not change that seam.

Persistence: ``data/marks/last_good.json`` is the carry store ({SYMBOL: {price, asof, source}}),
and each run logs a per-symbol source attribution to ``data/marks/<asof>.json`` (audit: which feed
priced what, and how many symbols fell to carry). Best-effort throughout; never raises into a build.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

_ROOT = Path(__file__).resolve().parent.parent
_MARKS_DIR = _ROOT / "data" / "marks"
_CARRY_PATH = _MARKS_DIR / "last_good.json"

# source labels — the precedence order is the list order (most authoritative first).
SOURCE_POLYGON = "polygon-eod"
SOURCE_YAHOO = "yahoo-parquet"
SOURCE_CARRY = "last-good-carry"

_STALE_MAX_DAYS_DEFAULT = 30      # a carry older than this is dropped (a 30d-stale mark is not a mark)


def _stale_max_days() -> int:
    """doctrine.yml marks.carry_stale_max_days (unverified-prior), degrade-safe to the default."""
    try:
        from bot.doctrine_config import load_doctrine
        v = (load_doctrine().get("marks") or {}).get("carry_stale_max_days")
        return int(v) if v is not None else _STALE_MAX_DAYS_DEFAULT
    except Exception:  # noqa: BLE001
        return _STALE_MAX_DAYS_DEFAULT


# ─────────────────────────────────────────────────────────────────────────────
# carry store (the last-good-price memory)
# ─────────────────────────────────────────────────────────────────────────────
def _emit_marks_hard_stop(guard: str, detail: str) -> None:
    """Emit a HARD_STOP GuardrailResult to run_events for marks layer failures. Never raises."""
    try:
        from control_plane.guardrail import GuardrailResult, Severity
        GuardrailResult.failed(
            guard,
            Severity.HARD_STOP,
            detail=detail,
            action_taken="marks layer degraded; fabrication prevented",
        ).log(job="marks_layer", book="")
    except Exception:  # noqa: BLE001
        pass


def _load_carry() -> dict:
    try:
        raw = json.loads(_CARRY_PATH.read_text())
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}  # normal on first run — no HARD_STOP, just start fresh
    except Exception as _exc:  # noqa: BLE001
        _emit_marks_hard_stop("marks_carry_read", f"carry read failed: {_exc!r}"[:200])
        return {}


def _save_carry(carry: dict) -> None:
    try:
        _MARKS_DIR.mkdir(parents=True, exist_ok=True)
        _CARRY_PATH.write_text(json.dumps(carry, indent=2, default=str, sort_keys=True))
    except Exception as _exc:  # noqa: BLE001
        _emit_marks_hard_stop("marks_carry_write", f"carry write failed: {_exc!r}"[:200])


def _days_between(a: str, b: str) -> Optional[int]:
    try:
        return abs((date.fromisoformat(str(a)[:10]) - date.fromisoformat(str(b)[:10])).days)
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────────────────────────────────────────────────────────
# the two live sources (both optional / offline-safe / injectable in tests)
# ─────────────────────────────────────────────────────────────────────────────
def _polygon_eod(symbol: str) -> Optional[float]:
    """Regular-session EOD close from Polygon (snapshot: lastTrade → min → day → prevDay). USD."""
    try:
        from data_layer import polygon
        px = polygon.snapshot_price(symbol)
        if px and float(px) > 0:
            return float(px)
    except Exception:  # noqa: BLE001
        pass
    return None


def _yahoo_parquet(symbol: str, asof: str) -> Optional[float]:
    """Last dividend-adjusted daily close ≤ asof from the vendored yahoo parquet store. USD."""
    try:
        import pandas as pd
        from lib import store
        df = store.read("yahoo", symbol)
        if df is None or "close" not in df.columns or len(df) == 0:
            return None
        s = df["close"].astype(float).dropna()
        s.index = pd.to_datetime(s.index)
        s = s[(s.index <= pd.Timestamp(str(asof)[:10])) & (s > 0)]
        if len(s) == 0:
            return None
        return float(s.iloc[-1])
    except Exception:  # noqa: BLE001
        return None


# ─────────────────────────────────────────────────────────────────────────────
# the public marking API
# ─────────────────────────────────────────────────────────────────────────────
def mark_symbols(symbols: Iterable[str], asof: str, *,
                 seed: dict | None = None,
                 polygon_fn=None, yahoo_fn=None,
                 carry: dict | None = None,
                 persist: bool = True) -> dict:
    """Mark every symbol through the ONE logged precedence chain, returning:

        {
          "as_of": <asof>,
          "prices": {SYMBOL: usd_price, ...},          # ONLY symbols that got a real mark
          "sources": {SYMBOL: {"source": <label>, "price": px, "stale_days": n|None}},
          "counts": {"polygon-eod": .., "yahoo-parquet": .., "last-good-carry": .., "unpriced": ..},
        }

    ``seed`` (SYMBOL→px) is treated as an already-trusted mark (used by tests / a shared fetch) and
    short-circuits the source chain for that symbol (logged source ``seed``). A symbol that resolves
    to no source AND has no fresh-enough carry is *unpriced* — absent from ``prices`` (never marked
    to avg_cost). Successful marks refresh the carry store so tomorrow's missing feed can carry.

    ``polygon_fn`` / ``yahoo_fn`` inject the two live sources for offline tests (default: the real
    Polygon snapshot + the yahoo parquet). Best-effort; never raises."""
    asof = str(asof)[:10]
    seed = {(k or "").upper(): v for k, v in (seed or {}).items() if v and v > 0}
    pfn = polygon_fn if polygon_fn is not None else _polygon_eod
    yfn = yahoo_fn if yahoo_fn is not None else _yahoo_parquet
    carry = dict(_load_carry() if carry is None else carry)

    prices: dict = {}
    sources: dict = {}
    counts = {SOURCE_POLYGON: 0, SOURCE_YAHOO: 0, SOURCE_CARRY: 0, "seed": 0, "unpriced": 0}

    for raw in symbols or []:
        sym = (raw or "").upper().strip()
        if not sym or sym in prices:
            continue

        px: Optional[float] = None
        src: Optional[str] = None
        stale: Optional[int] = None

        if sym in seed:
            px, src = float(seed[sym]), "seed"
        if px is None:
            try:
                v = pfn(sym)
            except Exception:  # noqa: BLE001 — a dead feed degrades to the next source (P2)
                v = None
            if v and v > 0:
                px, src = float(v), SOURCE_POLYGON
        if px is None:
            try:
                v = yfn(sym, asof)
            except Exception:  # noqa: BLE001
                v = None
            if v and v > 0:
                px, src = float(v), SOURCE_YAHOO
        if px is None:                                   # carry the last good mark forward (P2)
            c = carry.get(sym)
            if c and c.get("price") and c["price"] > 0:
                d = _days_between(asof, c.get("asof") or asof)
                if d is None or d <= _stale_max_days():
                    px, src, stale = float(c["price"]), SOURCE_CARRY, (d if d is not None else 0)

        if px is None or px <= 0:                        # genuinely unpriceable — leave UNPRICED
            counts["unpriced"] += 1
            sources[sym] = {"source": None, "price": None, "stale_days": None}
            continue

        prices[sym] = px
        sources[sym] = {"source": src, "price": round(px, 6), "stale_days": stale}
        counts[src] = counts.get(src, 0) + 1
        # refresh the carry ONLY from a live source (never re-stamp a carry as fresh — that would
        # let a stale mark live forever by resetting its own asof each run).
        if src in (SOURCE_POLYGON, SOURCE_YAHOO, "seed"):
            carry[sym] = {"price": round(px, 6), "asof": asof, "source": src}

    result = {"as_of": asof, "prices": prices, "sources": sources, "counts": counts}
    if persist:
        _save_carry(carry)
        try:
            _MARKS_DIR.mkdir(parents=True, exist_ok=True)
            (_MARKS_DIR / f"{asof}.json").write_text(
                json.dumps({"as_of": asof, "sources": sources, "counts": counts,
                            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")},
                           indent=2, default=str))
        except Exception:  # noqa: BLE001
            pass
    return result


def mark_one(symbol: str, asof: str, **kw) -> Optional[float]:
    """Single-symbol convenience — the ONE price for `symbol` (or None if unpriceable). Used by the
    injection seam in self_directed so its book finally reads the same mark every other book does."""
    kw.setdefault("persist", False)
    return mark_symbols([symbol], asof, **kw)["prices"].get((symbol or "").upper().strip())


def prices_for(symbols: Iterable[str], asof: str, **kw) -> dict:
    """Just the {SYMBOL: usd_price} union dict — the common shape the NAV writers consume."""
    return mark_symbols(symbols, asof, **kw)["prices"]
