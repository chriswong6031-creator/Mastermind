"""Three-sleeve assembler + cross-sleeve firebreaks (doctrine §2).

Leadership = engine.narrative_rotation.allocate() AS-IS (equal-weight, mechanical,
monthly cadence). Conviction = top_picks + stock_score half-Kelly (capped, falsifiable).
Cash = sized, never residual. The NEW glue (W3 A2) is the cross-sleeve BOOK cluster cap
(keyed on real correlation structure via fragility_chain.cluster_id — NOT per-instrument
theme_id, which was structurally defeated) + a name cap that binds EVERY sleeve (the
leadership exemption is dead) with a broad-index allowlist carve-out — neither engine
enforces these.
"""
from __future__ import annotations

import math
from collections import defaultdict

from bot.doctrine_config import load_doctrine

# ── W2.1 shared leadership-cap fallbacks — MUST mirror config/doctrine.yml's leadership_caps: block
#    AND config/etf_strategy.yml's guardrails.overextension (the drift-check test enforces parity).
#    Used only when the doctrine block is absent/malformed, so a missing config never breaks a build.
_LEAD_CAP_FALLBACK: dict = {
    "overextension": {"pct_vs_200d_cap": 40.0, "max_weight": 0.08},
    "late_cycle_mult": 0.5,
    "offensive_gross_floor_frac": 0.5,
}


def _floor4(x: float) -> float:
    """Truncate toward zero at 4 decimals — a cap must never be exceeded by a round-to-nearest
    artifact; the freed sub-cent falls to cash (subtract-only, matching conviction._floor4)."""
    return math.floor(max(0.0, x) * 10000) / 10000.0


def leadership_caps_cfg() -> dict:
    """The shared leadership-cap config from doctrine.yml (with the mirrored in-code fallback).

    ONE tuning surface: this reads doctrine.yml's ``leadership_caps:`` block; the ETF book reads the
    SAME thresholds from etf_strategy.yml's ``guardrails.overextension``. The drift-check test asserts
    the two agree so there is never a second live definition of "how extended is too extended".
    A missing/partial block degrades field-by-field to the fallback (never raises)."""
    cfg = {
        "overextension": dict(_LEAD_CAP_FALLBACK["overextension"]),
        "late_cycle_mult": _LEAD_CAP_FALLBACK["late_cycle_mult"],
        "offensive_gross_floor_frac": _LEAD_CAP_FALLBACK["offensive_gross_floor_frac"],
    }
    try:
        block = load_doctrine().get("leadership_caps") or {}
    except Exception:  # noqa: BLE001 — a config failure degrades to the fallback (never breaks a build)
        block = {}
    if isinstance(block, dict):
        ov = block.get("overextension")
        if isinstance(ov, dict):
            cap = ov.get("pct_vs_200d_cap")
            mw = ov.get("max_weight")
            if isinstance(cap, (int, float)):
                cfg["overextension"]["pct_vs_200d_cap"] = float(cap)
            if isinstance(mw, (int, float)):
                cfg["overextension"]["max_weight"] = float(mw)
        lcm = block.get("late_cycle_mult")
        if isinstance(lcm, (int, float)):
            cfg["late_cycle_mult"] = float(lcm)
        floor = block.get("offensive_gross_floor_frac")
        if isinstance(floor, (int, float)):
            cfg["offensive_gross_floor_frac"] = float(floor)
    return cfg


