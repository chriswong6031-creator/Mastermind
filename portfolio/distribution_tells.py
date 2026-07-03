"""portfolio/distribution_tells.py — holdings-level DISTRIBUTION TELLS (W-I task 1).

WHY THIS MODULE EXISTS
----------------------
The 2026-07-02 semis breakdown post-mortem's central finding: the books sat 60-90% offensive on a
crowded semis/AI pile while the tape distributed under a held index. The bot had exit machinery
(W0 sells-first queue), concentration brakes (W2/W3 caps) and a severity ladder (W2 tripwire), but
NO machinery that reads *distribution in the names it already holds* and routes that evidence into
the response. This module is that reader.

It is DELIBERATELY NOT a per-name exit predictor. Two prior negative results constrain the design:
the cycle-phase veto on held leaders was walk-forward REFUTED, and the macro repo's own signal-engine
EXIT rule was a NO-GO. So this module ships in two honestly-separated roles:

  1. Distribution EVIDENCE routed through the ALREADY-VALIDATED severity ladder (bot/derisk.py). A
     book whose weight is heavily concentrated in distributing names earns +1 severity — no new
     prediction claim, just more evidence into a lever that is already proven and already caps at
     the ladder floor (0.55). This is a SHRINK-ONLY escalation; it can never un-cap or add risk.

  2. A per-name TRIM ladder that ships in SHADOW ONLY (``shadow_trim_ladder`` /
     ``write_shadow_trims``) — it emits recommendations to a data/shadow artifact with 21td
     falsifiers so the walk-forward can grade it before it is ever wired into sizing. The
     pre-registered promotion rule lives in ``shadow_trim_ladder``'s docstring.

THE FOUR TELLS (each per-holding, each degrade-safe → the tell is simply absent on missing data)
-----------------------------------------------------------------------------------------------
  * crowding      — the name's 60d return sits in the top pctile of its OWN history (>= 95th by
                    default), or the board's published ``pctile_252d`` where available (the dashboard's
                    own crowding read, preferred when present so bot and board agree).
  * macd_3d_bear  — the 3-day-bar RSI-MACD is in a BEARISH STATE (line < signal). State, not a fresh
                    cross: a fresh-cross timing read relocates ~80% of dates on a holiday/gap (canon's
                    session-grouped audit), whereas the bearish *state* is the robust, degrade-safe
                    read a distribution tell wants. Computed from the parquet store via an INJECTED
                    series fn (default: portfolio.paper_account._fetch_price_series) so tests can
                    fixture-inject without live-reading the shared mutating store.
  * macd_wk_bear  — the WEEKLY-bar RSI-MACD is in a bearish state (line < signal). Slower confirm.
  * def_rs_cross  — the defensive-vs-offensive RS differential has crossed positive (defensives now
                    out-performing offense). This is a BOOK-LEVEL regime tell (same for every holding
                    on a given day), reused BY the price-action nowcast (task 3) via the shared helper
                    ``defensive_offensive_rs_diff`` defined HERE — task 3 imports OURS. Owned here.

THE INVARIANT (governs every path)
----------------------------------
Missing/stale/wrong data COARSENS or SHRINKS — never un-caps, raises authority, or flips direction.
Every tell degrades to absent (never fabricated True). The escalator is SHRINK-ONLY: it can only add
severity, composed via max() into the existing ladder that caps at sev-3 (0.55). The trim ladder only
ever moves a name TOWARD its cap (down), never up, and it does not execute — it writes to shadow.

Pure / deterministic (given the artifacts + injected series fn) / degrade-never-raise.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Callable, Sequence

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_V = _ROOT / "vendor" / "macro"
_SHADOW = _ROOT / "data" / "shadow"

# The offensive / defensive RS baskets for the def-vs-offense differential. These MIRROR the
# incident autopsy's construction (signals.md §3: XLV+XLU+XLP defensives vs SMH+XLK offense) so the
# nowcast (task 3) and this module read ONE definition. Kept as module constants so a config edit
# touches one place; both consumers import this helper, never re-derive the baskets.
_DEFENSIVE_BASKET: tuple[str, ...] = ("XLV", "XLU", "XLP")
_OFFENSIVE_BASKET: tuple[str, ...] = ("SMH", "XLK")

# ---- default thresholds (mirrors doctrine.yml distribution_tells block; these are the degrade-safe
# fallbacks used when the config file / key is absent). All (unverified-prior). ----
_CROWD_PCTILE_MIN = 0.95        # 60d-return own-history pctile (or board pctile_252d) at/above this → crowded
_CROWD_LOOKBACK_D = 60          # the return horizon whose percentile defines "crowding"
_CROWD_PCTILE_WINDOW = 252      # trailing window the own-history percentile ranks within
_RS_DIFF_WINDOW = 20            # the RS differential lookback (task 3 coordinates on this default)
_MIN_TELLS_FOR_HOT = 2          # a holding is "distributing" at >= this many tells
_BOOK_WEIGHT_ESCALATE_FRAC = 0.25   # >= this fraction of book weight in distributing names → +1 severity


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def _cfg() -> dict:
    """The distribution_tells doctrine block, or {} on any miss (→ all fallbacks apply)."""
    try:
        from bot.doctrine_config import load_doctrine
        block = load_doctrine().get("distribution_tells")
        return block if isinstance(block, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _cfg_f(key: str, default: float) -> float:
    v = _cfg().get(key)
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _u(t: Any) -> str:
    return (str(t) if t is not None else "").upper().strip()


def _f(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _default_series_fn(ticker: str):
    """Default injected series fn: the engine price store reader. Returns a date-indexed close
    Series or None. Wrapped so a missing lib / cold store degrades to None (tell absent), never
    raises — tests fixture-inject a pure fn instead so the shared mutating store is never live-read."""
    try:
        from portfolio import paper_account
        return paper_account._fetch_price_series(ticker)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# board-published crowding (preferred when present so bot & board agree)
# ---------------------------------------------------------------------------

def _board_pctile_252d() -> dict[str, float]:
    """Map ticker -> published ``pctile_252d`` from the dashboard sector_rs table (the board's OWN
    crowding read). {} on any miss → the crowding tell falls back to own-history percentile."""
    try:
        p = _V / "data" / "regime" / "latest.json"
        if not p.exists():
            return {}
        r = json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, float] = {}
    for row in (r.get("sector_rs") or []):
        if not isinstance(row, dict):
            continue
        tk = _u(row.get("ticker"))
        pc = _f(row.get("pctile_252d"))
        if tk and pc is not None:
            # the board publishes 0-100 or 0-1 depending on product; normalize to 0-1
            out[tk] = pc / 100.0 if pc > 1.0 else pc
    return out


# ---------------------------------------------------------------------------
# the per-holding tells (each degrade-safe → absent on missing data)
# ---------------------------------------------------------------------------

def _crowding_tell(ticker: str, series, board_pctile: dict[str, float]) -> float | None:
    """Crowding percentile in [0,1], or None if undeterminable. Prefers the board's published
    ``pctile_252d`` (bot & board agree); falls back to the name's OWN 60d-return percentile within a
    trailing 252d window computed from the injected series."""
    tk = _u(ticker)
    if tk in board_pctile:
        return board_pctile[tk]
    if series is None:
        return None
    try:
        import pandas as pd  # noqa: F401
        s = series.astype(float).dropna().sort_index()
        lb = int(_cfg_f("crowd_lookback_d", _CROWD_LOOKBACK_D))
        win = int(_cfg_f("crowd_pctile_window", _CROWD_PCTILE_WINDOW))
        if len(s) < lb + 2:
            return None
        ret = s.pct_change(lb).dropna()
        if ret.empty:
            return None
        tail = ret.tail(win)
        if len(tail) < 2:
            return None
        # percentile of the most-recent 60d return within the trailing window (0-1)
        last = float(tail.iloc[-1])
        return float((tail <= last).mean())
    except Exception:  # noqa: BLE001
        return None


def _macd_bear_state(series, timeframe: str) -> bool | None:
    """3D or WEEKLY RSI-MACD bearish STATE (line < signal) at the latest bar, or None if
    undeterminable. Uses canon's session-grouped 3D grid / W-FRI weekly + the SMA-seeded RMA +
    adjust=False EMA the validated confluence cascade rides on — ONE definition of the MACD, imported
    not re-implemented. State (not a fresh cross) is the robust read (canon's audit: fresh-cross dates
    relocate ~80% on gaps; the bearish state does not)."""
    if series is None:
        return None
    try:
        from engine import canon
        import pandas as pd
        s = pd.to_numeric(series, errors="coerce").astype(float).dropna().sort_index()
        if timeframe == "3d":
            bars, _known = canon.resample_sessions(s, 3)
        elif timeframe == "wk":
            bars, _known = canon._resample_weekly(s, "W-FRI")
        else:
            return None
        if bars is None or len(bars) < (canon.BASE_LEN + canon.SIG_LEN + 2):
            return None
        macd, sig = canon.rsi_macd(bars)
        m, g = macd.dropna(), sig.dropna()
        if m.empty or g.empty:
            return None
        # align on the last common bar
        last = m.index[-1]
        if last not in g.index:
            common = m.index.intersection(g.index)
            if len(common) == 0:
                return None
            last = common[-1]
        return bool(float(m.loc[last]) < float(g.loc[last]))
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# defensive-vs-offensive RS differential — OWNED HERE; task 3 imports THIS
# ---------------------------------------------------------------------------

def defensive_offensive_rs_diff(
    window: int = _RS_DIFF_WINDOW,
    series_fn: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """The defensive-minus-offensive relative-strength differential over *window* trading days.

    This is the SHARED helper the incident wave coordinates on: the distribution tell reads it (a
    positive differential is the ``def_rs_cross`` tell), AND the price-action nowcast (task 3) imports
    THIS SAME function so the two builds never derive two different RS diffs. Owned here.

    Construction (mirrors the incident autopsy, signals.md §3):
      def_rs = mean over the DEFENSIVE basket of each name's window-day total return
      off_rs = mean over the OFFENSIVE basket of each name's window-day total return
      diff   = def_rs − off_rs   (> 0 ⇒ defensives outperforming offense ⇒ rotation)

    Returns ``{diff, def_rs, off_rs, crossed, window, n_def, n_off}``. ``crossed`` is True when
    diff > 0 (defensives leading). Degrade-safe: any basket that yields zero usable names →
    ``{diff: None, crossed: None, ...}`` (the tell is ABSENT, never fabricated). Never raises.
    """
    fn = series_fn if series_fn is not None else _default_series_fn

    def _basket_ret(basket: Sequence[str]) -> tuple[float | None, int]:
        rets: list[float] = []
        for tk in basket:
            try:
                s = fn(tk)
            except Exception:  # noqa: BLE001
                s = None
            if s is None:
                continue
            try:
                import pandas as pd  # noqa: F401
                ss = s.astype(float).dropna().sort_index()
                if len(ss) < window + 1:
                    continue
                r = float(ss.iloc[-1] / ss.iloc[-1 - window] - 1.0)
                rets.append(r)
            except Exception:  # noqa: BLE001
                continue
        if not rets:
            return None, 0
        return sum(rets) / len(rets), len(rets)

    def_rs, n_def = _basket_ret(_DEFENSIVE_BASKET)
    off_rs, n_off = _basket_ret(_OFFENSIVE_BASKET)
    if def_rs is None or off_rs is None:
        return {"diff": None, "def_rs": def_rs, "off_rs": off_rs, "crossed": None,
                "window": window, "n_def": n_def, "n_off": n_off}
    diff = def_rs - off_rs
    return {"diff": round(diff, 6), "def_rs": round(def_rs, 6), "off_rs": round(off_rs, 6),
            "crossed": bool(diff > 0.0), "window": window, "n_def": n_def, "n_off": n_off}


# ---------------------------------------------------------------------------
# public API: per-holding scoring
# ---------------------------------------------------------------------------

def score(
    holdings: Sequence[dict] | None,
    prices_fn: Callable[[str], Any] | None = None,
    *,
    rs_diff: dict | None = None,
) -> dict[str, Any]:
    """Score DISTRIBUTION TELLS across the book's holdings.

    Parameters
    ----------
    holdings : sequence of {ticker, current_weight, ...}
        The open positions (e.g. ``position_log.open_positions()`` rows). None/empty → an empty,
        legal 'no tells' result. Weights are used to compute the book-weight-in-distributing-names
        fraction that drives the escalator.
    prices_fn : callable ticker -> date-indexed close Series | None
        INJECTED price series reader. None → the engine price store (``_default_series_fn``). Tests
        fixture-inject a pure fn so the shared mutating store is never live-read.
    rs_diff : dict | None
        A pre-computed ``defensive_offensive_rs_diff`` result (so callers that already computed it —
        e.g. the nowcast — don't pay for it twice). None → computed here with ``prices_fn``.

    Returns
    -------
    {
      "asof": ...,                       # informational (caller-independent)
      "rs_diff": {...},                  # the book-level def-vs-offense differential (task-3 shared)
      "def_rs_cross": bool | None,       # book-level tell (same for every holding)
      "holdings": [ {ticker, weight, tells:{...}, n_tells, distributing, reason}, ... ],
      "distributing_weight_frac": float, # fraction of book weight in >=2-tell names
      "hot": bool,                       # distributing_weight_frac >= escalate_frac
      "escalate_severity": int,          # +1 when hot, else 0 (SHRINK-ONLY into the ladder)
      "reason": str,                     # names the tells, e.g. 'distribution: SMH crowd99+3D-MACD-bear'
    }

    Degrade: every tell independently degrades to absent on missing data; a holding with no
    determinable tells simply carries ``n_tells: 0``. NEVER raises.
    """
    fn = prices_fn if prices_fn is not None else _default_series_fn
    crowd_min = _cfg_f("crowd_pctile_min", _CROWD_PCTILE_MIN)
    min_tells = int(_cfg_f("min_tells_for_hot", _MIN_TELLS_FOR_HOT))
    escalate_frac = _cfg_f("book_weight_escalate_frac", _BOOK_WEIGHT_ESCALATE_FRAC)
    rs_window = int(_cfg_f("rs_diff_window", _RS_DIFF_WINDOW))

    # book-level def-vs-offense RS differential (same for every holding)
    if rs_diff is None:
        try:
            rs_diff = defensive_offensive_rs_diff(window=rs_window, series_fn=fn)
        except Exception:  # noqa: BLE001
            rs_diff = {"diff": None, "crossed": None}
    def_rs_cross = rs_diff.get("crossed") if isinstance(rs_diff, dict) else None

    board_pctile = _board_pctile_252d()

    rows: list[dict[str, Any]] = []
    distributing_weight = 0.0
    total_weight = 0.0
    hot_names: list[str] = []
    for h in (holdings or []):
        if not isinstance(h, dict):
            continue
        tk = _u(h.get("ticker"))
        if not tk:
            continue
        w = _f(h.get("current_weight")) or _f(h.get("weight")) or 0.0
        total_weight += w
        try:
            series = fn(tk)
        except Exception:  # noqa: BLE001
            series = None

        crowd_pct = _crowding_tell(tk, series, board_pctile)
        macd3d = _macd_bear_state(series, "3d")
        macdwk = _macd_bear_state(series, "wk")

        tells: dict[str, Any] = {
            "crowding": (crowd_pct is not None and crowd_pct >= crowd_min),
            "crowding_pctile": (round(crowd_pct, 4) if crowd_pct is not None else None),
            "macd_3d_bear": macd3d,       # True/False/None
            "macd_wk_bear": macdwk,       # True/False/None
            "def_rs_cross": (bool(def_rs_cross) if def_rs_cross is not None else None),
        }
        # count only DETERMINED, TRUE tells (None never counts — absent data can't fire a tell)
        fired: list[str] = []
        if tells["crowding"] is True:
            fired.append(f"crowd{int(round((crowd_pct or 0) * 100))}")
        if macd3d is True:
            fired.append("3D-MACD-bear")
        if macdwk is True:
            fired.append("W-MACD-bear")
        if def_rs_cross is True:
            fired.append("def-RS-cross")
        n_tells = len(fired)
        distributing = n_tells >= min_tells
        if distributing:
            distributing_weight += w
            hot_names.append(f"{tk} {'+'.join(fired)}")
        rows.append({
            "ticker": tk, "weight": round(w, 6), "tells": tells,
            "n_tells": n_tells, "fired": fired, "distributing": distributing,
        })

    frac = round(distributing_weight / total_weight, 6) if total_weight > 0 else 0.0
    hot = frac >= escalate_frac and frac > 0.0
    reason = ""
    if hot and hot_names:
        # name the tells in the escalation reason (spec 1b), e.g.
        # 'distribution: SMH crowd99+3D-MACD-bear (28% book weight, 1 name)'
        lead = "; ".join(hot_names[:3])
        reason = (f"distribution: {lead} "
                  f"({frac * 100:.0f}% book weight, {len(hot_names)} name{'s' if len(hot_names) != 1 else ''})")

    return {
        "asof": date.today().isoformat(),
        "rs_diff": rs_diff,
        "def_rs_cross": (bool(def_rs_cross) if def_rs_cross is not None else None),
        "holdings": rows,
        "distributing_weight_frac": frac,
        "hot": hot,
        "escalate_severity": 1 if hot else 0,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# SHADOW per-name trim ladder — emits recommendations, NEVER executes
# ---------------------------------------------------------------------------

def shadow_trim_ladder(
    holdings: Sequence[dict] | None,
    prices_fn: Callable[[str], Any] | None = None,
    *,
    name_cap: float = 0.08,
    scored: dict | None = None,
) -> list[dict[str, Any]]:
    """A per-name TRIM ladder that ships in SHADOW ONLY — it EMITS recommendations, it does not size.

    PRE-REGISTERED FALSIFIER (the load-bearing contract, mirrors the walk-forward that killed the
    macro repo's exit rule and the refuted cycle-phase veto):

        The per-name trim ladder promotes from shadow to live sizing ONLY IF, over >= 40 shadow-graded
        trim calls, the trimmed sleeve beats HOLD on RISK-ADJUSTED forward returns (Sharpe / drawdown
        -normalized) measured over a 21-trading-day forward window. Fewer than 40 graded calls, or a
        risk-adjusted improvement that is not positive, keeps it in shadow. A raw-return improvement
        alone does NOT promote — the prior exit rule looked good on raw return and failed risk-adjusted,
        which is exactly why this is the pre-committed gate.

    The ladder recommends QUARTER-POSITION steps toward the name cap for each DISTRIBUTING holding
    (>= min_tells tells), never an outright exit, never a raise. A name already at/under the cap gets
    no recommendation. Deterministic; degrade-safe; NEVER executes and NEVER raises.

    Returns a list of ``{ticker, from_weight, to_weight, step, target_cap, tells, falsifier_by,
    n_tells}`` rows. ``falsifier_by`` is the asof + 21 trading days (informational stamp; the grader
    resolves it). Empty list when nothing distributes.
    """
    fn = prices_fn if prices_fn is not None else _default_series_fn
    sc = scored if scored is not None else score(holdings, fn)
    rows = sc.get("holdings") or []
    out: list[dict[str, Any]] = []
    for r in rows:
        if not r.get("distributing"):
            continue
        w = _f(r.get("weight")) or 0.0
        if w <= name_cap + 1e-9:
            continue  # already at/under cap — nothing to trim toward
        # quarter-position step toward the cap (never below the cap in one step)
        step_size = w * 0.25
        to_w = max(w - step_size, name_cap)
        out.append({
            "ticker": r.get("ticker"),
            "from_weight": round(w, 6),
            "to_weight": round(to_w, 6),
            "step": round(w - to_w, 6),
            "target_cap": name_cap,
            "n_tells": r.get("n_tells"),
            "tells": r.get("fired"),
            "falsifier_by": _plus_trading_days(sc.get("asof"), 21),
            "graded": False,
        })
    return out


def write_shadow_trims(
    holdings: Sequence[dict] | None,
    prices_fn: Callable[[str], Any] | None = None,
    *,
    portfolio_id: str = "flagship",
    asof: str | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Persist the shadow trim recommendations to data/shadow/distribution_trims/<asof>_<pid>.json so
    the walk-forward grader can resolve them against the 21td falsifier. SHADOW artifact only — no
    sizing is touched. Returns the written payload (also on a no-op write). Never raises."""
    asof = asof or date.today().isoformat()
    scored = score(holdings, prices_fn)
    trims = shadow_trim_ladder(holdings, prices_fn, scored=scored)
    payload = {
        "asof": asof,
        "portfolio_id": portfolio_id,
        "escalate_severity": scored.get("escalate_severity"),
        "distributing_weight_frac": scored.get("distributing_weight_frac"),
        "reason": scored.get("reason"),
        "trims": trims,
        "falsifier": (
            "SHADOW: promotes to live sizing ONLY if >=40 graded trims beat HOLD on risk-adjusted "
            "(Sharpe/drawdown-normalized) forward returns over a 21td window. Raw-return improvement "
            "alone does NOT promote (the prior exit rule failed exactly there)."
        ),
    }
    try:
        d = out_dir if out_dir is not None else (_SHADOW / "distribution_trims")
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{str(asof)[:10]}_{portfolio_id}.json").write_text(
            json.dumps(payload, indent=2, default=str)
        )
    except Exception:  # noqa: BLE001
        pass
    return payload


def _plus_trading_days(asof: Any, n: int) -> str | None:
    """asof + n TRADING days (NYSE calendar when available; business-day fallback otherwise).
    Informational falsifier stamp only — the grader resolves the actual forward window. None on an
    unparseable asof."""
    try:
        import pandas as pd
        base = pd.Timestamp(str(asof)[:10])
    except Exception:  # noqa: BLE001
        return None
    try:
        from portfolio import market_calendar
        d = base.date()
        for _ in range(n):
            d = market_calendar.next_trading_day(d)
        return d.isoformat()
    except Exception:  # noqa: BLE001
        try:
            import pandas as pd
            return (base + pd.tseries.offsets.BDay(n)).date().isoformat()
        except Exception:  # noqa: BLE001
            return None
