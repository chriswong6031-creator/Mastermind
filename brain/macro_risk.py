"""MACRO RISK OFFICER — the top-down DEFENSE seat the desk was missing.

``brain/strategist.py`` reads the in-house macro dashboard for OFFENSE: which themes are in
confirmed leadership, where to lean in. This seat is its mirror image: it reads the SAME dashboard
for DEFENSE — is the market about to roll over, and is our book a leveraged bet on the thing
cracking? On 2026-06-23 the desk's own Strategist printed "memory late-stage blow-off, contracting
liquidity breaks the most crowded momentum first" — but it was narrative only and nothing acted on
it. This seat exists so that warning BINDS.

DESIGN — deterministic spine, optional LLM narrative. The other desk seats are LLM-only and return
``None`` on failure; fine for advice, fatal for teeth (the whole 06-23 failure was a narrative warning
that bound nothing). So the RISK STATE here is a PURE deterministic function (like
``data_layer.overnight.risk_read`` / ``portfolio.firm_exposure.summary``) that fuses the warning
signals the dashboard already exposes into a falsifiable ``risk_on | caution | risk_off`` and the
teeth bind to it regardless of whether an LLM is up. An optional Opus narrative pass
(``macro_risk_assess``, flag ``MASTERMIND_MACRO_RISK``) adds the prose synthesis on top.

Signals fused (all read defensively → degrade to None, so the seat scores offline):
  * VOLATILITY    — ``vol_sentiment.json``: VIX level/percentile, term contango→backwardation,
                    put/call complacency.
  * CREDIT / USD  — ``etf_pulse.json`` risk block: HYG/TLT credit, DX-Y USD, Gold/SPY, VIX; tilt+label.
  * LIQUIDITY     — regime ``liquidity_overlay`` (contracting = the 06-23 driver), cycle_tag,
                    transition_flags, complacency/capitulation conditions.
  * CROWDING      — regime ``sector_rs`` blow-off extremes + basket flow + the breadth artifact.
  * DEALER GAMMA  — ``gex/SPY.json``: short-gamma regime, distance to the gamma flip, vol-hole state.

The TEETH (``gross_cap`` / ``allow_adds`` / ``apply_risk_state``) are PURE subtract-only helpers
mirroring ``risk_officer.apply_exits`` — exhaustively unit-testable with no LLM. Additive + reversible;
never raises.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

from brain import client

_ARTIFACTS = Path(__file__).resolve().parent.parent / "data" / "macro_risk"
_V = Path(__file__).resolve().parent.parent / "vendor" / "macro"

_STATES = ("risk_on", "caution", "risk_off")

# ── axis weights (sum to 1.0) + state thresholds (env-tunable) ──────────────────────────────────
_AXIS_WEIGHTS = {"liquidity": 0.25, "crowding": 0.20, "volatility": 0.20,
                 "credit_usd": 0.20, "dealer_gamma": 0.15}


def _envf(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, default))
        return v
    except (TypeError, ValueError):
        return default


def _risk_off_score() -> float:
    return _envf("MASTERMIND_RISKOFF_SCORE", 0.60)


def _caution_score() -> float:
    return _envf("MASTERMIND_CAUTION_SCORE", 0.35)


def _risk_off_gross() -> float:
    return _envf("MASTERMIND_RISKOFF_GROSS", 0.55)


def _caution_gross() -> float:
    return _envf("MASTERMIND_CAUTION_GROSS", 0.85)


def enabled() -> bool:
    """The LLM narrative pass runs only when explicitly armed AND an LLM is reachable. Default OFF.
    NOTE: the deterministic ``risk_state`` / teeth do NOT depend on this — they bind regardless."""
    flag = os.environ.get("MASTERMIND_MACRO_RISK", "0").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    try:
        return client.available()
    except Exception:  # noqa: BLE001
        return False


def _load(rel: str):
    """Read a published dashboard JSON, degrading to None (same idiom as ``strategist._load``)."""
    p = _V / rel
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _g(d, path: str):
    """Nested get by dotted path; None on any miss. Never raises."""
    cur = d
    for k in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _f(x):
    """Coerce to float or None."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# ─────────────────────────────────────────────────────────────────────────────