def apply_leadership_caps(legs: list[dict],
                          cycles: dict[str, dict] | None = None,
                          trend_fn=None,
                          held: set | None = None) -> dict:
    """Per-leg brake stack on the LEADERSHIP sleeve (architecture Stage 6.1) — SUBTRACT-ONLY, in place.

    For each leadership leg, scale its weight by ``MIN(extension_clamp, cycle_mult)``:

      * EXTENSION CLAMP (proven ETF-book G4): read ``pct_vs_200d`` from ``trend_fn(ticker)`` (default
        ``brain.etf_board.etf_trend`` — the engine price store, OUTAGE-INDEPENDENT of site/stockdata,
        so the brake still bites during a stockdata publish gap). When a leg is more than
        ``overextension.pct_vs_200d_cap``% above its 200d AND its current weight exceeds
        ``overextension.max_weight``, the leg is CLAMPED to ``max_weight`` (the effective multiplier is
        ``max_weight / weight``). A leg with no trend series (pct_vs_200d None/absent) is left un-clamped
        — missing data degrades to today's weight, never a phantom shrink.

      * CYCLE MULT (late_cycle): if the leg's sector (mapped via ``_leg_sector``) is ``late_cycle`` in
        ``cycles`` (default ``regime_frame.cycles()``), a NEW leg is HALVED by ``late_cycle_mult``. This
        is an ENTRY-size brake on new leadership legs ONLY — the walk-forward REFUTED the cycle veto on
        held/leading sectors (masterplan §0). An UNMAPPED sector, an uncovered ticker, or a STALE/empty
        cycles dict → multiplier 1.0 (un-shrunk): a missing mapping can only ever REMOVE a shrink.

    The two multipliers COMPOSE via MIN (the tighter brake wins — they are not additive; a leg that is
    both over-extended AND late-cycle is clamped by whichever cuts more). The freed weight is dropped to
    CASH — never redistributed to the surviving legs (doctrine A6). Held legs are NOT exempt from the
    EXTENSION clamp (an over-extended ETF is a fragile position however long held — this is a position-
    SIZE cap, not a momentum-exit), but ARE exempt from the CYCLE halving via the ``verdict``/``retained``
    hold marker so the refuted held-sector veto is never reintroduced.

    Parameters
    ----------
    legs : the leadership positions [{ticker, weight, sleeve, verdict, ...}] — mutated in place.
    cycles : optional pre-read cycles() dict (DI for tests); None → regime_frame.cycles().
    trend_fn : optional ticker→trend dict callable (DI for tests); None → etf_board.etf_trend.
    held : optional set of ALREADY-HELD leadership tickers. When provided it is AUTHORITATIVE for the
           cycle-halving exemption (a held leg is never cycle-halved — the refuted held-sector veto).
           When None, the exemption falls back to the leg's ``retained``/``verdict=='hold'`` marker.
           NOTE: phase2 marks every mechanical leadership leg ``verdict='hold'``, so without an explicit
           held set the cycle brake would be inert there — phase2 passes the real held set so the cycle
           halving bites on genuinely NEW leadership additions only.

    Returns
    -------
    {
      "legs": legs,                 # the same list, weights mutated (subtract-only)
      "freed_to_cash": float,       # total weight removed by the brakes (lands in cash)
      "brakes": [ {ticker, from, to, reason, ext_mult, cycle_mult} ],  # per-leg audit for the runlog
    }
    Degrades to a no-op (freed 0, empty brakes) on any missing input — never raises.
    """
    cfg = leadership_caps_cfg()
    ov = cfg["overextension"]
    cap_pct = ov.get("pct_vs_200d_cap")
    max_wt = ov.get("max_weight")
    late_mult = cfg["late_cycle_mult"]

    # cycle read (DI-friendly; a failure or stale file → {} → the cycle brake is a no-op).
    if cycles is None:
        try:
            from brain import regime_frame
            cycles = regime_frame.cycles() or {}
        except Exception:  # noqa: BLE001
            cycles = {}
    cycles = cycles or {}

    if trend_fn is None:
        try:
            from brain import etf_board
            trend_fn = etf_board.etf_trend
        except Exception:  # noqa: BLE001 — no trend source → extension clamp is inert (degrade-safe)
            trend_fn = lambda _t: {}  # noqa: E731

    held_up = {str(h).upper() for h in (held or set())}

    freed = 0.0
    brakes: list[dict] = []
    for p in legs:
        if p.get("sleeve") != "leadership":
            continue
        w0 = float(p.get("weight") or 0.0)
        if w0 <= 0:
            continue
        t = p.get("ticker")

        # ── extension clamp: multiplier that would bring the leg to max_weight if over-extended ──
        ext_mult = 1.0
        if (isinstance(cap_pct, (int, float)) and cap_pct > 0
                and isinstance(max_wt, (int, float)) and max_wt > 0):
            try:
                pct = (trend_fn(t) or {}).get("pct_vs_200d")
            except Exception:  # noqa: BLE001 — a trend-read failure never blocks; leg un-clamped
                pct = None
            if isinstance(pct, (int, float)) and pct > cap_pct and w0 > max_wt:
                ext_mult = max_wt / w0     # scales w0 down to exactly max_wt

        # ── cycle mult: HALVE a NEW late-cycle leg (never a held/retained one — refuted veto) ──
        cycle_mult = 1.0
        # An explicit `held` set (from phase2) is AUTHORITATIVE; else fall back to the row's own
        # retained/verdict marker (DI-friendly for tests that don't pass a held set).
        if held is not None:
            is_held = (str(t).upper() in held_up) or bool(p.get("retained"))
        else:
            is_held = bool(p.get("retained")) or p.get("verdict") == "hold"
        if not is_held and isinstance(late_mult, (int, float)):
            sec = _leg_sector(t)
            row = cycles.get(sec) if sec else None
            if isinstance(row, dict) and row.get("late_cycle") is True:
                cycle_mult = float(late_mult)

        mult = min(ext_mult, cycle_mult)
        if mult >= 1.0:
            continue                        # neither brake bites — leave the leg untouched
        w1 = _floor4(w0 * mult)
        if w1 >= w0:
            continue
        p["weight"] = w1
        freed += (w0 - w1)
        # record WHY (most-binding reason first) for the runlog / audit trail
        reason = ("overextension" if ext_mult <= cycle_mult else "late_cycle")
        p["lead_capped"] = {"reason": reason, "from": round(w0, 4), "to": w1}
        brakes.append({"ticker": t, "from": round(w0, 4), "to": w1,
                       "reason": reason, "ext_mult": round(ext_mult, 4),
                       "cycle_mult": round(cycle_mult, 4)})

    return {"legs": legs, "freed_to_cash": round(freed, 4), "brakes": brakes}


