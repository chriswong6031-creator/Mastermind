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
# Canonical labels for every allowlisted ETF. Macro's per-ticker snapshots cover
# most of these names, but cash-equivalent funds such as SGOV are deliberately
# absent from that stock-oriented surface. Keeping the fixed universe's names
# beside its membership metadata guarantees a local, non-blocking display
# fallback for positions, decisions, orders, and historical trades.
_NAMES: dict[str, str] = {
    "SPY": "SPDR S&P 500 ETF Trust",
    "QQQ": "Invesco QQQ Trust",
    "IWM": "iShares Russell 2000 ETF",
    "DIA": "SPDR Dow Jones Industrial Average ETF Trust",
    "VTI": "Vanguard Total Stock Market ETF",
    "RSP": "Invesco S&P 500 Equal Weight ETF",
    "XLK": "Technology Select Sector SPDR Fund",
    "XLF": "Financial Select Sector SPDR Fund",
    "XLE": "Energy Select Sector SPDR Fund",
    "XLV": "Health Care Select Sector SPDR Fund",
    "XLU": "Utilities Select Sector SPDR Fund",
    "XLI": "Industrial Select Sector SPDR Fund",
    "XLP": "Consumer Staples Select Sector SPDR Fund",
    "XLY": "Consumer Discretionary Select Sector SPDR Fund",
    "XLB": "Materials Select Sector SPDR Fund",
    "XLC": "Communication Services Select Sector SPDR Fund",
    "XLRE": "Real Estate Select Sector SPDR Fund",
    "SMH": "VanEck Semiconductor ETF",
    "IGV": "iShares Expanded Tech-Software Sector ETF",
    "XBI": "SPDR S&P Biotech ETF",
    "XOP": "SPDR S&P Oil & Gas Exploration & Production ETF",
    "MTUM": "iShares MSCI USA Momentum Factor ETF",
    "QUAL": "iShares MSCI USA Quality Factor ETF",
    "USMV": "iShares MSCI USA Min Vol Factor ETF",
    "VLUE": "iShares MSCI USA Value Factor ETF",
    "SIZE": "iShares MSCI USA Size Factor ETF",
    "TLT": "iShares 20+ Year Treasury Bond ETF",
    "IEF": "iShares 7-10 Year Treasury Bond ETF",
    "SGOV": "iShares 0-3 Month Treasury Bond ETF",
    "BIL": "SPDR Bloomberg 1-3 Month T-Bill ETF",
    "SHV": "iShares Short Treasury Bond ETF",
    "SHY": "iShares 1-3 Year Treasury Bond ETF",
    "GLD": "SPDR Gold Shares",
}
_DEFAULT_DEFENSIVE_EXTRA = ["USMV", "XLP", "XLU"]
_DEFAULT_GUARDRAILS = {"max_single_weight": 0.35, "min_trade": 0.015,
                       "offensive_cap": {"stressed": 0.55, "elevated": 0.80}}
# Overextension: a name extended past `pct_vs_200d_cap`% above its 200d is parabolic — cap its weight
# to `max_weight` so the book can't ride a blow-off top at size. `pct_vs_200d_cap: null`/≤0 disables.
_DEFAULT_OVEREXTENSION = {"pct_vs_200d_cap": 40.0, "max_weight": 0.08}
# Factor clusters: cap the COMBINED gross of a correlated leadership group so the book can't be a
# single-factor bet wearing many tickers (SPY/QQQ/SMH/MTUM were all one growth/semis trade on 06-22).
_DEFAULT_FACTOR_CLUSTERS = [
    {"name": "megacap_growth_semis", "members": ["QQQ", "XLK", "SMH", "IGV", "MTUM", "SIZE"],
     # 0.35 mirrors the firm-decided cap in config/clusters.yml + config/etf_strategy.yml. This in-code
     # default is the fallback when BOTH configs are absent/unparseable, so it must NOT loosen below the
     # firm level (a stale 0.40 silently weakened the proven G5 semis/AI brake by 5pp during a config gap).
     "max_gross": 0.35},
]
_DEFAULT_HORIZON = 21


