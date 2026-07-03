"""portfolio/rotation.py — the deterministic DEF_SLEEVE rotation floor (W4, task B2).

WHY THIS MODULE EXISTS
----------------------
This is the masterplan's PRE-COMMITTED E1-failure branch (architecture Stage 4). Two roles:

  1. If the armed PM (the additive judgment seat, task B1) turns out to ECHO the engine —
     i.e. it diverges too little to earn promotion — this deterministic sleeve ships defensive
     rotation ANYWAY, so a defensive tilt reaches the book on evidence, not on an LLM's mood.

  2. It is ALSO the rotation FLOOR an armed PM may not undercut: when ``DEF_SLEEVE_MAX > 0`` the
     PM may pick WHICH defensives (its champion pool, from portfolio/defensive_candidates.py) but
     may never push the defensive sleeve BELOW ``floor_legs()``'s weights. B1 imports floor_legs()
     lazily; the contract lives HERE (this module owns it).

THE CONTROL ARM — DEF_SLEEVE_MAX = 0 (default)
----------------------------------------------
``DEF_SLEEVE_MAX`` defaults to 0.0 in config (doctrine.yml ``def_sleeve.max``). At 0 the def budget
is 0 → the sleeve is INERT → ``build_def_sleeve`` returns no legs and the book is BYTE-IDENTICAL to
today. That is the E1 control arm the integrator diff-tests. Arming (setting max>0, capped at 0.35)
is an ops decision at the coordinated prod restart, NOT a code default.

THE SIZING FORMULA (spec 1) — every constant in doctrine.yml (unverified-prior)
-------------------------------------------------------------------------------
    fragility_signal = clamp( w_dwell·dwell_level
                              + w_conf·(1 - confidence)
                              + weakening_bump·[transition ∈ WEAKENING_STATES] ,  0, 1 )
      dwell_level = {risk_on: 0.0, caution: 0.5, risk_off: 1.0}   (unknown → 0.0, invariant-safe)
      confidence  = the regime frame confidence in [0,1] (missing → 1.0 so (1-conf) contributes 0)

    def_budget = DEF_SLEEVE_MAX · fragility_signal

More fragile tape (deeper dwell, lower confidence, WEAKENING) → a LARGER defensive budget. A calm,
confident, stable tape → fragility_signal 0 → def_budget 0 (no defensive rotation when nothing is
breaking). Missing inputs push the signal DOWN (missing confidence → (1-conf)=0; unknown dwell → 0),
never up — the master invariant: missing data shrinks, never inflates.

BUDGET DISCIPLINE — NO DOUBLE-CLAIM (spec 3)
--------------------------------------------
The def sleeve's weight is drawn FROM the same de-gross that already shrank leadership (the W2 budget
flex + W2/W3 caps freed leadership weight to cash). It REDEPLOYS freed cash; it does NOT lever new
gross. The binding invariant (encoded in ``_headroom``):

    total book gross WITH the def sleeve  <=  the UN-FLEXED engine book's gross
                                              (what the engine would have held at the MIDPOINT budget,
                                               before the W2 confidence×transition flex shrank it)
    AND cash stays >= the doctrine cash floor.

So the sleeve can consume UP TO the leadership de-gross (midpoint_lead_budget − current leadership
gross), never one basis point of NEW gross beyond the un-flexed engine ceiling. (Note: the naive
"gross_on <= gross_off + 1e-9" invariant is WRONG — it forbids redeploying freed cash, which is the
sleeve's entire purpose. The correct ceiling is the un-flexed engine gross, encoded below.)

THE BUY LIST (spec 2)
---------------------
The sleeve BUYS from portfolio/defensive_candidates.py ONLY — the ONE canonical generator (no seat
invents its own list). Equal-weight across its frozen weights (defensive_candidates.weights(), frozen
until >=12 resolved theses). Each leg gets ``theme_id='DEFENSIVE_<archetype>'`` so W3's cluster pass
(enforce_book_caps) sees a real, cluster-mapped position — a defensive pick must not accidentally
breach a cluster (e.g. XLV sits in no semis cluster; a bottoming single name that IS a semi would be
caught by the semis cluster cap downstream, which is correct).

THE INVARIANT (governs every path)
----------------------------------
Missing/stale/wrong data COARSENS or SHRINKS — it never un-caps, raises authority, or flips
direction. Empty candidates → empty sleeve (a legal no-op, never raises). No headroom → empty sleeve.
DEF_SLEEVE_MAX=0 → empty sleeve. This module NEVER raises.
"""
from __future__ import annotations