def _leg_sector(ticker: str | None) -> str | None:
    """Best-effort sector-ETF key for a leadership leg, for the late_cycle cycle brake.

    ``regime_frame.cycles()`` is keyed by the 11 GICS sector-ETF tickers (XLK/XLV/…). A leadership leg
    from phase2 IS a sector ETF (its ``ticker`` == the sector ETF), so the identity mapping covers the
    common case; a thematic/basket-block ETF (SMH/QQQ/IGV/…) is folded into its parent GICS sector so
    the sector's cycle phase still applies. Anything not in the map and not a bare sector ticker returns
    the ticker itself — which simply won't be a key in cycles() (→ unmapped → un-shrunk, invariant-safe:
    a missing mapping only ever removes a shrink)."""
    if not ticker or not isinstance(ticker, str):
        return None
    t = ticker.upper()
    # thematic/basket ETFs → parent sector (mirrors conviction._BASKET_ETF_TO_SECTOR intent).
    _THEMATIC_TO_SECTOR = {"SMH": "XLK", "IGV": "XLK", "QQQ": "XLK",
                           "XHB": "XLY", "KRE": "XLF"}
    return _THEMATIC_TO_SECTOR.get(t, t)


def offensive_gross_tripwire(legs: list[dict], lead_budget: float,
                             parabolic_veto_fired: bool = False) -> dict:
    """Guard-rail against the compounding-shrink stack (architecture Stage 6.5): the offensive-gross
    floor. After ALL brakes, offensive (leadership) gross must stay >= floor_frac · lead_budget UNLESS a
    parabolic hard veto fired (a real veto is allowed to drive gross below the floor — that is the one
    sanctioned way past it).

    ENFORCEMENT CHOICE — TRIPWIRE, not proportional scale-back. The architecture offered a choice:
    scale the brakes back proportionally (except extension clamps) to defend the floor, OR emit a loud
    runlog tripwire. We choose the TRIPWIRE because (a) the brakes here are all subtract-only position-
    SIZE caps whose whole point is that an over-extended / late-cycle leg is UNSAFE at size — mechanically
    un-shrinking them to hit a gross floor would re-inflate exactly the risk the brake removed, defeating
    the lever; and (b) a legitimately-braked book SHOULD sometimes sit below the floor (that is the brake
    working), so the floor is a monitoring alarm, not a hard invariant. The tripwire makes an
    over-de-grossed book LOUD and auditable without silently fighting the risk controls.

    Returns {"breached": bool, "offensive_gross": float, "floor": float, "reason": str|None}. Degrades
    to breached=False on any bad input (lead_budget<=0) — never raises, never itself un-caps."""
    cfg = leadership_caps_cfg()
    floor_frac = cfg["offensive_gross_floor_frac"]
    try:
        gross = sum(float(p.get("weight") or 0.0) for p in legs
                    if p.get("sleeve") == "leadership")
        lb = float(lead_budget)
    except (TypeError, ValueError):
        return {"breached": False, "offensive_gross": 0.0, "floor": 0.0, "reason": None}
    if lb <= 0 or not isinstance(floor_frac, (int, float)):
        return {"breached": False, "offensive_gross": round(gross, 4), "floor": 0.0, "reason": None}
    floor = float(floor_frac) * lb
    # A parabolic hard veto is the sanctioned way below the floor — never a breach.
    breached = (gross < floor - 1e-9) and not parabolic_veto_fired
    return {
        "breached": breached,
        "offensive_gross": round(gross, 4),
        "floor": round(floor, 4),
        "reason": (f"over_degross: leadership gross {gross:.4f} < floor {floor:.4f} "
                   f"({floor_frac:.0%} of lead_budget {lb:.4f})") if breached else None,
    }