# raw signal collection — the same fused read the scorer AND the LLM prompt use.
# ─────────────────────────────────────────────────────────────────────────────
def _collect(regime: dict | None) -> dict:
    regime = regime or {}
    vs = _load("site/basketdata/vol_sentiment.json") or {}
    pulse = _load("site/basketdata/etf_pulse.json") or {}
    flow = _load("site/basketdata/flow.json") or {}
    gex = _load("site/gex/SPY.json") or {}

    risk = pulse.get("risk") or {}
    legs = {}
    for leg in (risk.get("legs") or []):
        if isinstance(leg, dict) and leg.get("pair"):
            legs[str(leg["pair"])] = {k: leg.get(k) for k in ("chg_1d", "chg_5d", "chg_20d", "level",
                                                               "direction", "contrib")}

    # crowded/late baskets (stage confirmed/late with high momentum) — light read
    flow_rows = flow.get("baskets") or flow.get("flow") or flow.get("rows") or []
    crowded_baskets = []
    for b in flow_rows:
        if not isinstance(b, dict):
            continue
        stage = str(b.get("stage") or "").lower()
        if stage in ("confirmed", "late", "distribution", "crowded"):
            crowded_baskets.append({"name": str(b.get("name") or b.get("id") or "")[:50],
                                    "stage": stage, "breadth": b.get("breadth"),
                                    "perf_20d_rel": b.get("perf_20d_rel") or b.get("ret_20d_rel")})

    return {
        "vol_regime": {k: _g(vs, f"vol_regime.{k}") for k in
                       ("vix", "vix_pctile", "term_state", "term_ratio", "realized_vol")},
        "put_call": {k: _g(vs, f"put_call.{k}") for k in ("equity_pc", "sentiment_en")},
        "etf_risk": {"tilt": risk.get("tilt"), "label_en": risk.get("label_en"), "legs": legs},
        "regime": {k: regime.get(k) for k in
                   ("quad", "quad_name", "liquidity_overlay", "cycle_tag", "growth_score",
                    "inflation_score")},
        "transition_flags": regime.get("transition_flags") or {},
        "conditions": {"complacency": (regime.get("conditions") or {}).get("complacency") or {},
                       "capitulation": (regime.get("conditions") or {}).get("capitulation") or {}},
        "sector_rs": [{"ticker": r.get("ticker"), "pctile_252d": r.get("pctile_252d"),
                       "mom_60d_pct": r.get("mom_60d_pct"), "mom_20d_pct": r.get("mom_20d_pct"),
                       "above_200d_trend": r.get("above_200d_trend")}
                      for r in (regime.get("sector_rs") or [])[:12] if isinstance(r, dict)],
        "crowded_baskets": crowded_baskets[:12],
        "gex_spy": {**{k: _g(gex, f"summary.{k}") for k in
                       ("spot", "regime", "net_gex_bn", "gamma_flip", "dist_to_flip_pct")},
                    "vol_hole_state": _g(gex, "vol_hole.state")},
    }


# ─────────────────────────────────────────────────────────────────────────────
# per-axis fragility scorers — each returns (fragility 0..1, reason str). PURE.
# ─────────────────────────────────────────────────────────────────────────────
def _axis_volatility(s: dict) -> tuple[float, str]:
    vr = s.get("vol_regime") or {}
    pc = s.get("put_call") or {}
    frag, bits = 0.0, []
    term = str(vr.get("term_state") or "").lower()
    if term == "backwardation":
        frag += 0.55
        bits.append("VIX term BACKWARDATION (stress)")
    elif term and term != "contango":
        frag += 0.2
        bits.append(f"VIX term {term}")
    vp = _f(vr.get("vix_pctile"))
    if vp is not None:
        if vp >= 0.85:
            frag += 0.3
            bits.append(f"VIX {vp * 100:.0f}th pctile")
        elif vp >= 0.6:
            frag += 0.15
            bits.append(f"VIX {vp * 100:.0f}th pctile")
    vix = _f(vr.get("vix"))
    if vix is not None and vix >= 25:
        frag += 0.2
        bits.append(f"VIX {vix:.0f}")
    sent = str(pc.get("sentiment_en") or "").lower()
    if sent in ("complacent", "greedy", "complacency"):
        frag += 0.2
        bits.append(f"put/call {sent} (latent fragility)")
    return _clamp01(frag), "; ".join(bits) or "vol benign"


