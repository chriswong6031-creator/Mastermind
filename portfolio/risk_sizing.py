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
from vendor/macro. The board is the PRIMARY (exact-key) size lens. When a name is OFF the
board (the selection/board universe mismatch — the book is AI/semis while the board's BUY
list is defensives/value), we do NOT silently degrade to a neutral 1.0 (that laundered an
inert lever as risk-managed sizing); instead we estimate an inverse-vol multiplier from the
name's own trailing realized-vol price series, clamped to the SAME band. Only a name with NO
price series at all degrades to exactly 1.0 (true fail-closed neutral). Risk lever ONLY: it
re-weights WITHIN the selected book and can never RAISE gross (see NEW-SIZE-1: the renorm
target is capped at the incoming invested total), never changes which names are in it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

_V = Path(__file__).resolve().parent.parent / "vendor" / "macro"
_MULT_CAP, _MULT_FLOOR = 1.6, 0.4      # per-name vol-multiplier clamp
_COVERAGE_FLOOR = 0.5                  # < this fraction of the book on-board => inert-lever diagnostic
_EQW_SHARE = 0.80                      # > this fraction sharing one weight => degenerate-equal-weight probe
_MIN_VOL_OBS = 40                      # a price series needs >= this many returns for a trustworthy vol


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


def _clamp_mult(x: float) -> float:
    return float(min(_MULT_CAP, max(_MULT_FLOOR, x)))


def vol_mult(ticker: str) -> float:
    """Per-name vol-managed size multiplier (clamped) from the EXACT-key board lookup; 1.0 when the
    board has no row for the name. This is the PRIMARY path. Off-board names are handled by the
    on-the-fly inverse-vol fallback inside apply() (NEW-SIZE-2) — vol_mult() itself stays a pure,
    side-effect-free board reader so its semantics (and the existing golden tests) are unchanged."""
    _ensure()
    return _clamp_mult(_CACHE["mult"].get(ticker, 1.0))


def on_board(ticker: str) -> bool:
    """True iff the macro standout board carries a size_mult for this exact ticker key."""
    _ensure()
    return ticker in _CACHE["mult"]


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


# ── NEW-SIZE-2: on-the-fly inverse-vol fallback for OFF-BOARD names ──────────────────────────────
def _default_price_series(ticker: str):
    """Default price-series loader = the same vendored store safety.py reads (yahoo ETFs/SPY +
    breadth single-name parquet). Imported lazily + defensively so a missing pandas/vendor lib (or a
    test with no price store) degrades to None -> the caller falls back to the neutral 1.0."""
    try:
        from portfolio import paper_account
        return paper_account._fetch_price_series(ticker)
    except Exception:  # noqa: BLE001 — no price store => no fallback, degrade to neutral
        return None