def _caps_cfg() -> dict:
    """The ``caps:`` block from doctrine.yml with degrade-safe fallbacks (never raises). Holds the
    name cap, the broad-index allowlist + its looser cap, the ballast allowlist + its looser cap,
    and the cluster-cap fallbacks used when config/clusters.yml (A1, authoritative) is absent."""
    out = {
        "name_cap": 0.08,
        "broad_index_allowlist": {"SPY", "QQQ", "VTI", "RSP", "IWM", "DIA"},
        "broad_index_cap": 0.15,
        # W-I Task 5(a): ballast cash-equivalents — cap-exempt up to ballast_cap, excluded from clusters.
        "ballast_allowlist": {"SGOV", "BIL", "SHY", "USFR"},
        "ballast_cap": 0.40,
        "default_cluster_cap": 0.40,
        "cluster_caps": {"semis_ai": 0.35},
    }
    try:
        caps = (load_doctrine().get("caps") or {})
    except Exception:  # noqa: BLE001 — a config failure degrades to the fallback (never breaks a build)
        caps = {}
    if isinstance(caps, dict):
        nc = caps.get("name_cap")
        if isinstance(nc, (int, float)):
            out["name_cap"] = float(nc)
        al = caps.get("broad_index_allowlist")
        if isinstance(al, (list, tuple)):
            out["broad_index_allowlist"] = {str(t).upper().strip() for t in al if str(t).strip()}
        bic = caps.get("broad_index_cap")
        if isinstance(bic, (int, float)):
            out["broad_index_cap"] = float(bic)
        # ballast allowlist + cap (W-I Task 5a)
        bal = caps.get("ballast_allowlist")
        if isinstance(bal, (list, tuple)):
            out["ballast_allowlist"] = {str(t).upper().strip() for t in bal if str(t).strip()}
        bapc = caps.get("ballast_cap")
        if isinstance(bapc, (int, float)):
            out["ballast_cap"] = float(bapc)
        dcc = caps.get("default_cluster_cap")
        if isinstance(dcc, (int, float)):
            out["default_cluster_cap"] = float(dcc)
        cc = caps.get("cluster_caps")
        if isinstance(cc, dict):
            out["cluster_caps"] = {str(k): float(v) for k, v in cc.items()
                                   if isinstance(v, (int, float))}
    return out


