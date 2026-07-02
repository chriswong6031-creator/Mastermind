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
# severity rank of each state (0 = safest). Escalation = a JUMP UP in rank; de-escalation = one step
# DOWN in rank. Used by the dwell state machine to compare the stateless read against the held state.
_RANK = {"risk_on": 0, "caution": 1, "risk_off": 2}

# the persistent dwell state — one file firm-wide, versioned, add-only fields (invariant: a corrupt or
# missing file degrades to today's STATELESS behaviour, never to a looser cap).
_STATE_MACHINE = _ARTIFACTS / "state_machine.json"
_STATE_VERSION = 1

# hard fallback dwell thresholds if config/doctrine.yml `risk_state:` is missing/unreadable. These are
# the SAME values shipped in doctrine.yml (unverified-prior); duplicated only so the machine still binds
# when the config can't be read (degrade-to-safe, never degrade-to-loose).
_DWELL_DEFAULTS = {
    "caution_exit_frag": 0.28, "risk_off_exit_frag": 0.50, "deescalate_sessions": 3,
    "escalation_cooldown_sessions": 2, "tripwire_clamp_sessions": 2, "tripwire_clamp_severity": 2,
    "max_dwell_sessions": 15,
}

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
# the fragility DWELL STATE MACHINE — persistent memory over the stateless scorer.
#
# WHY THIS EXISTS: the raw scorer is memoryless. On 2026-07-01 SOXX fell -6.4% intraday, and BECAUSE
# the crowded/complacent read collapses in a crash, that day's raw fragility read 0.121 (RISK_ON) —
# after two straight CAUTION sessions (0.516, 0.469). The stateless code un-capped the book 0.7 -> 1.0
# on the crash day and a severity-2 tripwire fired into an empty cap. The dwell machine makes that
# structurally impossible: state ESCALATES instantly (any run whose raw read is more severe jumps up
# same-run) but DE-ESCALATES slowly (3 consecutive sub-deadband sessions + a post-escalation cooldown),
# and de-escalation is HARD-CLAMPED whenever a severity>=2 tripwire fired in the last N sessions. A
# max-dwell auto-release prevents a stuck-forever CAUTION. INVARIANT: every failure path (corrupt file,
# unreadable config) degrades to the STATELESS read — coarser/tighter, never looser.
# ─────────────────────────────────────────────────────────────────────────────
def _dwell_cfg() -> dict:
    """The dwell thresholds from config/doctrine.yml `risk_state:`, overlaid on safe hard defaults.
    Any read failure keeps the defaults — the machine still binds (degrade-to-safe)."""
    cfg = dict(_DWELL_DEFAULTS)
    try:
        from bot.doctrine_config import load_doctrine
        rs = (load_doctrine() or {}).get("risk_state") or {}
        for k in cfg:
            if rs.get(k) is not None:
                cfg[k] = rs[k]
    except Exception:  # noqa: BLE001
        pass
    return cfg


def _load_state_machine() -> dict | None:
    """Read the persistent dwell state. Returns None on missing/corrupt/version-mismatch (→ the caller
    degrades to stateless). Never raises."""
    try:
        if not _STATE_MACHINE.exists():
            return None
        j = json.loads(_STATE_MACHINE.read_text())
        if not isinstance(j, dict) or j.get("v") != _STATE_VERSION:
            return None
        if j.get("state") not in _STATES:
            return None
        return j
    except Exception:  # noqa: BLE001
        return None


def _save_state_machine(j: dict) -> None:
    """Persist the dwell state (add-only fields, versioned). Never raises into the caller."""
    try:
        _STATE_MACHINE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_MACHINE.write_text(json.dumps(j, indent=2, default=str))
    except Exception:  # noqa: BLE001
        pass


def _recent_tripwire_severity(asof: str, sessions: int) -> int:
    """The MAX derisk tripwire severity observed in the last ``sessions`` dated artifact dirs up to and
    INCLUDING ``asof`` (bot/derisk.py writes data/macro_risk/<date>/derisk_*.json with a `severity`).
    A crash-day tripwire that fired while the raw state already read RISK_ON is exactly the signal that
    must block a de-escalation. Returns 0 on any miss. Never raises."""
    try:
        d0 = _asof_d(asof)
        if d0 is None:
            return 0
        # collect dated dirs (YYYY-MM-DD) at or before asof, take the most recent `sessions`
        dated: list[date] = []
        for p in _ARTIFACTS.iterdir():
            if not p.is_dir():
                continue
            dd = _asof_d(p.name)
            if dd is not None and dd <= d0:
                dated.append(dd)
        dated.sort(reverse=True)
        worst = 0
        for dd in dated[:max(1, int(sessions))]:
            ddir = _ARTIFACTS / dd.isoformat()
            for f in ddir.glob("derisk_*.json"):
                try:
                    j = json.loads(f.read_text())
                except Exception:  # noqa: BLE001
                    continue
                sev = ((j or {}).get("tripwire") or {}).get("severity")
                try:
                    sev = int(sev)
                except (TypeError, ValueError):
                    sev = 0
                worst = max(worst, sev)
        return worst
    except Exception:  # noqa: BLE001
        return 0


