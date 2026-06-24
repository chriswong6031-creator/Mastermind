"""Fragility-CHAIN map + correlation gate — is the book a leveraged bet on what's cracking?

`portfolio/firm_exposure.py` flags when many books pile into the same TICKER or SECTOR. This module
goes one step deeper, to the question nobody answered on 2026-06-23 when the desk sat long the
crowded AI-buildout names into a memory/semis blow-off: not "do we hold the same names" but "is the
WHOLE book a concentrated, correlated bet on ONE crowded+extended THEME-CHAIN whose leading edge is
already cracking?".

A *fragility chain* is a causal supply/demand chain whose leading-edge node tends to crack first when
liquidity tightens or a theme blows off, propagating downstream — e.g. memory/DRAM → hyperscaler
capex → AI-compute buildout → industrial-AI power. The map is externalized to
``config/fragility_chains.yml`` (tunable, reviewable — like ``config/etf_strategy.yml``); this module
degrades to an in-code default if that file is missing/malformed, so a bad edit never breaks a book.

Public surface (all PURE / deterministic / NEVER raises — every read degrades to an honest stub):
  * ``classify(ticker)``            — which chain(s) a name sits in, and whether at the leading edge.
  * ``chains_of(ticker)``           — the set of chain ids a name belongs to (membership test).
  * ``book_chain_exposure(book)``   — gross weight per chain across a proposed/held book.
  * ``fragile_chains(hot_tickers)`` — which chains the macro read says are leading-edge fragile NOW.
  * ``assess_book(book, risk_state)`` — the SUBTRACT-ONLY gate: when risk-off/caution AND a cracking
    chain is over-concentrated, emit trims to bring it under cap + mark it add-blocked. Never injects.

The TEETH (applying the trims, capping gross, blocking adds) live in ``brain/macro_risk.py``
(``apply_risk_state``) so the seat owns the de-risking action; this module only *measures and flags*.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_SPEC_PATH = _ROOT / "config" / "fragility_chains.yml"


# ── in-code DEFAULT — the fallback when the YAML spec is missing/malformed ──────────────────────
# Kept in sync with config/fragility_chains.yml; a lean checkout (no PyYAML / no file) still gets the
# critical ai_buildout chain so the 06-23 failure mode is covered out of the box.
_DEFAULT_CHAINS: dict[str, dict] = {
    "ai_buildout": {
        "name": "AI build-out (memory → hyperscaler capex → buildout → industrial-AI power)",
        "driver": "memory / DRAM pricing + hyperscaler-capex guidance",
        "nodes": [
            "memory/DRAM (the first cyclical tell)",
            "hyperscaler capex (funds the chain)",
            "AI compute buildout (the crowded momentum core)",
            "industrial-AI capex (power/grid/cooling — most extended late)",
        ],
        "leading": ["MU", "STX", "WDC", "SMCI"],
        "tickers": ["MU", "STX", "WDC", "SMCI", "MSFT", "GOOGL", "GOOG", "META", "AMZN", "NVDA",
                    "AVGO", "AMD", "TSM", "ANET", "APH", "VRT", "ETN", "GEV", "PWR", "CEG", "VST",
                    "NBIS", "ORCL", "DELL"],
        "proxies": ["SMH", "SOXX", "XLK", "IGV"],
    },
    "long_duration_tech": {
        "name": "Long-duration / high-multiple tech",
        "driver": "real yields + risk appetite for long-duration cash flows",
        "nodes": ["real yields up compresses the multiple", "high-beta software de-rates",
                  "momentum unwinds"],
        "leading": ["ARKK"],
        "tickers": ["CRM", "NOW", "SNOW", "CRWD", "PLTR", "NET", "DDOG", "SHOP", "PANW"],
        "proxies": ["IGV", "ARKK", "QQQ"],
    },
    "rate_sensitive": {
        "name": "Rate-sensitive financials & real assets (banks / REITs / utilities)",
        "driver": "the long end + the credit cycle",
        "nodes": ["rates-up hurts REITs/utilities", "a credit event hurts banks but helps duration",
                  "the sign flips with the driver"],
        "leading": [],
        "tickers": ["JPM", "BAC", "WFC", "C", "GS", "MS", "PLD", "AMT", "O", "SPG"],
        "proxies": ["XLF", "XLRE", "KRE"],
    },
    "commodity_inflation": {
        "name": "Commodity / inflation cyclicals (energy + materials)",
        "driver": "the inflation impulse + the global growth cycle",
        "nodes": ["an inflation shock lifts the chain", "a growth-scare cracks it"],
        "leading": [],
        "tickers": ["XOM", "CVX", "COP", "SLB", "FCX", "NUE", "CAT", "DE"],
        "proxies": ["XLE", "XLB", "XOP"],
    },
}


def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, default))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def load_spec() -> dict:
    """The externalized fragility-chain spec (``config/fragility_chains.yml``). {} on a missing/corrupt
    file or absent PyYAML — the caller then falls back to the in-code default. Read fresh each call so
    edits are picked up next run; never raises."""
    try:
        import yaml
        if _SPEC_PATH.exists():
            d = yaml.safe_load(_SPEC_PATH.read_text())
            return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def all_chains() -> dict[str, dict]:
    """The chain map: the YAML ``chains`` block when present + valid, else the in-code default. Each
    value is normalised so every chain has tickers/proxies/leading as upper-cased lists. Never raises."""
    raw = (load_spec().get("chains") if isinstance(load_spec().get("chains"), dict) else None) \
        or _DEFAULT_CHAINS
    out: dict[str, dict] = {}
    for cid, c in raw.items():
        if not isinstance(c, dict):
            continue
        def _up(key):
            return [str(t).upper().strip() for t in (c.get(key) or []) if str(t).strip()]
        out[str(cid)] = {
            "name": str(c.get("name") or cid),
            "driver": str(c.get("driver") or ""),
            "nodes": [str(n) for n in (c.get("nodes") or [])],
            "leading": _up("leading"),
            "tickers": _up("tickers"),
            "proxies": _up("proxies"),
        }
    return out


def classify(ticker: str) -> list[dict]:
    """Which chain(s) ``ticker`` belongs to (single name OR ETF proxy), and whether it sits at the
    fragile leading edge. Returns ``[{chain, name, driver, position}]`` (``position`` ∈
    {leading_edge, downstream}); ``[]`` for an unmapped name. Pure; never raises."""
    t = (ticker or "").upper().strip()
    if not t:
        return []
    out: list[dict] = []
    for cid, c in all_chains().items():
        members = set(c["tickers"]) | set(c["proxies"])
        if t in members:
            out.append({
                "chain": cid, "name": c["name"], "driver": c["driver"],
                "position": "leading_edge" if t in set(c["leading"]) else "downstream",
            })
    return out


def chains_of(ticker: str) -> set[str]:
    """The set of chain ids ``ticker`` belongs to (membership test). Pure; never raises."""
    return {c["chain"] for c in classify(ticker)}


def _book_weights(book: Any) -> dict[str, float]:
    """Coerce a book (list of {ticker, weight} dicts OR a {ticker: weight} mapping) into
    {TICKER: weight}. Defensive — drops malformed rows, sums duplicates. Never raises."""
    out: dict[str, float] = {}
    if isinstance(book, dict):
        items = book.items()
    else:
        items = []
        for r in (book or []):
            if isinstance(r, dict):
                items.append((r.get("ticker"), r.get("weight")))
    for tk, w in items:
        t = str(tk or "").upper().strip()
        if not t:
            continue
        try:
            wv = float(w)
        except (TypeError, ValueError):
            continue
        if wv <= 0:
            continue
        out[t] = out.get(t, 0.0) + wv
    return out


def book_chain_exposure(book: Any) -> dict[str, dict]:
    """Gross book weight attributed to each fragility chain. A name in two chains counts in BOTH —
    that double-count is honest: the book genuinely carries both exposures. Returns
    ``{chain_id: {weight, weight_pct, tickers, leading_tickers, has_leading}}`` sorted by weight.
    Pure; never raises (empty dict on an empty book)."""
    weights = _book_weights(book)
    chains = all_chains()
    agg: dict[str, dict] = {}
    for cid, c in chains.items():
        members = set(c["tickers"]) | set(c["proxies"])
        leading = set(c["leading"])
        held = [(t, w) for t, w in weights.items() if t in members]
        if not held:
            continue
        w_sum = round(sum(w for _t, w in held), 6)
        agg[cid] = {
            "name": c["name"],
            "driver": c["driver"],
            "weight": w_sum,
            "weight_pct": round(w_sum * 100, 2),
            "tickers": sorted(t for t, _w in held),
            "leading_tickers": sorted(t for t, _w in held if t in leading),
            "has_leading": any(t in leading for t, _w in held),
        }
    return dict(sorted(agg.items(), key=lambda kv: kv[1]["weight"], reverse=True))


def fragile_chains(hot_tickers: Any, regime: dict | None = None) -> list[dict]:
    """Which chains the macro read says are LEADING-EDGE fragile right now. ``hot_tickers`` is the set
    of crowded/extended/distributing names the Macro Risk Officer's crowding axis surfaced (sector
    leaders at ~100th pctile, blow-off baskets, names with a hard extension grade). A chain is
    'fragile' when those hot names overlap it — STRONGER when the overlap hits the leading-edge node.

    Returns ``[{id, name, driver, hot_overlap, leading_hit, score}]`` sorted by score (most-fragile
    first). Pure; never raises."""
    hot = {str(t).upper().strip() for t in (hot_tickers or []) if str(t).strip()}
    out: list[dict] = []
    if not hot:
        return out
    for cid, c in all_chains().items():
        members = set(c["tickers"]) | set(c["proxies"])
        overlap = sorted(hot & members)
        if not overlap:
            continue
        leading_hit = sorted(hot & set(c["leading"]))
        # score: each hot member counts; a hot LEADING-edge name counts double (it cracks first).
        score = round(len(overlap) + len(leading_hit), 3)
        out.append({
            "id": cid, "name": c["name"], "driver": c["driver"],
            "hot_overlap": overlap, "leading_hit": leading_hit, "score": score,
        })
    return sorted(out, key=lambda d: d["score"], reverse=True)


def assess_book(book: Any, risk_state: dict | None, *, chain_cap: float | None = None) -> dict:
    """The SUBTRACT-ONLY fragility gate. When the macro state is caution/risk-off AND a chain the
    Macro Risk Officer named as a leading-edge fragile DRIVER carries more than ``chain_cap`` of the
    book, the book is a concentrated bet on the cracking theme: emit proportional trims to bring that
    chain back to ``chain_cap`` and mark it add-blocked. Pure; NEVER injects a name; never raises.

    Returns::

        {chain_flags:   [{chain, name, weight, cap, reason}],
         blocked_chains:[chain_id],          # adds into these are hard-stopped
         trims:         [{ticker, action:'trim', scale, chain, reason}]}

    Returns the empty stub when risk-on, no driver chain is over-cap, or the book is empty — so the
    caller can apply the result unconditionally. Trims are in the same shape the Gate/Risk officers
    emit, so ``brain/macro_risk.apply_risk_state`` consumes them with the gross-cap pass."""
    empty = {"chain_flags": [], "blocked_chains": [], "trims": []}
    rs = risk_state or {}
    state = str(rs.get("state") or "risk_on")
    if state not in ("caution", "risk_off"):
        return empty
    cap = chain_cap if chain_cap is not None else _env_float("MASTERMIND_CHAIN_CAP", 0.35)

    # the leading-edge fragile DRIVER chains the macro read named (ids only; tolerate dict or str)
    drivers: set[str] = set()
    for d in (rs.get("drivers") or []):
        if isinstance(d, dict) and d.get("id"):
            drivers.add(str(d["id"]))
        elif isinstance(d, str):
            drivers.add(d)
    if not drivers:
        return empty

    exposure = book_chain_exposure(book)
    chains = all_chains()
    flags: list[dict] = []
    blocked: list[str] = []
    trims: list[dict] = []
    for cid in drivers:
        info = exposure.get(cid)
        if not info or info["weight"] <= cap + 1e-9:
            continue
        # over-concentrated on a cracking chain → trim it back to the cap, leading-edge names first.
        scale = round(cap / info["weight"], 4) if info["weight"] > 0 else 0.0
        blocked.append(cid)
        flags.append({"chain": cid, "name": info["name"], "weight": info["weight"],
                      "weight_pct": info["weight_pct"], "cap": cap,
                      "reason": (f"book carries {info['weight_pct']:.1f}% in the {cid} chain "
                                 f"(> {cap * 100:.0f}% cap) while it is the leading-edge fragile driver")})
        leading = set(chains.get(cid, {}).get("leading") or [])
        for t in info["tickers"]:
            # risk-off cuts the leading edge harder (it cracks first); caution trims proportionally.
            sc = scale
            if state == "risk_off" and t in leading:
                sc = round(min(scale, scale * 0.5), 4)
            trims.append({"ticker": t, "action": "trim", "scale": sc, "chain": cid,
                          "reason": f"{cid} chain over-cap into a {state} tape"})
    return {"chain_flags": flags, "blocked_chains": sorted(set(blocked)), "trims": trims}
