"""Vol-managed / risk-parity position sizing for the conviction book — the one
PORTFOLIO upgrade that survived an honest net-of-cost backtest (Moreira-Muir:
+0.10-0.15 Sharpe with ZERO directional alpha; measured SPY 0.64->0.76, equal-weight
1.07->1.22 on the macro side, research/MAGICAL_SIGNALS_ROADMAP.md in the macro repo).

It decides HOW MUCH to own (risk), orthogonal to confluence (WHAT) and the entry gauge
(WHEN): bet LESS on high-volatility names, MORE on calm ones, and take more total gross
when the cross-sectional DISPERSION regime says stock-selection pays.

Reuses the macro engine's already-validated, point-in-time sizing rather than recomputing:
each name on the macro standout board carries `risk_sizing.size_mult` (inverse forecasted
vol x regime), and the board carries `dispersion_regime` (the selection-gross dial), read
from vendor/macro. GRACEFUL: until the macro side ships those fields (a vendor/macro
rebuild), `size_mult` defaults to 1.0 and the book sizes exactly as before — additive,
never breaks. Risk lever ONLY: it re-weights WITHIN the selected book, never changes which
names are in it.
"""
from __future__ import annotations

import json
from pathlib import Path

_V = Path(__file__).resolve().parent.parent / "vendor" / "macro"
_MULT_CAP, _MULT_FLOOR = 1.6, 0.4      # per-name vol-multiplier clamp


def _load(rel: str):
    p = _V / rel
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:
        return None


def _index() -> tuple[dict, dict]:
    """{ticker: size_mult} from the macro standout board + the dispersion regime dict."""
    w = _load("site/factordata/us_standouts.json") or {}
    mult: dict[str, float] = {}
    for sec in ("buy", "watch", "laggards"):
        for r in (w.get(sec) or []):
            rs = r.get("risk_sizing") or {}
            sm = rs.get("size_mult")
            if isinstance(sm, (int, float)) and r.get("ticker"):
                mult[r["ticker"]] = float(sm)
    return mult, (w.get("dispersion_regime") or {})


_CACHE: dict = {}


def _ensure():
    if "mult" not in _CACHE:
        _CACHE["mult"], _CACHE["regime"] = _index()


def vol_mult(ticker: str) -> float:
    """Per-name vol-managed size multiplier (clamped); 1.0 when the macro field is absent."""
    _ensure()
    return float(min(_MULT_CAP, max(_MULT_FLOOR, _CACHE["mult"].get(ticker, 1.0))))


def selection_gross() -> float:
    """The dispersion-regime gross dial for the whole book; 1.0 when absent. Only ever
    DE-grosses the long-only book (lean_out < 1); a favourable regime keeps full budget,
    it does not lever above it here."""
    _ensure()
    g = (_CACHE.get("regime") or {}).get("gross_mult")
    return min(float(g), 1.0) if isinstance(g, (int, float)) else 1.0


def regime_state() -> str | None:
    _ensure()
    return (_CACHE.get("regime") or {}).get("state")


def reset_cache() -> None:
    _CACHE.clear()


def apply(positions: list[dict], budget: float, name_cap: float = 0.08) -> list[dict]:
    """Re-size positions by risk: weight_i *= vol_mult_i, scale total invested by the
    selection-gross dial (de-gross in a poor selection regime, holding more cash),
    renormalize to that target, cap per name. Mutates + returns the list. Never changes
    membership — only how much of the budget each name takes."""
    if not positions or budget <= 0:
        return positions
    raw = {}
    for p in positions:
        raw[id(p)] = max(0.0, float(p.get("weight", 0.0))) * vol_mult(p.get("ticker", ""))
    tot = sum(raw.values())
    if tot <= 0:
        return positions
    target = budget * selection_gross()                # full budget in lean_in; less in lean_out
    for p in positions:
        w = min(raw[id(p)] / tot * target, name_cap)
        p["weight"] = round(w, 4)
        p["vol_mult"] = round(vol_mult(p.get("ticker", "")), 2)
    return positions