def _exit_deadband(state: str, cfg: dict) -> float:
    """The raw-fragility deadband a state must sit BELOW to earn a step down out of it."""
    if state == "risk_off":
        return float(cfg["risk_off_exit_frag"])
    if state == "caution":
        return float(cfg["caution_exit_frag"])
    return 0.0  # risk_on has no floor to leave


def _advance_dwell(prior: dict | None, raw_state: str, raw_frag: float, asof: str,
                   *, tripwire_sev: int, cfg: dict) -> dict:
    """PURE transition function: fold today's stateless read into the persistent dwell state. Returns
    the NEW dwell record (to persist). Rules, in order:

      * ESCALATE INSTANTLY — if the raw read outranks the held state, jump to it THIS run, reset the
        sub-deadband streak, and stamp the escalation session (arms the cooldown + starts the clamp).
      * TRIPWIRE CLAMP — a severity>=`tripwire_clamp_severity` derisk within the last
        `tripwire_clamp_sessions` sessions BLOCKS every de-escalation, regardless of streak/cooldown.
      * DE-ESCALATE SLOWLY — step DOWN one rank only when the raw read has been below the current
        state's exit deadband for `deescalate_sessions` CONSECUTIVE sessions AND at least
        `escalation_cooldown_sessions` sessions have elapsed since the last escalation.
      * MAX-DWELL AUTO-RELEASE — a state held longer than `max_dwell_sessions` sessions whose raw read
        is (this session) below its exit deadband releases one step even if the strict streak/cooldown
        aren't both met (prevents a stuck-forever CAUTION); still blocked by the tripwire clamp.

    ``prior`` None (cold start / corrupt file) seeds the machine at TODAY'S RAW state — degrade-to-
    stateless: with no history, the dwell state == the stateless read, so behaviour is unchanged."""
    if prior is None:
        return {
            "v": _STATE_VERSION, "state": raw_state, "asof": str(asof)[:10],
            "dwell": 1, "below_streak": 0, "sessions_since_escalation": 999,
            "last_escalation": None, "clamp_reason": None,
        }

    def _int(v, default):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    pstate = prior.get("state") if prior.get("state") in _STATES else raw_state
    dwell = _int(prior.get("dwell"), 1)
    below_streak = _int(prior.get("below_streak"), 0)
    since_esc = _int(prior.get("sessions_since_escalation"), 999)
    last_esc = prior.get("last_escalation")

    # is this a NEW session (distinct date) or a re-run of the same date? Only advance counters on a
    # genuinely new session so intraday re-runs don't inflate the dwell/streak counts.
    same_session = str(prior.get("asof") or "")[:10] == str(asof)[:10]

    # ── (1) ESCALATE INSTANTLY ────────────────────────────────────────────────────────────────
    if _RANK.get(raw_state, 0) > _RANK.get(pstate, 0):
        return {
            "v": _STATE_VERSION, "state": raw_state, "asof": str(asof)[:10],
            "dwell": 1 if not same_session else dwell,
            "below_streak": 0, "sessions_since_escalation": 0,
            "last_escalation": str(asof)[:10], "clamp_reason": None,
        }

    # not escalating → advance the session counters (new session only)
    if not same_session:
        dwell += 1
        since_esc = since_esc + 1 if since_esc < 900 else since_esc
        deadband = _exit_deadband(pstate, cfg)
        below_streak = below_streak + 1 if raw_frag < deadband else 0

    # ── (2) TRIPWIRE CLAMP — blocks ANY de-escalation ─────────────────────────────────────────
    clamp_reason = None
    if pstate != "risk_on" and tripwire_sev >= int(cfg["tripwire_clamp_severity"]):
        clamp_reason = (f"severity-{tripwire_sev} tripwire within last "
                        f"{int(cfg['tripwire_clamp_sessions'])} sessions blocks de-escalation")

    new_state = pstate
    if pstate != "risk_on" and clamp_reason is None:
        deadband = _exit_deadband(pstate, cfg)
        below_now = raw_frag < deadband
        streak_ok = below_streak >= int(cfg["deescalate_sessions"])
        cooldown_ok = since_esc >= int(cfg["escalation_cooldown_sessions"])
        max_dwell_hit = dwell > int(cfg["max_dwell_sessions"]) and below_now
        # ── (3) DE-ESCALATE SLOWLY  or  (4) MAX-DWELL AUTO-RELEASE ──
        if (streak_ok and cooldown_ok) or max_dwell_hit:
            rank = max(0, _RANK[pstate] - 1)
            new_state = _STATES[rank]
            dwell, below_streak = 1, 0

    return {
        "v": _STATE_VERSION, "state": new_state, "asof": str(asof)[:10],
        "dwell": dwell, "below_streak": below_streak,
        "sessions_since_escalation": since_esc, "last_escalation": last_esc,
        "clamp_reason": clamp_reason,
    }


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