def _axis_credit_usd(s: dict) -> tuple[float, str]:
    r = s.get("etf_risk") or {}
    legs = r.get("legs") or {}
    frag, bits = 0.0, []
    label = str(r.get("label_en") or "").upper()
    if "RISK-OFF" in label or "RISK OFF" in label:
        frag += 0.4
        bits.append("ETF risk block RISK-OFF")
    tilt = _f(r.get("tilt"))
    if tilt is not None:
        if tilt <= -0.5:
            frag += 0.4
            bits.append(f"risk tilt {tilt:+.2f}")
        elif tilt <= -0.2:
            frag += 0.2
            bits.append(f"risk tilt {tilt:+.2f}")
    # HYG/TLT credit weakening (credit underperforming duration over 5d)
    hyg = legs.get("HYG/TLT") or {}
    c5 = _f(hyg.get("chg_5d"))
    if c5 is not None and c5 <= -1.0:
        frag += 0.15
        bits.append(f"HYG/TLT credit {c5:+.1f}% (5d)")
    # DX-Y USD firming = tightening
    for pair, v in legs.items():
        if pair.startswith("DX-Y") or pair == "DXY":
            d5 = _f((v or {}).get("chg_5d"))
            if d5 is not None and d5 >= 1.0:
                frag += 0.1
                bits.append(f"USD {d5:+.1f}% (5d)")
    return _clamp01(frag), "; ".join(bits) or "credit/USD calm"


def _axis_liquidity(s: dict) -> tuple[float, str]:
    reg = s.get("regime") or {}
    tf = s.get("transition_flags") or {}
    comp = (s.get("conditions") or {}).get("complacency") or {}
    frag, bits = 0.0, []
    liq = str(reg.get("liquidity_overlay") or "").lower()
    if liq in ("contracting", "tight", "tightening", "draining"):
        frag += 0.5
        bits.append(f"liquidity {liq}")
    cyc = str(reg.get("cycle_tag") or "").lower()
    if cyc in ("late", "peak"):
        frag += 0.2
        bits.append(f"cycle {cyc}")
    for flag in ("flag_credit_equity", "flag_breadth_price", "flag_gex", "flag_ratio_inflection"):
        if tf.get(flag):
            frag += 0.1
            bits.append(flag.replace("flag_", ""))
    if comp.get("warning") or comp.get("fragility"):
        frag += 0.15
        bits.append("complacency warning")
    return _clamp01(frag), "; ".join(bits) or "liquidity supportive"


def _axis_crowding(s: dict) -> tuple[float, list[str], str]:
    """Returns (fragility, hot_tickers, reason). The hot tickers feed the fragility-chain driver map."""
    frag, bits = 0.0, []
    hot: list[str] = []
    for r in (s.get("sector_rs") or []):
        pct = _f(r.get("pctile_252d"))
        mom = _f(r.get("mom_60d_pct"))
        tk = str(r.get("ticker") or "").upper().strip()
        if pct is not None and pct >= 95 and (mom is None or mom >= 25):
            frag += 0.18
            if tk:
                hot.append(tk)
            bits.append(f"{tk} {pct:.0f}th pctile"
                        + (f"/+{mom:.0f}% 60d" if mom is not None else ""))
    # blow-off baskets (confirmed/late stage with strong rel perf but a breadth tell)
    for b in (s.get("crowded_baskets") or []):
        perf = _f(b.get("perf_20d_rel"))
        if b.get("stage") in ("distribution", "crowded") or (perf is not None and perf >= 5):
            frag += 0.1
            bits.append(f"{b.get('name')} {b.get('stage')}")
    tf = s.get("transition_flags") or {}
    if tf.get("flag_breadth_price"):
        frag += 0.15
        bits.append("breadth/price divergence (narrowing leadership)")
    # de-dup hot tickers, cap
    seen, hot_u = set(), []
    for t in hot:
        if t not in seen:
            seen.add(t)
            hot_u.append(t)
    return _clamp01(frag), hot_u[:12], "; ".join(bits) or "breadth healthy"