def _cluster_cap(cluster_id: str, caps: dict, name_cap: float, cap_fn=None) -> float:
    """Resolve the max-gross-book cap for a cluster (subtract-only floor at name_cap).

    Precedence: an injected ``cap_fn(cluster_id)`` (DI / A1's clusters.yml lookup) → the doctrine
    ``cluster_caps`` per-cluster override → the doctrine ``default_cluster_cap``. The result is
    floored at ``name_cap`` so a SINGLETON cluster (its own ticker, per the invariant for unknown
    names) can NEVER breach a cluster cap it already satisfies via the name pass — its cluster cap is
    at least its name cap. Never raises (a bad cap_fn / value degrades to the doctrine default)."""
    cap = None
    if cap_fn is not None:
        try:
            v = cap_fn(cluster_id)
            if isinstance(v, (int, float)) and v > 0:
                cap = float(v)
        except Exception:  # noqa: BLE001 — a lookup failure degrades to the doctrine default
            cap = None
    if cap is None:
        cc = caps.get("cluster_caps") or {}
        v = cc.get(cluster_id)
        cap = float(v) if isinstance(v, (int, float)) and v > 0 else float(caps["default_cluster_cap"])
    return max(cap, float(name_cap))


def _log_guardrail_freeze(guard: str, detail: str, job: str = "", book: str = "") -> None:
    """Emit a FREEZE GuardrailResult to run_events.  Never raises."""
    try:
        from control_plane.guardrail import GuardrailResult, Severity
        GuardrailResult.failed(
            guard,
            Severity.FREEZE,
            detail=detail,
            action_taken="positions unchanged (firebreak exception; no new risk added)",
        ).log(job=job, book=book)
    except Exception:  # noqa: BLE001
        pass


