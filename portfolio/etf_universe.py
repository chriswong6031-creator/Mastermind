"""The ETF book's tradeable universe + an ETF-aware USD mark — loaded from the strategy spec.

The universe, the crisis-floor buckets, the risk guardrails, and the grading horizon are all read
from the externalized spec ``config/etf_strategy.yml`` (so the strategy is a tunable, reviewable
artifact, not a code string). A missing or malformed spec degrades to the in-code DEFAULTS below,
so a bad edit never breaks the book.

This module is the single source of truth for:
  * ``is_etf()`` — the trusted-layer membership gate. Anything the Brain submits that is NOT in the
    allowlist is dropped (the ETF analogue of the China book's venue filter): no single names, no
    leveraged/inverse products, no off-list ETFs.
  * ``DEFENSIVE`` — the crisis-floor bucket (long duration / T-bill cash / min-vol / staples /
    utilities / gold). ``bot/etf.py``'s risk guardrail caps OFFENSIVE gross (everything else) when
    the dashboard reads stressed, freeing the rest into this bucket + cash.
  * ``price()`` / ``warm()`` — an ETF-aware mark. The shared ``paper_account`` path only live-fetches
    ``*.HK`` names via Yahoo; bare US tickers fall back to a STATIC vendored snapshot, so any ETF the
    macro dashboard hasn't cached (DIA, VTI, VLUE, SGOV, GLD, …) reads as unpriceable. We add a live
    Yahoo leg here (US ETFs quote USD, so the local close == the book mark, no FX) so the WHOLE
    universe is priceable, degrading to ``paper_account._current_price`` on any miss. Contained to
    this book — no behaviour change for the other US books.

The universe section is read at import (a change needs a process restart); ``guardrails()`` /
``horizon_days()`` are read fresh per call (hot-tunable).
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SPEC_PATH = _ROOT / "config" / "etf_strategy.yml"

# ── in-code DEFAULTS — the fallback when the spec is missing/malformed ───────
_DEFAULT_GROUPS: dict[str, list[str]] = {
    "core_index":  ["SPY", "QQQ", "IWM", "DIA", "VTI", "RSP"],
    "sectors":     ["XLK", "XLF", "XLE", "XLV", "XLU", "XLI", "XLP", "XLY", "XLB", "XLC", "XLRE",
                    "SMH", "IGV", "XBI", "XOP"],
    "factors":     ["MTUM", "QUAL", "USMV", "VLUE", "SIZE"],
    "duration":    ["TLT", "IEF"],
    "cash":        ["SGOV", "BIL", "SHV", "SHY"],
    "diversifier": ["GLD"],
}
_DEFAULT_DEFENSIVE_EXTRA = ["USMV", "XLP", "XLU"]
_DEFAULT_GUARDRAILS = {"max_single_weight": 0.35, "min_trade": 0.015,
                       "offensive_cap": {"stressed": 0.55, "elevated": 0.80}}
_DEFAULT_HORIZON = 21


def load_spec() -> dict:
    """The externalized ETF strategy spec (``config/etf_strategy.yml``). {} on a missing/corrupt
    file or absent PyYAML — every consumer falls back to its in-code default, so a bad edit never
    breaks the book. Read fresh each call so guardrail/doctrine edits are picked up next run."""
    try:
        import yaml
        if _SPEC_PATH.exists():
            d = yaml.safe_load(_SPEC_PATH.read_text())
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _build_groups() -> dict[str, list[str]]:
    u = load_spec().get("universe")
    if isinstance(u, dict):
        g = {k: [str(t).upper().strip() for t in v]
             for k, v in u.items() if isinstance(v, list) and v}
        if g:
            return g
    return {k: list(v) for k, v in _DEFAULT_GROUPS.items()}


def _build_defensive(groups: dict[str, list[str]]) -> frozenset[str]:
    extra = load_spec().get("defensive_extra")
    extra = [str(t).upper().strip() for t in extra] if isinstance(extra, list) else list(_DEFAULT_DEFENSIVE_EXTRA)
    always = groups.get("duration", []) + groups.get("cash", []) + groups.get("diversifier", [])
    return frozenset(always + extra)


# ── the universe, resolved from the spec at import ──────────────────────────
GROUPS: dict[str, list[str]] = _build_groups()
ALL: frozenset[str] = frozenset(t for g in GROUPS.values() for t in g)
DEFENSIVE: frozenset[str] = _build_defensive(GROUPS)
OFFENSIVE: frozenset[str] = frozenset(t for t in ALL if t not in DEFENSIVE)


def guardrails() -> dict:
    """The hard risk limits from the spec (with in-code fallbacks). Read fresh so an edit retunes
    the next run. Shape: {max_single_weight, min_trade, offensive_cap: {stressed, elevated}}."""
    g = load_spec().get("guardrails") or {}
    oc = g.get("offensive_cap") or {}
    d = _DEFAULT_GUARDRAILS
    try:
        return {
            "max_single_weight": float(g.get("max_single_weight", d["max_single_weight"])),
            "min_trade": float(g.get("min_trade", d["min_trade"])),
            "offensive_cap": {
                "stressed": float(oc.get("stressed", d["offensive_cap"]["stressed"])),
                "elevated": float(oc.get("elevated", d["offensive_cap"]["elevated"])),
            },
        }
    except (TypeError, ValueError):
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in d.items()}


def horizon_days() -> int:
    """The forward window (business days) each daily pick is graded over, vs SPY."""
    try:
        return int((load_spec().get("accountability") or {}).get("horizon_days", _DEFAULT_HORIZON))
    except (TypeError, ValueError):
        return _DEFAULT_HORIZON


# ── membership + buckets ────────────────────────────────────────────────────

def is_etf(ticker) -> bool:
    """True iff ``ticker`` is in the book's allowlist (case-insensitive). A non-string (a malformed
    submission) is simply not an ETF — never raises."""
    return isinstance(ticker, str) and ticker.upper().strip() in ALL


def is_defensive(ticker) -> bool:
    """True iff ``ticker`` is in the crisis-floor (defensive) bucket."""
    return isinstance(ticker, str) and ticker.upper().strip() in DEFENSIVE


def group_of(ticker) -> str | None:
    """The universe group a ticker belongs to ('sectors', 'duration', …), or None if off-list."""
    if not isinstance(ticker, str):
        return None
    t = ticker.upper().strip()
    for g, names in GROUPS.items():
        if t in names:
            return g
    return None


# ── ETF-aware pricing ───────────────────────────────────────────────────────

def warm(tickers=None) -> None:
    """Batch-prefetch live USD marks for the universe in ONE Yahoo request (Yahoo throttles rapid
    single-symbol calls). Defaults to the whole universe. Best-effort; never raises."""
    try:
        from data_layer import yahoo_feed
        yahoo_feed.warm(sorted(tickers if tickers is not None else ALL))
    except Exception:
        pass


def price(ticker: str | None) -> float | None:
    """Best USD mark for an ETF: the live Yahoo close (warmed via ``warm()``), else the shared
    ``paper_account`` path (vendored snapshot / engine parquet). US ETFs quote USD, so the Yahoo
    local close needs no FX conversion. Returns None when neither has a mark (→ the name is skipped
    and surfaced honestly, never silently dropped)."""
    t = (ticker or "").upper().strip()
    if not t:
        return None
    try:
        from data_layer import yahoo_feed
        px = yahoo_feed.price_local(t)        # USD for a US-listed ETF (local quote == book ccy)
        if px and px > 0:
            return float(px)
    except Exception:
        pass
    try:
        from portfolio import paper_account
        return paper_account._current_price(t)
    except Exception:
        return None