def _axis_dealer_gamma(s: dict) -> tuple[float, str]:
    g = s.get("gex_spy") or {}
    tf = s.get("transition_flags") or {}
    frag, bits = 0.0, []
    if str(g.get("regime") or "").lower() == "short":
        frag += 0.45
        bits.append("SPY dealers SHORT gamma (amplifies moves)")
    d2f = _f(g.get("dist_to_flip_pct"))
    if d2f is not None and d2f < 0:
        frag += 0.2
        bits.append(f"SPY {d2f:+.1f}% below gamma-flip")
    vh = str(g.get("vol_hole_state") or "").upper()
    if vh == "EXPANSION":
        frag += 0.15
        bits.append("vol-hole EXPANSION")
    elif vh == "COILED":
        frag += 0.1
        bits.append("vol-hole COILED (compression → vacuum)")
    if tf.get("flag_gex"):
        frag += 0.15
        bits.append("regime GEX flag")
    return _clamp01(frag), "; ".join(bits) or "dealer gamma stabilising"


# ─────────────────────────────────────────────────────────────────────────────
# the teeth — PURE subtract-only. gross_cap / allow_adds / apply_risk_state.
# ─────────────────────────────────────────────────────────────────────────────
def gross_cap(state: str) -> float:
    """The hard gross cap (= a cash floor of 1 - cap) for a risk state. PURE; env-tunable."""
    if state == "risk_off":
        return _risk_off_gross()
    if state == "caution":
        return _caution_gross()
    return 1.0


def allow_adds(state: str) -> bool:
    """Whether NET-NEW adds are permitted. Hard-stopped in risk_off (regardless of conviction)."""
    return state != "risk_off"


def apply_risk_state(book: list[dict], risk_state: dict | None, *,
                     fragility: dict | None = None, held: set | None = None) -> list[dict]:
    """SUBTRACT-ONLY de-risking of ``book`` per the macro state. Returns a NEW list; never adds, never
    raises. Mirrors ``gate_officer.apply_gate`` / ``risk_officer.apply_exits``.

    Three subtract-only operations, in order:
      1. ADD-BLOCK   — when adds are off (risk_off) and ``held`` is supplied, a NET-NEW name (not in
                       ``held``) that sits in a blocked fragile chain is DROPPED (no adding into the crack).
      2. CHAIN TRIM  — a name in an over-concentrated cracking chain is scaled by the fragility-gate trim.
      3. GROSS CAP   — if the surviving gross still exceeds the cap, the whole book is scaled down to it
                       (the hard cash floor) — proportional, conviction-blind.

    ``risk_on`` is a pure no-op (returns the input unchanged → byte-identical when the market is calm)."""
    rs = risk_state or {}
    state = str(rs.get("state") or "risk_on")
    if state == "risk_on":
        return book
    gcap = _f(rs.get("gross_cap"))
    if gcap is None:
        gcap = gross_cap(state)

    if fragility is None:
        try:
            from portfolio import fragility_chain
            fragility = fragility_chain.assess_book(book, rs)
        except Exception:  # noqa: BLE001
            fragility = {"blocked_chains": [], "trims": []}
    trims = {str(t.get("ticker")).upper(): t for t in (fragility or {}).get("trims") or []
             if isinstance(t, dict) and t.get("ticker")}
    # the cracking DRIVER chains — a NET-NEW add into ANY of them is hard-stopped when adds are off,
    # even before the chain is over-concentrated (don't add into the crack at all). The over-cap TRIM
    # (above) is separate: it only fires once a chain breaches the concentration cap.
    driver_chains: set[str] = set()
    for d in (rs.get("drivers") or []):
        if isinstance(d, dict) and d.get("id"):
            driver_chains.add(str(d["id"]))
        elif isinstance(d, str):
            driver_chains.add(d)

    try:
        from portfolio import fragility_chain as _fc
    except Exception:  # noqa: BLE001
        _fc = None

    kept: list[dict] = []
    for r in (book or []):
        t = str(r.get("ticker") or "").upper().strip()
        if not t:
            continue
        try:
            w = float(r.get("weight") or 0.0)
        except (TypeError, ValueError):
            w = 0.0
        tag = {"state": state}
        # (1) add-block on net-new names into a cracking DRIVER chain
        if not allow_adds(state) and held is not None and t not in held and driver_chains and _fc is not None:
            if _fc.chains_of(t) & driver_chains:
                continue  # dropped — never add into the crack
        # (2) chain-concentration trim
        if t in trims:
            try:
                sc = max(0.0, min(1.0, float(trims[t].get("scale", 1.0))))
            except (TypeError, ValueError):
                sc = 1.0
            w = round(w * sc, 4)
            tag["chain_trim"] = {"scale": sc, "chain": trims[t].get("chain")}
        if w <= 0:
            continue
        nr = dict(r)
        nr["weight"] = w
        nr["risk_state"] = tag
        kept.append(nr)

    # (3) gross cap (the cash floor) — proportional scale-down if still over the cap
    gross = round(sum(float(x.get("weight") or 0.0) for x in kept), 6)
    if gcap is not None and gross > gcap > 0:
        scale = round(gcap / gross, 6)
        for x in kept:
            x["weight"] = round(float(x.get("weight") or 0.0) * scale, 4)
            x["risk_state"]["gross_scale"] = scale
        kept = [x for x in kept if (x.get("weight") or 0.0) > 0]
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# risk_state — the PURE deterministic core. Never raises.
# ─────────────────────────────────────────────────────────────────────────────
def _asof_d(asof) -> date | None:
    if isinstance(asof, date):
        return asof
    try:
        return date.fromisoformat(str(asof)[:10])
    except Exception:  # noqa: BLE001
        return None