def enforce_book_caps(positions: list[dict], *, cluster_fn=None, cluster_cap_fn=None) -> dict:
    """Apply the firebreaks across BOTH sleeves (subtract-only). W3 A2 rebuild.

    positions: [{ticker, theme_id, sleeve, weight}]  (weights = fraction of book)
    Returns the capped book + any breaches (which feed detector D6).

    THREE binding layers, in order (each subtract-only; freed weight ALWAYS falls to CASH, never
    redistributed — doctrine A6):

      1. NAME cap — EVERY position, EVERY sleeve. The old leadership exemption
         ('if sleeve==leadership: continue') DIES: it let SMH/XLK/MTUM sit uncapped (SMH at 12.5% was
         the single largest risk line). Now two carve-outs:
           * BROAD-INDEX ALLOWLIST (SPY/QQQ/VTI/RSP/IWM/DIA) — internally-diversified market indices
             capped at the looser ``broad_index_cap`` (0.15). SMH is NOT on this list.
           * BALLAST ALLOWLIST (SGOV/BIL/SHY/USFR) — T-bill/cash-equivalent ETFs capped at the
             much looser ``ballast_cap`` (0.40). These are NOT concentrated bets; they are dry powder
             with a coupon. Trimming $39.2k of SGOV ballast to enforce the name cap is a false positive
             that cuts defence while the engine thinks it is de-risking (W-I Task 5a). Ballast still
             counts in GROSS (it is not leverage-neutral). breach kind:'name'.

      2. CLUSTER cap (the fix for theme-cap-defeated-by-per-name-theme-id) — aggregate weight by
         ``cluster_fn(ticker)`` ACROSS BOTH SLEEVES and scale any over-cap cluster DOWN pro-rata across
         its members. BALLAST NAMES ARE EXCLUDED FROM CLUSTER AGGREGATION — a T-bill is a cash-like
         instrument, not a correlated equity factor; grouping it into any cluster would be wrong. The
         0.25 theme_id cap was structurally defeated: leadership legs set theme_id=ticker, so the
         correlated tech cohort (SMH/XLK/MTUM + conviction tech) NEVER SUMMED — the live book carried
         IT = 42.6% with breaches:[]. Per-cluster caps come from clusters.yml (A1) when present, else
         the doctrine fallbacks (default 0.40, semis_ai 0.35). breach kind:'cluster'. An UNKNOWN name
         degrades to a SINGLETON cluster (its own id) whose cap ≥ name_cap → it can never spuriously
         group with peers and never falsely breach.

      3. theme_id is now DISPLAY-ONLY. WHY the old per-theme_id cap loop was DELETED: it keyed on the
         per-instrument theme_id, and leadership legs set theme_id=ticker, so three 0.79-0.94-correlated
         ETFs (SMH/XLK/MTUM) registered as three separate 'themes' that never summed — the cap could
         not see a correlated cohort and so never fired. Correlation structure lives in the CLUSTER
         layer now; theme_id is kept (computed/emitted below) only for artifact/UI compatibility.

    cluster_fn : optional ticker->cluster_id callable (DI for tests; also lets this suite pass before
                 A1's fragility_chain.cluster_id lands). None → lazy fragility_chain.cluster_id, and if
                 that is absent the name IS ITS OWN cluster (singleton) — degrade-safe, never groups.
    cluster_cap_fn : optional cluster_id->cap callable (DI / A1's clusters.yml cap lookup). None → the
                 doctrine cluster_caps override then default_cluster_cap.
    """
    try:
        return _enforce_book_caps_inner(positions, cluster_fn=cluster_fn, cluster_cap_fn=cluster_cap_fn)
    except Exception as _exc:  # noqa: BLE001 — a firebreak exception must never propagate; log + FREEZE
        _log_guardrail_freeze(
            "enforce_book_caps",
            detail=f"enforce_book_caps raised: {_exc!r}"[:200],
            job="enforce_book_caps",
            book="",
        )
        # Conservative action taken BEFORE constructing the result: return positions unchanged
        # so no new allocation is applied (freeze = keep whatever the caller had before).
        return {"positions": positions, "breaches": [], "_guardrail_freeze": True}