def _norm_overextension(ov) -> dict:
    ov = ov if isinstance(ov, dict) else {}
    d = _DEFAULT_OVEREXTENSION
    cap = ov.get("pct_vs_200d_cap", d["pct_vs_200d_cap"])
    try:
        cap = float(cap) if cap is not None else None      # None / null = disabled
    except (TypeError, ValueError):
        cap = d["pct_vs_200d_cap"]
    try:
        mw = float(ov.get("max_weight", d["max_weight"]))
    except (TypeError, ValueError):
        mw = d["max_weight"]
    return {"pct_vs_200d_cap": cap, "max_weight": mw}


def _norm_clusters(clusters) -> list[dict]:
    if not isinstance(clusters, list):
        clusters = _DEFAULT_FACTOR_CLUSTERS
    out: list[dict] = []
    for c in clusters:
        if not isinstance(c, dict):
            continue
        members = [str(m).upper().strip() for m in (c.get("members") or []) if m]
        mg = c.get("max_gross")
        if members and isinstance(mg, (int, float)):
            out.append({"name": str(c.get("name") or "cluster"), "members": members, "max_gross": float(mg)})
    return out


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


def _clusters_from_shared_config() -> list[dict]:
    """Load factor_clusters from the shared config/clusters.yml (A3 single source of truth).

    Returns the normalised list (same shape as _norm_clusters output) when clusters.yml is present
    and parseable; returns [] when absent/unparseable so the caller falls back to etf_strategy.yml
    values.  Lazy-import keeps the dependency optional — a missing portfolio.cluster_config never
    breaks the ETF book."""
    try:
        from portfolio import cluster_config  # noqa: PLC0415 — lazy to stay import-order safe
        etf_cls = cluster_config.etf_clusters()
        if etf_cls:
            return _norm_clusters(etf_cls)   # re-normalise for uniform type guarantees
    except Exception:  # noqa: BLE001 — any failure degrades to fallback; never weakens the brake
        pass
    return []


def guardrails() -> dict:
    """The hard risk limits from the spec (with in-code fallbacks). Read fresh so an edit retunes
    the next run. Shape: {max_single_weight, min_trade, offensive_cap: {stressed, elevated},
    overextension: {pct_vs_200d_cap, max_weight}, factor_clusters: [{name, members, max_gross}]}.

    ``factor_clusters`` source priority:
      1. ``config/clusters.yml`` — the shared firm-wide definition (A3: single source of truth).
      2. ``config/etf_strategy.yml`` guardrails.factor_clusters — DEPRECATED mirror; used as
         fallback when clusters.yml is absent/unparseable so the proven G5 brake never weakens
         during rollout.
      3. _DEFAULT_FACTOR_CLUSTERS — the in-code fallback when both files are missing/invalid.
    """
    g = load_spec().get("guardrails") or {}
    oc = g.get("offensive_cap") or {}
    d = _DEFAULT_GUARDRAILS
    overext = _norm_overextension(g.get("overextension"))      # tolerant of its own bad fields

    # Prefer clusters.yml (shared firm-wide config); fall back to etf_strategy.yml then in-code.
    clusters = _clusters_from_shared_config()
    if not clusters:
        clusters = _norm_clusters(g.get("factor_clusters"))

    try:
        return {
            "max_single_weight": float(g.get("max_single_weight", d["max_single_weight"])),
            "min_trade": float(g.get("min_trade", d["min_trade"])),
            "offensive_cap": {
                "stressed": float(oc.get("stressed", d["offensive_cap"]["stressed"])),
                "elevated": float(oc.get("elevated", d["offensive_cap"]["elevated"])),
            },
            "overextension": overext,
            "factor_clusters": clusters,
        }
    except (TypeError, ValueError):
        return {**{k: (dict(v) if isinstance(v, dict) else v) for k, v in d.items()},
                "overextension": dict(_DEFAULT_OVEREXTENSION),
                "factor_clusters": [dict(c) for c in _DEFAULT_FACTOR_CLUSTERS]}


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


def name_of(ticker) -> str | None:
    """Canonical human label for an allowlisted ETF, or ``None`` off-list."""
    if not isinstance(ticker, str):
        return None
    return _NAMES.get(ticker.upper().strip())


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