def risk_state(asof: str, regime: dict | None) -> dict:
    """Fuse the dashboard warning signals into a falsifiable RISK STATE. PURE; NEVER raises.

    Returns a self-describing, gradable dict::

        {agent:"macro_risk", asof, state, fragility, supportive,
         axes:{volatility|credit_usd|liquidity|crowding|dealer_gamma:{fragility, reason}},
         signals:[str], drivers:[{id,name,driver,...}], hot_tickers:[str],
         falsifier:str, check_by:str, regime_growth, regime_inflation,
         gross_cap, allow_adds, defensive_tilt:{...}}

    Every firing axis is named with its level; the falsifier states the reversal that de-escalates the
    state; check_by bounds it in time — falsifiable + probabilistic per the house rules."""
    try:
        s = _collect(regime)
    except Exception:  # noqa: BLE001
        s = {}

    fv, rv = _axis_volatility(s)
    fc, rc = _axis_credit_usd(s)
    fl, rl = _axis_liquidity(s)
    fcr, hot, rcr = _axis_crowding(s)
    fg, rg = _axis_dealer_gamma(s)

    axes = {
        "volatility": {"fragility": round(fv, 3), "reason": rv},
        "credit_usd": {"fragility": round(fc, 3), "reason": rc},
        "liquidity": {"fragility": round(fl, 3), "reason": rl},
        "crowding": {"fragility": round(fcr, 3), "reason": rcr},
        "dealer_gamma": {"fragility": round(fg, 3), "reason": rg},
    }
    fragility = round(sum(_AXIS_WEIGHTS[k] * axes[k]["fragility"] for k in _AXIS_WEIGHTS), 4)

    if fragility >= _risk_off_score():
        state = "risk_off"
    elif fragility >= _caution_score():
        state = "caution"
    else:
        state = "risk_on"

    # the leading-edge fragile chains the crowding axis surfaced (drives the chain gate + playbook)
    drivers = []
    try:
        from portfolio import fragility_chain
        drivers = fragility_chain.fragile_chains(hot, regime)
    except Exception:  # noqa: BLE001
        drivers = []

    reg = s.get("regime") or {}
    growth = _f(reg.get("growth_score")) or 0.0
    infl = _f(reg.get("inflation_score")) or 0.0

    out = {
        "agent": "macro_risk",
        "asof": str(asof)[:10],
        "state": state,
        "fragility": fragility,
        "supportive": state == "risk_on",
        "axes": axes,
        "signals": [axes[k]["reason"] for k in
                    sorted(axes, key=lambda k: axes[k]["fragility"], reverse=True)
                    if axes[k]["fragility"] > 0.0],
        "drivers": drivers,
        "hot_tickers": hot,
        "regime_growth": round(growth, 3),
        "regime_inflation": round(infl, 3),
    }

    # the defensive tilt (advisory favor + enforceable avoid/cash_floor) for this driver
    tilt = {}
    try:
        from portfolio import defensive_playbook
        tilt = defensive_playbook.defensive_tilt(out)
    except Exception:  # noqa: BLE001
        tilt = {}
    out["defensive_tilt"] = tilt

    # the teeth — the effective gross cap is the MORE conservative of the state cap and the driver's
    # cash floor (a credit-event cash floor of 0.35 → 0.65 gross can tighten a caution state's 0.85).
    base_cap = gross_cap(state)
    cash_floor = _f((tilt or {}).get("cash_floor"))
    eff_cap = base_cap
    if state != "risk_on" and cash_floor is not None:
        eff_cap = round(min(base_cap, 1.0 - cash_floor), 4)
    out["gross_cap"] = eff_cap
    out["allow_adds"] = allow_adds(state)

    # falsifier — the concrete reversal that de-escalates the state, built from the firing axes.
    firing = [k for k in axes if axes[k]["fragility"] >= 0.4]
    flips = {
        "volatility": "VIX term re-steepens to contango and the percentile drops",
        "credit_usd": "the ETF risk block turns RISK-ON (HYG/TLT credit stabilises, USD eases)",
        "liquidity": "the liquidity overlay turns from contracting to neutral/expanding",
        "crowding": "leadership broadens (the blow-off names mean-revert off the 95th+ percentile)",
        "dealer_gamma": "SPY reclaims the gamma-flip and dealers return to long gamma",
    }
    if state == "risk_on":
        falsifier = ("escalates to caution if any two of {liquidity contracts, VIX backwardates, "
                     "credit widens, dealers go short-gamma, leadership narrows} confirm")
    else:
        conds = [flips[k] for k in firing] or [flips["liquidity"]]
        falsifier = f"de-escalates to {'caution' if state == 'risk_off' else 'risk_on'} when " \
                    + " AND ".join(conds)
    out["falsifier"] = falsifier
    d = _asof_d(asof)
    out["check_by"] = (d + timedelta(days=3)).isoformat() if d else None
    return out