import logging
import math
from typing import Any

log = logging.getLogger(__name__)


def _floor4(x: float) -> float:
    """Truncate toward zero at 4 decimals — a budget ceiling must NEVER be exceeded by a round-to-
    nearest artifact (the def sleeve is subtract-from-freed-cash only; the freed sub-cent stays cash).
    Mirrors sleeves._floor4 / conviction._floor4 so the whole firm rounds budgets DOWN, never up."""
    return math.floor(max(0.0, x) * 10000) / 10000.0

# ── doctrine fallbacks — MUST mirror config/doctrine.yml's def_sleeve: block. Used only when the
#    block is absent/malformed so a missing config never breaks a build (and, critically, DEFAULTS
#    THE SLEEVE OFF: max 0.0 → inert → byte-identical book). ──────────────────────────────────────
_DEF_SLEEVE_FALLBACK: dict[str, Any] = {
    "max": 0.0,                 # DEF_SLEEVE_MAX — 0 = INERT (the control arm). Armed max: 0.35.
    "armed_ceiling": 0.35,      # a hard upper clamp on `max` even if config over-sets it (masterplan)
    "w_dwell": 0.5,             # (unverified-prior) weight on the dwell-state fragility term
    "w_conf": 0.3,              # (unverified-prior) weight on the (1-confidence) term
    "weakening_bump": 0.2,      # (unverified-prior) additive bump when transition ∈ WEAKENING_STATES
    "dwell_levels": {"risk_on": 0.0, "caution": 0.5, "risk_off": 1.0},
    "weakening_states": ["WEAKENING", "ROLLING", "DETERIORATING"],
    "cash_floor": 0.05,         # mirror of doctrine cash.rotation_floor (the sleeve respects it)
}


def _cfg() -> dict[str, Any]:
    """The ``def_sleeve:`` block from doctrine.yml merged over the mirrored fallback (never raises).

    The fallback wins field-by-field on any gap, so a partial/malformed block can never inflate the
    sleeve — in particular a missing ``max`` degrades to 0.0 (the inert control arm). ``max`` is hard-
    clamped to [0, armed_ceiling] so no config edit can push the sleeve past the masterplan's 0.35.
    """
    cfg = dict(_DEF_SLEEVE_FALLBACK)
    try:
        from bot.doctrine_config import load_doctrine
        block = load_doctrine().get("def_sleeve") or {}
    except Exception:  # noqa: BLE001 — a config failure degrades to the (OFF) fallback
        block = {}
    if isinstance(block, dict):
        for k in ("max", "armed_ceiling", "w_dwell", "w_conf", "weakening_bump", "cash_floor"):
            v = block.get(k)
            if isinstance(v, (int, float)):
                cfg[k] = float(v)
        dl = block.get("dwell_levels")
        if isinstance(dl, dict):
            cfg["dwell_levels"] = {str(k).lower(): float(v) for k, v in dl.items()
                                   if isinstance(v, (int, float))}
        ws = block.get("weakening_states")
        if isinstance(ws, (list, tuple)):
            cfg["weakening_states"] = [str(x).upper() for x in ws if x]
    # Hard clamp: `max` can never exceed the armed ceiling (0.35) nor go below 0 — even a bad config
    # edit cannot un-cap the sleeve. This is the invariant in action at the config boundary.
    ceiling = float(cfg.get("armed_ceiling") or _DEF_SLEEVE_FALLBACK["armed_ceiling"])
    cfg["max"] = min(max(float(cfg.get("max") or 0.0), 0.0), ceiling)
    return cfg


def _clamp01(x: float) -> float:
    return min(max(x, 0.0), 1.0)