def _dwell_enabled() -> bool:
    """The dwell state machine is ON by default (it is pure risk-reduction and degrades to stateless on
    any failure). ``MASTERMIND_DWELL=0`` reverts to the pre-W1 stateless behaviour byte-for-byte — the
    escape hatch the invariant requires (a new state machine must be defeatable without un-capping)."""
    return os.environ.get("MASTERMIND_DWELL", "1").strip().lower() in ("1", "true", "yes", "on")


def risk_state(asof: str, regime: dict | None, *,
               dwell: bool | None = None, state_loader=None, state_saver=None,
               tripwire_sev: int | None = None) -> dict:
    """Fuse the dashboard warning signals into a falsifiable RISK STATE. PURE; NEVER raises.

    Returns a self-describing, gradable dict::

        {agent:"macro_risk", asof, state, raw_state, fragility, supportive,
         axes:{volatility|credit_usd|liquidity|crowding|dealer_gamma:{fragility, reason}},
         signals:[str], drivers:[{id,name,driver,...}], hot_tickers:[str],
         falsifier:str, check_by:str, regime_growth, regime_inflation,
         gross_cap, allow_adds, defensive_tilt:{...},
         dwell:int, deescalation_progress:str, clamp_reason:str|None}

    ``state`` is now the DWELL state (persistent memory over the stateless read); ``raw_state`` is the
    stateless read the scorer produced this run. ``gross_cap``/``allow_adds`` derive from ``state`` (the
    dwell state) — that is the whole fix for the SOXX-crash-day un-cap. The dwell machine escalates
    instantly and de-escalates slowly (see ``_advance_dwell``); it is defeatable via ``MASTERMIND_DWELL=0``
    and degrades to the stateless read on any failure. Injection params (``dwell``/``state_loader``/
    ``state_saver``/``tripwire_sev``) exist for deterministic replay tests — production passes none.

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
        raw_state = "risk_off"
    elif fragility >= _caution_score():
        raw_state = "caution"
    else:
        raw_state = "risk_on"

    # ── the DWELL STATE MACHINE — persistent memory over the stateless raw read. Escalate instantly,
    # de-escalate slowly, hard-clamp on a hot tripwire. Everything below wraps the raw read in a
    # try/except so ANY failure falls back to the stateless read (degrade-to-stateless invariant).
    state = raw_state
    dwell_record: dict = {"state": raw_state, "dwell": 1, "below_streak": 0,
                          "sessions_since_escalation": 999, "clamp_reason": None}
    use_dwell = _dwell_enabled() if dwell is None else bool(dwell)
    if use_dwell:
        try:
            cfg = _dwell_cfg()
            loader = state_loader if state_loader is not None else _load_state_machine
            prior = loader()
            if tripwire_sev is not None:
                tsev = int(tripwire_sev)
            else:
                tsev = _recent_tripwire_severity(asof, int(cfg["tripwire_clamp_sessions"]))
            dwell_record = _advance_dwell(prior, raw_state, fragility, asof,
                                          tripwire_sev=tsev, cfg=cfg)
            state = dwell_record.get("state") or raw_state
            saver = state_saver if state_saver is not None else _save_state_machine
            saver(dwell_record)
        except Exception:  # noqa: BLE001 — never let the memory layer defeat the stateless teeth
            state = raw_state
            dwell_record = {"state": raw_state, "dwell": 1, "below_streak": 0,
                            "sessions_since_escalation": 999, "clamp_reason": None}

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

    # de-escalation progress readout (n/N) — how many of the required consecutive sub-deadband sessions
    # the current dwell state has accrued. Display + gradability; 0/N whenever fully de-escalated.
    _cfg = _dwell_cfg()
    _need = int(_cfg["deescalate_sessions"])
    _prog = min(int(dwell_record.get("below_streak") or 0), _need) if state != "risk_on" else 0
    out = {
        "agent": "macro_risk",
        "asof": str(asof)[:10],
        "state": state,                                   # the DWELL state (drives the teeth)
        "raw_state": raw_state,                            # the stateless read this run
        "fragility": fragility,                            # the raw fragility this run
        "supportive": state == "risk_on",
        "axes": axes,
        "signals": [axes[k]["reason"] for k in
                    sorted(axes, key=lambda k: axes[k]["fragility"], reverse=True)
                    if axes[k]["fragility"] > 0.0],
        "drivers": drivers,
        "hot_tickers": hot,
        "regime_growth": round(growth, 3),
        "regime_inflation": round(infl, 3),
        # dwell telemetry (add-only; consumers that don't read these degrade to today's behaviour)
        "dwell": int(dwell_record.get("dwell") or 1),
        "deescalation_progress": f"{_prog}/{_need}",
        "clamp_reason": dwell_record.get("clamp_reason"),
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