# ─────────────────────────────────────────────────────────────────────────────
# macro_risk_assess — optional Opus narrative ON TOP of the deterministic state.
# ─────────────────────────────────────────────────────────────────────────────
def _macro_risk_input(regime: dict | None, asof: str, state: dict) -> dict:
    return {
        "asof": str(asof)[:10],
        "deterministic_state": {k: state.get(k) for k in
                                ("state", "fragility", "axes", "drivers", "hot_tickers",
                                 "regime_growth", "regime_inflation")},
        "raw_signals": _collect(regime),
    }


_MACRO_RISK_SYS = (
    "You are the MACRO RISK OFFICER — the top-down DEFENSE seat on an equity investment committee, "
    "the mirror image of the Macro Strategist (who finds leadership). A DETERMINISTIC engine has "
    "already fused the dashboard warning signals (vol/term-structure, credit/USD, liquidity overlay, "
    "crowding/breadth, dealer gamma) into a falsifiable RISK STATE (risk_on/caution/risk_off) and the "
    "leading-edge fragile theme-chains. Your job is the NARRATIVE SYNTHESIS the engine can't write: "
    "explain WHY the market is or isn't fragile right now, name the single leading-edge driver most "
    "likely to crack first and the fragility CHAIN it propagates through, and state what would prove "
    "you wrong. Do NOT overturn the deterministic state — interpret it. Confirmation over prediction; "
    "tag inferred signals (unverified); be blunt, no moralising. "
    "Reply ONLY with JSON: {\"rationale\": str, \"lead_driver\": str, \"driver_chains\": [str], "
    "\"probability_rolldown\": 0.0-1.0, \"what_proves_me_wrong\": str}."
)

_JSON_ONLY = (
    "\n\nCRITICAL OUTPUT CONTRACT: reply with ONLY a single valid JSON object — no markdown, no "
    "headers, no prose, no code fences. Your ENTIRE response must parse with json.loads, beginning "
    "with '{' and ending with '}'."
)


def _parse_json(txt: str) -> dict | None:
    if not txt:
        return None
    try:
        return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        return None


def _reformat_to_json(base_sys: str, txt: str | None) -> dict | None:
    if not txt:
        return None
    try:
        fix, _ = client.call_model(
            base_sys + _JSON_ONLY,
            "Convert the analysis below into the exact JSON object its schema requires. Output JSON "
            "ONLY.\n\n" + str(txt)[:8000], role="deep", max_tokens=1600)
        return _parse_json(fix)
    except Exception:  # noqa: BLE001
        return None