# ---------------------------------------------------------------------------
# fragility sizing
# ---------------------------------------------------------------------------

def fragility_signal(risk_state: dict | None,
                     budget_inputs: dict | None,
                     cfg: dict | None = None,
                     *,
                     evidence: dict | None = None) -> float:
    """The [0,1] fragility magnitude that sizes the def sleeve (spec 1 + W-I task 6). Pure; never raises.

    fragility_signal = clamp( w_dwell·dwell_level + w_conf·(1-confidence)
                              + weakening_bump·[transition ∈ WEAKENING_STATES]
                              + rotation-evidence LIFT , 0, 1 )

    dwell_level reads ``risk_state['state']`` (risk_on/caution/risk_off) — the W1 dwell machine's
    persistent state. An unknown/missing state → 0.0 (invariant-safe: missing data cannot inflate
    the defensive budget). confidence/transition come from ``budget_inputs`` (the same
    regime_frame.budget() ``inputs`` dict phase2 already threads) — missing confidence → treated as
    1.0 so (1-conf) contributes 0 (again, missing shrinks not inflates).

    THE ROTATION-EVIDENCE LIFT (W-I task 6) — unthrottles the incident's documented '7% when it
    should be 23%' problem: when the disagreement sources agree, the defensive floor should RISE.
    ``evidence`` is the SAME shrink-only agreement count budget()'s damp reads (a
    ``regime_frame.rotation_evidence()`` dict, or ``{"n_agree": int}``); PASSED IN so this stays pure
    and never live-reads the mutating store.  The lift is ``lift_per_source · max(0, n_agree − 1)`` —
    +0.15 per agreeing source BEYOND the first — clamped into [0,1] with the rest of the signal.
    ``None`` (the default, and every pre-W-I caller) → n_agree 0 → lift 0 → BYTE-IDENTICAL.  Because
    the lift is >= 0 it can only ever RAISE the defensive floor (never lower it) — and with
    DEF_SLEEVE_MAX=0 (default) def_budget = 0·anything = 0, so it changes NOTHING live until armed.
    """
    c = cfg or _cfg()
    # ── dwell term ──
    state = None
    if isinstance(risk_state, dict):
        state = risk_state.get("state") or risk_state.get("risk_state")
    dwell_level = 0.0
    if isinstance(state, str):
        dwell_level = float(c["dwell_levels"].get(state.lower().strip(), 0.0))

    # ── confidence term: (1 - confidence). Missing confidence → 1.0 → (1-conf)=0 (no inflation). ──
    conf = None
    if isinstance(budget_inputs, dict):
        conf = budget_inputs.get("confidence")
    try:
        conf_f = float(conf) if conf is not None else 1.0
    except (TypeError, ValueError):
        conf_f = 1.0
    conf_term = 1.0 - _clamp01(conf_f)

    # ── weakening bump: additive when the transition state is a WEAKENING-class read ──
    trans = None
    if isinstance(budget_inputs, dict):
        trans = budget_inputs.get("transition_state")
    is_weakening = isinstance(trans, str) and trans.upper() in set(c["weakening_states"])
    bump = float(c["weakening_bump"]) if is_weakening else 0.0

    # ── rotation-evidence lift: +lift_per_source per agreeing source BEYOND the first (>= 0 only) ──
    lift = _evidence_lift(evidence)

    sig = float(c["w_dwell"]) * dwell_level + float(c["w_conf"]) * conf_term + bump + lift
    return _clamp01(sig)


def _evidence_lift(evidence: dict | None) -> float:
    """``lift_per_source · max(0, n_agree − 1)`` — the DEF_SLEEVE unthrottle (W-I task 6). Never raises.

    Reads the SAME ``rotation_evidence`` config regime_frame.budget()'s damp reads (one definition of
    ``lift_per_source``), so the two levers stay coupled.  A missing/malformed evidence dict → 0.0
    (byte-identical no-op — absent evidence never lifts the floor).  The lift is always >= 0 (it can
    only raise the defensive floor, never lower it — the invariant for a shrink-on-offense signal)."""
    if not isinstance(evidence, dict):
        return 0.0
    try:
        n_agree = int(evidence.get("n_agree") or 0)
    except (TypeError, ValueError):
        return 0.0
    if n_agree <= 1:
        return 0.0
    per = 0.15  # fallback prior; overridden by doctrine's rotation_evidence.lift_per_source below
    try:
        from brain import regime_frame as _rf
        per = float(_rf._rotation_evidence_cfg().get("lift_per_source", per))
    except Exception:  # noqa: BLE001 — degrade to the mirrored prior; never break the sleeve
        pass
    return max(0.0, per) * (n_agree - 1)