def _realized_vol(series, lookback: int = 60) -> float | None:
    """Trailing realized (std of daily log-ish pct-change) vol from a close series. Returns None when
    the series is absent or too short to trust (< _MIN_VOL_OBS returns) — the caller then degrades to
    the neutral 1.0, matching the invariant (no price -> no lever, never a fabricated one)."""
    if series is None:
        return None
    try:
        s = series.dropna().astype(float)
        if len(s) < 2:
            return None
        rets = s.pct_change().dropna()
        if len(rets) > lookback:
            rets = rets.iloc[-lookback:]
        if len(rets) < _MIN_VOL_OBS:
            return None
        v = float(rets.std())
        return v if v > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _fallback_size_mults(off_board: list[dict],
                         price_series_fn: Callable[[str], object]) -> dict[int, float]:
    """Inverse-vol size multipliers for the OFF-BOARD names, keyed by id(position).

    Inverse-vol vs the book MEDIAN realized vol (a higher-vol name gets a <1 multiplier, a calmer
    name >1), clamped to the SAME [_MULT_FLOOR, _MULT_CAP] band as the board lever. A name whose
    price series is missing/too-short degrades to exactly 1.0 (today's behaviour — true fail-closed
    neutral). This keeps the lever a pure WITHIN-book risk redistribution: NEW-SIZE-1 caps the renorm
    target at incoming gross, so estimating a >1 mult for a calm off-board name can never raise the
    book's gross — it only shifts relative weight away from the wilder names."""
    vols: dict[int, float] = {}
    for p in off_board:
        v = _realized_vol(price_series_fn(p.get("ticker", "")))
        if v is not None:
            vols[id(p)] = v
    if not vols:
        return {}
    # median of the names we COULD measure; the inverse-vol multiplier is normalised to it so the
    # book-median-vol name sits at ~1.0 and the lever is a pure dispersion around it.
    ordered = sorted(vols.values())
    n = len(ordered)
    med = ordered[n // 2] if n % 2 else 0.5 * (ordered[n // 2 - 1] + ordered[n // 2])
    if med <= 0:
        return {}
    out: dict[int, float] = {}
    for p in off_board:
        v = vols.get(id(p))
        out[id(p)] = _clamp_mult(med / v) if v else 1.0    # no series -> neutral 1.0
    return out


def _emit_diag(positions: list, record: dict) -> None:
    """Attach a sizing diagnostic to the book's _SizedBook.data_health (the existing runlog seam)
    and mirror it to the module logger. Best-effort + additive: it never mutates weights and never
    raises into the sizing path. `positions` may be a plain list (no data_health attribute) — then
    the logger mirror is the only surface, which is fine (degrade-to-today)."""
    try:
        log.warning("risk_sizing diagnostic: %s", record)
    except Exception:  # noqa: BLE001
        pass
    try:
        dh = getattr(positions, "data_health", None)
        if isinstance(dh, dict):
            dh.setdefault("risk_sizing_diagnostics", []).append(record)
        else:
            # a plain list: stash on the first position so a runlog consumer can still find it.
            if positions:
                first = positions[0]
                if isinstance(first, dict):
                    first.setdefault("risk_sizing_diagnostics", []).append(record)
    except Exception:  # noqa: BLE001
        pass


def apply(positions: list[dict], budget: float, name_cap: float = 0.08,
          price_series_fn: Callable[[str], object] | None = None) -> list[dict]:
    """Re-size positions by risk: weight_i *= vol_mult_i (board lookup, or inverse-vol fallback for
    off-board names), scale the total invested by the selection-gross dial (de-gross in a poor
    selection regime, holding more cash), renormalize to that target, cap per name. Mutates + returns
    the list. Never changes membership — only how much of the budget each name takes.

    `price_series_fn` (DI): an override loader `ticker -> close Series | None` for the off-board
    inverse-vol fallback; defaults to the vendored price store. Injected in tests so no live path is
    referenced at test time.
    """
    if not positions or budget <= 0:
        return positions
    price_series_fn = price_series_fn or _default_price_series

    # NEW-SIZE-2 part (2): off-board names get an on-the-fly inverse-vol multiplier instead of a
    # silent neutral 1.0. Exact-key board lookup stays the PRIMARY path; the fallback only fills the
    # names the board doesn't cover.
    off_board = [p for p in positions if not on_board(p.get("ticker", ""))]
    fb = _fallback_size_mults(off_board, price_series_fn) if off_board else {}

    def _mult_for(p: dict) -> float:
        tkr = p.get("ticker", "")
        if on_board(tkr):
            return vol_mult(tkr)
        return fb.get(id(p), 1.0)          # off-board: inverse-vol estimate, or neutral 1.0 (no series)

    # NEW-SIZE-2 part (1): coverage diagnostic. If < half the book is on the board, the primary vol
    # lever is largely inert — surface it (VISIBLE) rather than laundering it as neutral sizing.
    n = len(positions)
    n_on = n - len(off_board)
    coverage = (n_on / n) if n else 1.0
    if coverage < _COVERAGE_FLOOR:
        _emit_diag(positions, {
            "kind": "board_coverage_low",
            "coverage": round(coverage, 3),
            "n_on_board": n_on, "n_total": n,
            "n_fallback_estimated": len(fb),
            "note": ("primary vol lens (macro board) covers <50% of the book; off-board names sized "
                     "by on-the-fly inverse-vol fallback where a price series exists, else neutral"),
        })

    raw = {}
    for p in positions:
        raw[id(p)] = max(0.0, float(p.get("weight", 0.0))) * _mult_for(p)
    tot = sum(raw.values())
    if tot <= 0:
        return positions

    # NEW-SIZE-3: degenerate equal-weight probe. If the vast majority of INPUT names share one
    # identical weight AND the vol multipliers carry ~no dispersion, the book is running pure
    # equal-weight (RC3 uniform-confluence x inert vol lever). We do NOT synthesize artificial
    # dispersion (that would fabricate conviction and violate the subtract-only invariant) — the
    # honest fix is the NEW-SIZE-2 fallback (restores the real vol lever) PLUS surfacing that the book
    # is equal-weight so it isn't mistaken for risk-managed sizing.
    in_weights = [max(0.0, float(p.get("weight", 0.0))) for p in positions]
    mults = [_mult_for(p) for p in positions]
    if n >= 3:
        from collections import Counter
        wshare = max(Counter(round(w, 6) for w in in_weights).values()) / n
        mult_spread = (max(mults) - min(mults)) if mults else 0.0
        if wshare > _EQW_SHARE and mult_spread <= 1e-6:
            _emit_diag(positions, {
                "kind": "degenerate_equal_weight",
                "shared_weight_fraction": round(wshare, 3),
                "vol_mult_spread": round(mult_spread, 6),
                "n_total": n,
                "note": ("book is running equal-weight: >80% of input names share one weight AND vol "
                         "dispersion ~0 — surfaced, not corrected (no synthetic dispersion added)"),
            })

    # ── NEW-SIZE-1: renorm is SCALE-DOWN-ONLY ────────────────────────────────────────────────────
    # The renorm target must never scale the book's gross UP — it may only scale DOWN to fit budget.
    # incoming_gross = the book's invested total BEFORE vol_mult (the sum of the pre-existing weights,
    # which already carry every upstream per-leg brake: the 0.7 initial-size catalyst haircut at
    # conviction.py, and any future ext_mult). Capping `target` at min(incoming_gross, budget)*gross
    # means vol_mult still REDISTRIBUTES weight across names (the risk lever is preserved) and
    # selection_gross can only DE-gross — but a book that arrived at ~0.0315 gross post-haircut stays
    # ~0.0315 and is never inflated back up to fill `budget`. Without this cap, `raw/tot*budget`
    # renormalises the post-haircut RELATIVE weights back up to the full budget, mathematically
    # cancelling the *0.7 (and it would erase ext_mult the same way). Renorm-down-only is the
    # invariant that lets every upstream subtract-only brake SURVIVE this stage.
    incoming_gross = sum(in_weights)
    target = min(incoming_gross, budget) * selection_gross()
    for p in positions:
        w = min(raw[id(p)] / tot * target, name_cap)      # per-name name_cap clamp preserved
        p["weight"] = round(w, 4)
        p["vol_mult"] = round(_mult_for(p), 2)
    return positions