def macro_risk_assess(asof: str, regime: dict | None) -> dict:
    """The full Macro Risk Officer pass: the deterministic state ALWAYS, plus the Opus narrative when
    armed. Returns the risk_state dict augmented with ``rationale`` / ``lead_driver`` /
    ``llm_driver_chains`` / ``probability_rolldown`` / ``ran``. The deterministic state + teeth stand
    even if no LLM is reachable. Additive; never raises."""
    state = risk_state(asof, regime)
    state["ran"] = False
    if not enabled():
        return state
    try:
        payload = _macro_risk_input(regime, asof, state)
        try:
            from brain import self_mirror
            sys_prompt = self_mirror.inject(_MACRO_RISK_SYS, "macro_risk", _asof_d(asof))
        except Exception:  # noqa: BLE001
            sys_prompt = _MACRO_RISK_SYS
        txt, _meta = client.call_model(sys_prompt + _JSON_ONLY, json.dumps(payload, default=str),
                                       role="deep", max_tokens=1600)
    except Exception:  # noqa: BLE001 — the narrative is additive; never break the state
        return state
    j = _parse_json(txt) or _reformat_to_json(_MACRO_RISK_SYS, txt) or {}
    if j:
        state["rationale"] = str(j.get("rationale", ""))[:1200]
        state["lead_driver"] = str(j.get("lead_driver", ""))[:200]
        state["llm_driver_chains"] = [str(c)[:80] for c in (j.get("driver_chains") or [])][:8]
        try:
            state["probability_rolldown"] = round(max(0.0, min(1.0,
                                                  float(j.get("probability_rolldown", 0.0)))), 2)
        except (TypeError, ValueError):
            state["probability_rolldown"] = None
        state["what_proves_me_wrong"] = str(j.get("what_proves_me_wrong", ""))[:300]
        state["ran"] = True
    try:
        from brain import calibration as _calib
        state["calibration_multiplier"] = round(_calib.multiplier("macro_risk"), 3)
    except Exception:  # noqa: BLE001
        state["calibration_multiplier"] = 1.0
    return state


def _write_artifacts(asof: str, state: dict | None) -> str | None:
    d = _ARTIFACTS / (str(asof)[:10] or date.today().isoformat())
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({"agent": "macro_risk", "state": state},
                                             indent=2, default=str))
    st = state or {}
    md = [f"# Macro Risk Officer — {str(asof)[:10]}", "",
          f"**RISK STATE: {str(st.get('state', '')).upper()}** (fragility {st.get('fragility')})", "",
          f"Gross cap **{st.get('gross_cap')}** · adds {'ALLOWED' if st.get('allow_adds') else 'BLOCKED'}",
          "", "## Axes", ""]
    for k, v in (st.get("axes") or {}).items():
        md.append(f"- **{k}** ({v.get('fragility')}): {v.get('reason')}")
    md += ["", "## Leading-edge fragile drivers", ""]
    for dch in (st.get("drivers") or []):
        md.append(f"- **{dch.get('id')}** — {dch.get('driver')} (hot: {', '.join(dch.get('hot_overlap') or [])})")
    if not (st.get("drivers")):
        md.append("- (none flagged)")
    tilt = st.get("defensive_tilt") or {}
    md += ["", "## Defensive tilt (favor = advisory; avoid/cash-floor = enforced)", "",
           f"- archetype: **{tilt.get('archetype')}**",
           f"- favor: {', '.join(tilt.get('favor') or [])}",
           f"- avoid: {', '.join(tilt.get('avoid') or [])}",
           f"- {tilt.get('rate_sensitive_note', '')}", "",
           f"**Falsifier:** {st.get('falsifier', '')}  (check by {st.get('check_by')})"]
    if st.get("rationale"):
        md += ["", "## Narrative", "", st["rationale"]]
    (d / "macro_risk.md").write_text("\n".join(md))
    return str(d)


def run(asof: str, regime: dict | None) -> dict:
    """Convenience wrapper: assess (deterministic + optional narrative) + write artifact. Returns the
    risk_state dict. Never raises."""
    state = macro_risk_assess(asof, regime)
    try:
        _write_artifacts(asof, state)
    except Exception:  # noqa: BLE001
        pass
    return state