def def_budget(risk_state: dict | None,
               budget_inputs: dict | None,
               cfg: dict | None = None,
               *,
               evidence: dict | None = None) -> float:
    """``DEF_SLEEVE_MAX · fragility_signal`` — the target defensive budget BEFORE headroom clamping.

    When DEF_SLEEVE_MAX is 0 (default / control arm) this is always 0.0 → the sleeve is inert.
    ``evidence`` (the shrink-only rotation-evidence dict) unthrottles the fragility signal (W-I t6);
    None → no lift → byte-identical.
    """
    c = cfg or _cfg()
    return float(c["max"]) * fragility_signal(risk_state, budget_inputs, c, evidence=evidence)


# ---------------------------------------------------------------------------
# budget discipline — the anti-double-claim headroom (spec 3)
# ---------------------------------------------------------------------------

def _w(p: Any) -> float:
    """The weight of a position row as a float, or 0.0 for junk (never raises — invariant-safe)."""
    if not isinstance(p, dict):
        return 0.0
    try:
        return float(p.get("weight") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _leadership_gross(book: list[dict]) -> float:
    return sum(_w(p) for p in (book or [])
               if isinstance(p, dict) and p.get("sleeve") == "leadership")


def _book_gross(book: list[dict]) -> float:
    return sum(_w(p) for p in (book or []))


def _headroom(book: list[dict], budget_inputs: dict | None, cfg: dict | None = None) -> float:
    """The freed cash the def sleeve may consume, in NAV fraction (>= 0). Never raises.

    Two binding limits (the sleeve may consume up to the MIN):

      1. LEADERSHIP DE-GROSS (no new gross): the sleeve may consume up to the weight the W2 flex +
         W2/W3 caps freed FROM leadership relative to the UN-FLEXED (midpoint) engine budget:

             lead_degross = max(0, midpoint_lead_budget − current leadership gross)

         Consuming exactly this keeps total book gross <= (conviction gross + midpoint budget) = the
         un-flexed engine ceiling. The sleeve therefore NEVER levers the book above what the engine
         would have held before the flex — it only redeploys freed cash. (``budget_inputs`` carries no
         midpoint; we read it from the budget config so there is one definition of the midpoint.)

      2. CASH FLOOR: cash after the sleeve must stay >= the doctrine cash floor:

             cash_headroom = max(0, 1 − cash_floor − current book gross)

    Degrades to 0.0 (no sleeve) on any bad input — never raises, never itself un-caps.
    """
    c = cfg or _cfg()
    midpoint = _budget_midpoint()
    lead_now = _leadership_gross(book)
    lead_degross = max(0.0, midpoint - lead_now)

    gross_now = _book_gross(book)
    cash_floor = float(c.get("cash_floor") or 0.0)
    cash_headroom = max(0.0, 1.0 - cash_floor - gross_now)

    return min(lead_degross, cash_headroom)


def _budget_midpoint() -> float:
    """The un-flexed leadership budget = the budget equation's midpoint (today's 0.50 behaviour).

    Reads config/doctrine.yml's ``budget.midpoint`` via regime_frame's own config helper so there is
    ONE definition of the midpoint firm-wide (the same value budget() degrades to on a missing frame).
    Falls back to 0.50 on any error.
    """
    try:
        from brain import regime_frame as _rf
        return float(_rf._budget_cfg()["midpoint"])
    except Exception:  # noqa: BLE001
        return 0.50


# ---------------------------------------------------------------------------
# leg construction
# ---------------------------------------------------------------------------

def _make_legs(def_actual: float, cands: list[dict], weights_map: dict[str, float]) -> list[dict]:
    """Turn a def budget + the frozen candidate weights into DEF_SLEEVE book legs.

    Equal-weight across the candidate weights (defensive_candidates.weights() is a frozen 1/N prior):
    each ticker gets ``def_actual · frozen_weight``. theme_id = 'DEFENSIVE_<archetype>' so W3's cluster
    pass sees a properly-tagged, cluster-mappable position. Zero-weight / unpriceable rows are dropped
    by the w>0 guard (mirrors the candidate generator's own drop guard).
    """
    arch_by_ticker = {}
    for r in cands or []:
        if isinstance(r, dict):
            tk = str(r.get("ticker") or "").upper().strip()
            if tk:
                arch_by_ticker[tk] = str(r.get("archetype") or "quality_defensive")

    legs: list[dict] = []
    for tk, fw in (weights_map or {}).items():
        tku = str(tk).upper().strip()
        try:
            # FLOOR (truncate), never round-to-nearest: the summed leg weights must never exceed the
            # clamped def_actual (which is itself bounded by the freed-cash headroom). Rounding UP even
            # a single sub-cent would breach the no-new-gross invariant. The truncated remainder → cash.
            w = _floor4(def_actual * float(fw))
        except (TypeError, ValueError):
            continue
        if w <= 0 or not tku:
            continue
        archetype = arch_by_ticker.get(tku, "quality_defensive")
        legs.append({
            "ticker": tku,
            "theme_id": f"DEFENSIVE_{archetype}",
            "sleeve": "defensive",
            "stage": 2,
            "weight": w,
            "verdict": "add",
            "archetype": archetype,
            "def_sleeve": True,          # provenance marker (audit / floor_legs contract)
        })
    return legs


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def build_def_sleeve(book: list[dict],
                     risk_state: dict | None,
                     budget_inputs: dict | None,
                     *,
                     candidates: list[dict] | None = None,
                     evidence: dict | None = None) -> dict[str, Any]:
    """Build the deterministic DEF_SLEEVE legs (the E1-failure branch + the armed-PM floor).

    Parameters
    ----------
    book : the engine book so far ([{ticker, sleeve, weight, ...}]) — used ONLY to measure the freed
           leadership de-gross and current gross (read-only; not mutated).
    risk_state : brain.macro_risk.risk_state dict (carries the W1 dwell ``state``); passed through to
           the fragility signal AND to the defensive candidate generator (source a).
    budget_inputs : the ``inputs`` dict from regime_frame.budget() (confidence / transition_state) —
           the same dict phase2 already threads; sizes the fragility signal.
    candidates : optional pre-read candidate list (DI for tests). None → defensive_candidates.candidates.
    evidence : the shrink-only rotation-evidence dict (regime_frame.rotation_evidence()); lifts the
           fragility signal (W-I task 6). None → no lift → byte-identical to pre-W-I.

    Returns
    -------
    {
      "legs": [ {ticker, theme_id='DEFENSIVE_<archetype>', sleeve='defensive', weight, ...} ],
      "def_budget": float,     # the target budget (DEF_SLEEVE_MAX · fragility_signal) BEFORE headroom
      "def_actual": float,     # the applied budget after the headroom clamp (== sum of leg weights)
      "headroom": float,       # the freed cash available (leadership de-gross ∩ cash-floor room)
      "fragility_signal": float,
      "reason": str,           # why the sleeve is the size it is (audit / runlog)
    }

    Degrade
    -------
    DEF_SLEEVE_MAX=0 → def_budget 0 → empty legs (the control arm). Empty candidates → empty legs.
    No headroom → empty legs. NEVER raises.
    """
    c = _cfg()
    empty = {"legs": [], "def_budget": 0.0, "def_actual": 0.0, "headroom": 0.0,
             "fragility_signal": 0.0, "reason": "inert"}

    sig = fragility_signal(risk_state, budget_inputs, c, evidence=evidence)
    tgt = float(c["max"]) * sig

    # Control arm: max 0 (or a zero fragility read) → the sleeve is inert and the book is unchanged.
    if tgt <= 0:
        empty["fragility_signal"] = round(sig, 4)
        empty["reason"] = "DEF_SLEEVE_MAX=0 (inert control arm)" if c["max"] <= 0 else "fragility_signal=0"
        return empty

    # Candidates (the ONE canonical generator). A degraded/absent pool → empty sleeve (legal no-op).
    if candidates is None:
        try:
            from portfolio import defensive_candidates as _dc
            candidates = _dc.candidates(risk_state)
        except Exception:  # noqa: BLE001 — a broken generator → no sleeve (never a fabricated basket)
            candidates = []
    if not candidates:
        empty.update({"def_budget": round(tgt, 4), "fragility_signal": round(sig, 4),
                      "reason": "no defensive candidates"})
        return empty

    # Frozen equal-weight prior (defensive_candidates.weights()); DI-safe.
    try:
        from portfolio import defensive_candidates as _dc
        weights_map = (_dc.weights(candidates) or {}).get("weights") or {}
    except Exception:  # noqa: BLE001 — degrade to an equal split we compute here (still frozen 1/N)
        tickers = [str(r.get("ticker") or "").upper().strip() for r in candidates
                   if isinstance(r, dict) and r.get("ticker")]
        tickers = [t for t in tickers if t]
        n = len(set(tickers))
        weights_map = {t: round(1.0 / n, 6) for t in set(tickers)} if n else {}
    if not weights_map:
        empty.update({"def_budget": round(tgt, 4), "fragility_signal": round(sig, 4),
                      "reason": "no weightable candidates"})
        return empty

    # BUDGET DISCIPLINE: clamp the target to the freed headroom (no new gross; cash floor respected).
    room = _headroom(book, budget_inputs, c)
    def_actual = min(tgt, room)
    if def_actual <= 0:
        return {"legs": [], "def_budget": round(tgt, 4), "def_actual": 0.0,
                "headroom": round(room, 4), "fragility_signal": round(sig, 4),
                "reason": "no freed headroom (leadership not de-grossed / cash floor binds)"}

    legs = _make_legs(def_actual, candidates, weights_map)
    applied = round(sum(float(p["weight"]) for p in legs), 4)
    return {
        "legs": legs,
        "def_budget": round(tgt, 4),
        "def_actual": applied,
        "headroom": round(room, 4),
        "fragility_signal": round(sig, 4),
        "reason": (f"DEF_SLEEVE sized {applied:.4f} (target {tgt:.4f}, headroom {room:.4f}, "
                   f"fragility {sig:.4f}) across {len(legs)} defensives"),
    }


def floor_legs(book: list[dict],
               risk_state: dict | None,
               budget_inputs: dict | None,
               *,
               candidates: list[dict] | None = None,
               evidence: dict | None = None) -> list[dict]:
    """The rotation FLOOR the armed PM (task B1) may not undercut when DEF_SLEEVE_MAX > 0.

    THE CONTRACT (this module owns it; B1 imports it lazily):
      * When DEF_SLEEVE_MAX == 0 → returns [] (there is no deterministic floor; the sleeve is off).
      * When DEF_SLEEVE_MAX  > 0 → returns the deterministic ``build_def_sleeve`` legs. The armed PM
        may pick WHICH defensives (its champion pool) and may size the defensive sleeve ABOVE these
        weights, but must not size it BELOW the returned per-leg floor weights (aggregate defensive
        gross >= sum of these leg weights). B1 is responsible for enforcing "not below"; this function
        is the single source of the floor it enforces against — never re-deriving it independently.

    Returns just the legs (the caller reads leg['weight'] as the floor). NEVER raises — a degraded
    build returns [] (no floor to enforce), which is invariant-safe (the PM is simply un-floored,
    exactly as when the sleeve is off).
    """
    try:
        out = build_def_sleeve(book, risk_state, budget_inputs,
                               candidates=candidates, evidence=evidence)
        return out.get("legs") or []
    except Exception:  # noqa: BLE001 — a floor read must never break the judgment layer
        return []
