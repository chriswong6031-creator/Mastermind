"""brain/regime_nowcast.py — the price-action regime NOWCAST (Incident Wave W-I, task 3).

WHY THIS MODULE EXISTS
----------------------
On 2026-07-01 the dashboard's regime label read ``Q1 Goldilocks / transition STABLE /
liquidity_overlay expanding / risk receding`` — and every US book bought that story, adding
$24.8k of SMH the day before semis printed a −6.4% session.  The dashboard ALSO published the
right story on planes the bot did not trust: sector_cycles read ``Technology Topping / SELL`` on
the RS leader, its own risk_radar read ``CAUTION: growth scare / defensive rotation (91/100)``,
and a trivial price-action read — defensive sectors out-performing offense, SMH momentum rolling
over, breadth deteriorating — said "reduce offense" days early.  The label lagged; the price tape
did not.

This module is the SECOND OPINION.  It re-derives a coarse regime read from PRICE ACTION alone —
three legs, no dependence on the (regressed) macro label — and returns a *doubt* verdict that a
consumer may use ONLY to SHRINK offense.  It is STANDALONE: it does NOT wire into ``budget()`` or
the DEF_SLEEVE here (task 6 wires it, behind its validation gate — see VALIDATION below).

THE ONE INVARIANT (governs every path here)
-------------------------------------------
Missing / stale / wrong data may coarsen, freeze, or SHRINK — never un-cap, raise authority, or
flip direction.  The nowcast is SHRINK-ONLY BY CONSTRUCTION in two senses:

  1. It can only ever emit ``confirm`` (no-op) or ``doubt`` / ``strong-doubt``.  There is no
     "confidence" / "add-risk" verdict — the module has no vocabulary to raise offense.
  2. It applies ONLY when the regime label is risk-on / Goldilocks-class (an OFFENSIVE quad).
     When the label is already defensive (caution / risk_off / Quad-4 / stagflation) the nowcast
     is a NO-OP: it can DOUBT offense, it can never doubt defense.  A second opinion that could
     talk a defensive book back into offense would be exactly the failure mode — so it cannot.

  A missing leg degrades that leg to ``None`` (it does NOT count toward the doubt tally — absent
  data can never *fire* a doubt), so a total data outage yields ``confirm`` (no shrink) — the
  conservative direction for a shrink-only signal is "do nothing", never "doubt on no evidence".

THE THREE LEGS (signals.md §3 — the incident autopsy's exact construction)
--------------------------------------------------------------------------
  (1) DEFENSIVE-vs-OFFENSIVE 20d RS differential — mean 20d total return of {XLV, XLU, XLP} minus
      mean 20d total return of {SMH, XLK}; > 0 ⇒ defensives leading ⇒ rotation.  IMPORTED from
      ``portfolio.distribution_tells.defensive_offensive_rs_diff`` (task 1 owns the basket + the
      construction) so the nowcast and the distribution escalator never derive two different RS
      diffs.  Back-tested: flipped positive 2026-06-24, durable crossover from 06-30.

  (2) SMH-or-top-RS-sector 3D-MACD BEARISH STATE — the offensive leader's 3-day MACD line below
      its signal.  IMPORTED from ``portfolio.distribution_tells._macd_bear_state(series, "3d")``,
      which rides canon's ONE MACD definition (SMA-seeded RMA + adjust=False EMA on the
      session-grouped 3D grid).  We use the STATE (line<signal), not a fresh cross: canon's audit
      showed fresh-cross dates relocate ~80% on gaps while the state does not — the robust read.

  (3) BREADTH — %sectors above their 50d SMA, FALLING over 5 sessions.  Owned here (a simple
      market-internals read, no shared surface).  Back-tested: %>50d 73→45→55 into 07-01.

  TALLY: 2-of-3 legs TRUE ⇒ ``doubt``; 3-of-3 ⇒ ``strong-doubt``; else ``confirm``.  Back-tested
  composite: soft (2-of-3) doubt present by 2026-06-24; hard (3-of-3) strong-doubt on 2026-07-01
  (the day before the SMH rebuy) and durably through it; a calm 2025-05 offense-led uptrend never
  reaches 2-of-3 (see tests/test_regime_nowcast.py + nowcast_validation.md).

VALIDATION STATUS (pre-registered — read nowcast_validation.md before trusting the wiring)
------------------------------------------------------------------------------------------
A pre-registered walk-forward (2015-2026, monthly + doubt-session, on the Macro-Dashboard yahoo
parquet) tests whether ``doubt`` episodes actually precede offense-minus-defense underperformance.
The gate and its result are recorded in ``research/incidents/2026-07-02-semis-breakdown/
nowcast_validation.md``.  ``BUDGET_INPUT_QUALIFIED`` below is set from that result:

  * If the gate PASSES, task 6 may wire the nowcast as a SHRINK-ONLY input to ``budget()``.
  * If the gate FAILS, the module STILL SHIPS but ``BUDGET_INPUT_QUALIFIED = False`` marks it
    ADVISORY-ONLY: a lens row + a DEF_SLEEVE-unthrottle input (raising the DEFENSIVE floor when the
    label lies), NOT a budget input.  Either way it is shrink-only; the flag governs only *which*
    shrink-only surface task 6 is permitted to wire.

ALL THRESHOLDS live in config/doctrine.yml ``regime_nowcast:`` (each an ``(unverified-prior)``);
the ``_FALLBACK`` dict below mirrors them for the PyYAML-absent / malformed-block case.

PUBLIC API
----------
* ``nowcast(series_fn=None, *, quad=None, quad_name=None, risk_state=None, asof=None) -> dict``
      the sole decision; ``series_fn(ticker) -> close Series | None`` is fixture-injectable.
* ``legs(series_fn=None, *, asof=None) -> dict``   — the three raw leg reads (no label gate);
      exposed so the lens row / tests can inspect the legs without the risk-on gate.
* ``default_series_fn`` — the production price reader (reused from distribution_tells so the two
      builds share ONE store surface).  Tests inject a synthetic series_fn instead.

Pure / deterministic (given the injected series) / degrade-never-raise.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# The module lives at brain/regime_nowcast.py; the repo root is two levels up.
_ROOT: Path = Path(__file__).resolve().parent.parent
_DOCTRINE_PATH: Path = _ROOT / "config" / "doctrine.yml"

# ── the pre-registered validation flag (set from nowcast_validation.md) ───────────────────────────
# When True, task 6 may wire the nowcast as a SHRINK-ONLY input to budget().  When False it is
# advisory-only (lens row + DEF_SLEEVE unthrottle).  See the VALIDATION section of the module
# docstring and nowcast_validation.md.  Conservative default: advisory-only until the gate is met.
BUDGET_INPUT_QUALIFIED: bool = False

# ── the market-breadth sector universe (%sectors>50d) — the 11 GICS SPDR sectors ─────────────────
# Owned here; it is a market-internals read, not a shared surface.  A sector whose series is absent
# simply doesn't vote (the fraction is over the sectors we CAN read) — never a fabricated default.
_BREADTH_SECTORS: tuple[str, ...] = (
    "XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
)

# ── which regime labels are "risk-on / Goldilocks-class" (the ONLY labels the nowcast acts on) ────
# Offensive quads: Q1 Goldilocks (growth up / inflation down) and Q2 Reflation (growth up / inflation
# up).  A defensive quad (Q3 Stagflation, Q4 Deflation) or an explicit caution/risk_off risk_state
# means the label already agrees with caution — nothing to doubt (shrink-only ⇒ no-op).
_RISK_ON_QUADS: frozenset[str] = frozenset({"Q1", "Q2"})
_RISK_ON_QUAD_NAMES: frozenset[str] = frozenset({"goldilocks", "reflation"})
_DEFENSIVE_RISK_STATES: frozenset[str] = frozenset(
    {"caution", "risk_off", "risk-off", "elevated", "stress", "stressed"}
)

# Fallback thresholds — MUST mirror config/doctrine.yml's `regime_nowcast:` block.  Used ONLY when
# PyYAML is absent or the block is malformed; the yml is the source of truth.
_FALLBACK: dict[str, Any] = {
    "rs_diff_window": 20,     # (unverified-prior) defensive-vs-offensive RS lookback (trading days)
    "breadth_ma": 50,        # (unverified-prior) sector trend MA for %sectors-above (trading days)
    "breadth_falling_lookback": 5,  # (unverified-prior) %>50d must FALL over this many sessions
    "doubt_legs": 2,         # (unverified-prior) legs TRUE for `doubt`
    "strong_doubt_legs": 3,  # (unverified-prior) legs TRUE for `strong-doubt`
}


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------
def _cfg() -> dict[str, Any]:
    """Read config/doctrine.yml ``regime_nowcast:``, merged over the _FALLBACK prior.

    Lazy and defensive — a missing PyYAML / config / block leaves every key at its
    (unverified-prior) fallback so a bare CI or a malformed yml never breaks a build.
    """
    cfg = dict(_FALLBACK)
    try:
        import yaml  # lazy — not on the hot path; gracefully absent in bare CI
        if _DOCTRINE_PATH.exists():
            block = (yaml.safe_load(_DOCTRINE_PATH.read_text()) or {}).get("regime_nowcast")
            if isinstance(block, dict):
                cfg.update({k: v for k, v in block.items() if v is not None})
    except Exception:  # noqa: BLE001 — degrade to the fallback prior
        pass
    return cfg


# ---------------------------------------------------------------------------
# the default production price reader — REUSED from distribution_tells so both
# builds share ONE store surface.  Tests inject a synthetic series_fn instead.
# ---------------------------------------------------------------------------
def default_series_fn(ticker: str) -> Any:
    """Default injected price reader: distribution_tells' engine-store reader.

    Returns a date-indexed close Series or None.  Reused (not re-implemented) so the nowcast and
    the distribution escalator read the SAME prices from the SAME store.  Degrades to None on a
    missing lib / cold store — never raises.  Tests fixture-inject a pure fn.
    """
    try:
        from portfolio import distribution_tells as _dt
        return _dt._default_series_fn(ticker)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# leg 3 — breadth (%sectors above 50d SMA, falling over N sessions).  Owned here.
# ---------------------------------------------------------------------------
def _pct_above_ma(series_fn: Callable[[str], Any], ma: int, asof: Any) -> Optional[float]:
    """%sectors whose latest close (<= *asof*) is above their trailing *ma*-day SMA, in [0,100].

    None when NO sector has enough history — the leg is then undeterminable (absent, never a
    fabricated 50).  A sector that can't be read simply doesn't vote; the fraction is over the
    sectors we CAN read (so a partial universe still yields an honest internal read).
    """
    import pandas as pd

    votes: list[float] = []
    for tk in _BREADTH_SECTORS:
        try:
            s = series_fn(tk)
        except Exception:  # noqa: BLE001
            s = None
        if s is None:
            continue
        try:
            ss = pd.to_numeric(s, errors="coerce").astype(float).dropna().sort_index()
            if asof is not None:
                ss = ss[ss.index <= asof]
            if len(ss) < ma + 1:
                continue
            votes.append(1.0 if float(ss.iloc[-1]) > float(ss.iloc[-ma:].mean()) else 0.0)
        except Exception:  # noqa: BLE001
            continue
    if not votes:
        return None
    return 100.0 * sum(votes) / len(votes)


def _breadth_falling(series_fn: Callable[[str], Any], cfg: dict[str, Any], asof: Any) -> Optional[bool]:
    """True when %sectors>MA is LOWER than it was *lookback* sessions ago (deteriorating breadth).

    Computed on the OFFENSIVE leader's own session axis (SMH → SPY fallback) so "5 sessions ago"
    means 5 trading days, not 5 calendar days.  None when the axis or either breadth read is
    undeterminable (the leg is absent — it cannot fire a doubt on missing data).
    """
    import pandas as pd

    ma = int(cfg["breadth_ma"])
    lb = int(cfg["breadth_falling_lookback"])

    # session axis: prefer SMH (the leader), fall back to SPY (the tape), then any breadth sector.
    axis = None
    for anchor in ("SMH", "SPY", "XLK"):
        try:
            a = series_fn(anchor)
        except Exception:  # noqa: BLE001
            a = None
        if a is None:
            continue
        try:
            aa = pd.to_numeric(a, errors="coerce").astype(float).dropna().sort_index()
            if asof is not None:
                aa = aa[aa.index <= asof]
            if len(aa) >= lb + 1:
                axis = aa.index
                break
        except Exception:  # noqa: BLE001
            continue
    if axis is None:
        return None

    now = _pct_above_ma(series_fn, ma, axis[-1])
    prev = _pct_above_ma(series_fn, ma, axis[-1 - lb])
    if now is None or prev is None:
        return None
    return bool(now < prev)


# ---------------------------------------------------------------------------
# the three legs
# ---------------------------------------------------------------------------
def legs(
    series_fn: Optional[Callable[[str], Any]] = None,
    *,
    asof: Any = None,
) -> dict[str, Any]:
    """The three raw leg reads, WITHOUT the risk-on label gate.

    Returns::

        {
          "rs_cross":       bool | None,   # leg 1: defensive-offensive 20d RS diff > 0
          "rs_diff":        float | None,  # the diff itself (informational)
          "smh_3d_bear":    bool | None,   # leg 2: SMH 3D-MACD line < signal (state)
          "breadth_falling":bool | None,   # leg 3: %sectors>50d falling over 5 sessions
          "n_doubt":        int,           # count of legs that are TRUE (None never counts)
          "asof":           the asof passed in (informational),
        }

    Each leg degrades INDEPENDENTLY to None on missing data; None NEVER counts toward n_doubt
    (absent data cannot fire a doubt).  Never raises.  ``asof`` (a Timestamp-like) restricts every
    series to closes at or before it — used by the walk-forward and the replay tests; None means
    "use the full injected series" (production: the latest bar).
    """
    fn = series_fn if series_fn is not None else default_series_fn
    cfg = _cfg()

    # leg 1 — shared RS differential (task 1 owns the construction + basket)
    rs_cross: Optional[bool] = None
    rs_diff_val: Optional[float] = None
    try:
        from portfolio import distribution_tells as _dt

        def _asof_fn(tk: str) -> Any:
            s = fn(tk)
            if s is None or asof is None:
                return s
            try:
                return s[s.index <= asof]
            except Exception:  # noqa: BLE001
                return s

        rs = _dt.defensive_offensive_rs_diff(
            window=int(cfg["rs_diff_window"]), series_fn=_asof_fn
        )
        if isinstance(rs, dict):
            rs_cross = rs.get("crossed")
            rs_diff_val = rs.get("diff")
    except Exception:  # noqa: BLE001 — a missing shared helper leaves the leg absent
        rs_cross, rs_diff_val = None, None

    # leg 2 — SMH 3D-MACD bearish STATE (shared canon MACD via task 1's helper)
    smh_3d_bear: Optional[bool] = None
    try:
        from portfolio import distribution_tells as _dt

        smh = fn("SMH")
        if smh is not None and asof is not None:
            try:
                smh = smh[smh.index <= asof]
            except Exception:  # noqa: BLE001
                pass
        smh_3d_bear = _dt._macd_bear_state(smh, "3d")
    except Exception:  # noqa: BLE001
        smh_3d_bear = None

    # leg 3 — breadth deteriorating (owned here)
    try:
        breadth_falling = _breadth_falling(fn, cfg, asof)
    except Exception:  # noqa: BLE001
        breadth_falling = None

    n_doubt = sum(1 for x in (rs_cross, smh_3d_bear, breadth_falling) if x is True)
    return {
        "rs_cross": rs_cross,
        "rs_diff": rs_diff_val,
        "smh_3d_bear": smh_3d_bear,
        "breadth_falling": breadth_falling,
        "n_doubt": n_doubt,
        "asof": asof,
    }


# ---------------------------------------------------------------------------
# the label gate
# ---------------------------------------------------------------------------
def _label_is_risk_on(quad: Any, quad_name: Any, risk_state: Any) -> bool:
    """True when the regime label is risk-on / Goldilocks-class — the ONLY case the nowcast acts on.

    A label is risk-on when its quad is offensive (Q1/Q2 or name Goldilocks/Reflation) AND its
    risk_state is not an explicit defensive token (caution / risk_off / elevated / stress).  When
    NO label is supplied at all (all three None), we treat it as risk-on — the caller asked the
    nowcast to run, and the incident's whole lesson is that the *absence* of a trustworthy caution
    read is exactly when the price-tape second opinion matters most.  A supplied DEFENSIVE label
    is respected: the nowcast no-ops (it can only doubt offense).
    """
    rs = str(risk_state).lower().strip() if risk_state is not None else None
    if rs in _DEFENSIVE_RISK_STATES:
        return False  # label already defensive → nothing to doubt

    q = str(quad).upper().strip() if quad is not None else None
    qn = str(quad_name).lower().strip() if quad_name is not None else None

    # An explicitly DEFENSIVE quad (anything offensive-set-excluded) blocks the nowcast — but only
    # when a quad was actually supplied.  A None quad is "unknown", handled below.
    if q is not None and q not in _RISK_ON_QUADS:
        return False
    if qn is not None and qn not in _RISK_ON_QUAD_NAMES:
        # a named non-offensive regime (stagflation/deflation/growth-scare) is defensive
        return False

    # Offensive label supplied, OR no label at all (unknown) — the nowcast is permitted to doubt.
    return True


# ---------------------------------------------------------------------------
# the nowcast
# ---------------------------------------------------------------------------
def nowcast(
    series_fn: Optional[Callable[[str], Any]] = None,
    *,
    quad: Any = None,
    quad_name: Any = None,
    risk_state: Any = None,
    asof: Any = None,
) -> dict[str, Any]:
    """The price-action regime nowcast — the sole public decision.

    Parameters
    ----------
    series_fn : callable ticker -> date-indexed close Series | None
        INJECTED price reader.  None → the production store reader (``default_series_fn``).  Tests
        fixture-inject a pure fn so the shared mutating store is never live-read.
    quad, quad_name, risk_state :
        The current regime label (from ``regime_frame.frame()`` / macro_risk).  Passed IN (not
        live-read) so the module stays standalone and the tests never pin today's regime.  The
        nowcast acts ONLY on a risk-on / Goldilocks-class label; a defensive label no-ops it.
    asof :
        Optional Timestamp-like; restricts every series to closes at or before it (the walk-forward
        and replay path).  None → the latest bar of the injected series (production).

    Returns
    -------
    {
      "stance":  "confirm" | "doubt" | "strong-doubt",
      "legs":    {rs_cross, rs_diff, smh_3d_bear, breadth_falling, n_doubt, asof},
      "applies": bool,     # False when the label gate no-ops the nowcast
      "reason":  str,      # human-readable ("2/3 price-action legs doubt offense: ..." / gate note)
      "budget_input_qualified": bool,   # mirror of BUDGET_INPUT_QUALIFIED (task-6 wiring gate)
    }

    INVARIANT: the only non-``confirm`` outputs are ``doubt`` / ``strong-doubt``, both of which a
    consumer may use ONLY to SHRINK offense (or raise the DEFENSIVE floor).  A defensive label, a
    total data outage, or fewer than ``doubt_legs`` firing all yield ``confirm`` (no shrink).
    """
    cfg = _cfg()
    leg_reads = legs(series_fn, asof=asof)
    applies = _label_is_risk_on(quad, quad_name, risk_state)

    if not applies:
        return {
            "stance": "confirm",
            "legs": leg_reads,
            "applies": False,
            "reason": (
                "label already defensive (risk_state="
                f"{risk_state!r}, quad={quad!r}) — nowcast no-ops (can only doubt offense)"
            ),
            "budget_input_qualified": BUDGET_INPUT_QUALIFIED,
        }

    n = int(leg_reads["n_doubt"])
    doubt_n = int(cfg["doubt_legs"])
    strong_n = int(cfg["strong_doubt_legs"])
    if n >= strong_n:
        stance = "strong-doubt"
    elif n >= doubt_n:
        stance = "doubt"
    else:
        stance = "confirm"

    fired = []
    if leg_reads["rs_cross"] is True:
        fired.append("defensive-RS-cross")
    if leg_reads["smh_3d_bear"] is True:
        fired.append("SMH-3D-MACD-bear")
    if leg_reads["breadth_falling"] is True:
        fired.append("breadth-falling")
    if stance == "confirm":
        reason = f"{n}/3 price-action legs doubt offense — below the {doubt_n}/3 doubt bar"
    else:
        reason = f"{stance}: {n}/3 price-action legs ({', '.join(fired)}) — shrink offense only"

    return {
        "stance": stance,
        "legs": leg_reads,
        "applies": True,
        "reason": reason,
        "budget_input_qualified": BUDGET_INPUT_QUALIFIED,
    }