def _enforce_book_caps_inner(positions: list[dict], *, cluster_fn=None, cluster_cap_fn=None) -> dict:
    """Inner implementation of enforce_book_caps — separated so the outer function can catch and log."""
    caps = _caps_cfg()
    name_cap = caps["name_cap"]
    allowlist = caps["broad_index_allowlist"]
    broad_cap = caps["broad_index_cap"]
    ballast_set = caps["ballast_allowlist"]
    ballast_cap = caps["ballast_cap"]

    breaches = []

    # ── LAYER 1: name cap — EVERY sleeve; carve-outs for broad-index + ballast ─────────────────
    # Precedence: ballast_allowlist → ballast_cap | broad_index_allowlist → broad_cap | name_cap.
    # A ticker on BOTH lists (impossible by design, but guard it) takes the HIGHER cap — degrade-safe.
    for p in positions:
        tkr = str(p.get("ticker") or "").upper().strip()
        if tkr in ballast_set:
            cap = ballast_cap           # cash-equivalent: ballast_cap (0.40), not name_cap
        elif tkr in allowlist:
            cap = broad_cap             # broad market index: 0.15
        else:
            cap = name_cap              # everything else: 0.08
        if p["weight"] > cap:
            breaches.append({"code": "D6", "kind": "name", "subject": p["ticker"],
                             "weight": round(p["weight"], 4), "cap": cap})
            p["weight"] = cap

    # ── LAYER 2: cluster cap — aggregate by cluster_fn ACROSS BOTH SLEEVES, pro-rata scale-down ──
    # BALLAST NAMES EXCLUDED FROM CLUSTER AGGREGATION — cash-equivalents are not correlated equity
    # factor clusters; including them would cause spurious cluster-cap breaches.
    # Lazy defaults: fragility_chain.cluster_id / .cluster_cap (task A1 — the authoritative clusters.yml
    # readers). Import inside the function (house style) so this module never hard-depends on them; if
    # A1 is absent, a name is its OWN cluster and caps fall back to doctrine.yml. cluster_cap is designed
    # (by A1) to be passed here as cluster_cap_fn so the caps live in ONE file — clusters.yml wins, and
    # our doctrine default is only the degrade-safe fallback that A1 returns when a cluster is unmapped.
    if cluster_fn is None:
        try:
            from portfolio import fragility_chain
            cluster_fn = fragility_chain.cluster_id
            if cluster_cap_fn is None:
                cluster_cap_fn = fragility_chain.cluster_cap  # (cluster_id) -> cap | None
        except Exception:  # noqa: BLE001 — no cluster map yet → singleton clusters (degrade-safe)
            cluster_fn = None

    def _cid(ticker) -> str:
        """The cluster id for a ticker; an UNKNOWN name degrades to a SINGLETON (its own id) so caps
        can never spuriously group it (invariant: missing data coarsens identity, never un-caps)."""
        t = str(ticker or "").upper().strip()
        if cluster_fn is None:
            return t or "?"
        try:
            c = cluster_fn(ticker)
        except Exception:  # noqa: BLE001 — a lookup failure → singleton
            c = None
        c = str(c).strip() if c else ""
        return c or (t or "?")

    by_cluster: dict[str, float] = defaultdict(float)
    for p in positions:
        tkr = str(p.get("ticker") or "").upper().strip()
        if tkr in ballast_set:
            continue           # ballast is EXCLUDED from cluster aggregation — cash-like, not equity factor
        by_cluster[_cid(p.get("ticker"))] += p["weight"]
    for cid, w in by_cluster.items():
        cap = _cluster_cap(cid, caps, name_cap, cluster_cap_fn)
        if w > cap + 1e-12:
            breaches.append({"code": "D6", "kind": "cluster", "subject": cid,
                             "weight": round(w, 4), "cap": round(cap, 4)})
            scale = cap / w
            for p in positions:
                tkr = str(p.get("ticker") or "").upper().strip()
                if tkr not in ballast_set and _cid(p.get("ticker")) == cid:
                    p["weight"] *= scale

    return {"positions": positions, "breaches": breaches}


def no_rotation_capacity(cash: float, top_theme_concentration: float) -> bool:
    """Detector D3 — the operator's structural pre-condition for the failure."""
    nrc = load_doctrine()["cash"]["no_rotation_capacity"]
    return cash <= nrc["cash_max"] and top_theme_concentration >= nrc["top_theme_concentration_min"]


def binding_cash(macro_implied: float) -> float:
    """Cash is sized, orthogonal to market risk: max(macro-implied, rotation floor)."""
    floor = load_doctrine()["cash"]["rotation_floor"]
    return max(macro_implied, floor)


def assemble(leadership: dict, conviction: dict, macro_implied_cash: float) -> dict:
    """Combine the sleeves into one book under the budgets + firebreaks. TODO(engine wiring)."""
    raise NotImplementedError(
        "wire leadership=narrative_rotation.allocate(); conviction=top_picks/stock_score; "
        "then enforce_book_caps + binding_cash"
    )
