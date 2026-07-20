"""CONTEXT GATE — the market-weather assessor for NEW entries (Flagship v2, W8 "the entry era").

The third axis of the buy triad (``research/FLAGSHIP_V2_DECISION_CORE.md`` §2.2): a name can be a
quality business (research) at a clean entry (entry_engine) and STILL be a bad buy because the
weather is against its cohort — its theme's health just broke, contagion is spreading from its
complex, the momentum cohort it belongs to is mid-unwind, or the risk regime demands stricter
confirmation than it carries. The 2026-07-13..16 tape is the type specimen: SMH bought with its
origin complex's leadership BROKEN and two mature intl risk-off markets in the supply chain; XLK
bought downstream of that same complex; MTUM bought with MTUM/SPY −8% over 20 sessions.

AUTHORITY MODEL (the ``_vol_regime_row`` precedent, asymmetric by design):
  * TIGHTENING is always allowed — cooling/broken/contagion/unwind reads may park or shrink a NEW
    entry even where the upstream product is display-tier ("descriptive"/"unconfirmed" grades).
    Unvalidated caution is safe.
  * LOOSENING is never allowed — a FAVORABLE read is a small score credit and a note; it can never
    bypass the entry engine, the confluence gate, or any veto.
  * HELD names are untouched (the refuted cycle veto stays dead — new-entry gating only).
  * FAIL-OPEN per input (charter P2): every artifact optional; a missing read contributes nothing.

Verdicts: ``blocked`` (park the new entry; triggers say what must clear) · ``against`` (allow at
0.6× entry size via the terminal-subtract composer) · ``favorable`` / ``neutral`` (no brake).
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# ── thresholds / maps — (unverified-prior) starter values, ledger-tuned later ──
_UNWIND_MTUM_SPY_20D = -5.0        # MTUM/SPY 20d underperformance ≤ −5pp → unwind condition
_CTX_AGAINST_MULT = 0.6            # entry-size multiplier for verdict=against (terminal subtract)
_COOLING_PENALTY = 10.0
_UNWIND_MEMBER_PENALTY = 15.0

# ETF → sector_pulse theme id (misses fail-open: no theme → the theme rules stay silent).
_ETF_THEME = {
    "XLK": "us_sector_tech", "XLF": "us_sector_financials", "XLE": "us_sector_energy",
    "XLV": "us_sector_healthcare", "XLI": "us_sector_industrials",
    "XLY": "us_sector_discretionary", "XLP": "us_sector_staples", "XLU": "us_sector_utilities",
    "XLB": "us_sector_materials", "XLRE": "us_sector_realestate", "XLC": "us_sector_communications",
}
# ETFs judged by CATEGORY membership (their theme is a whole complex, not one basket id).
_ETF_CATEGORY = {"SMH": "Semiconductors", "SOXX": "Semiconductors"}
# momentum-factor vehicles — blocked outright while the unwind condition holds.
_MOMENTUM_VEHICLES = {"MTUM", "SPMO", "IMTM", "JMOM"}
# defensive vehicles exempt from the risk_off new-entry block.
_DEFENSIVE = {"XLU", "XLP", "XLV", "GLD", "SHY", "BIL", "IEF", "TLT"}
# contagion origin complex → the sectors/categories downstream of it (supply-chain adjacency).
_CONTAGION_DOWNSTREAM = {
    "ai_hardware": {"Semiconductors", "Information Technology", "Technology"},
    "semis": {"Semiconductors", "Information Technology", "Technology"},
}


# ─────────────────────────────────────────────────────────────────────────────
# default loaders (all optional, all fail-open; tests inject)
# ─────────────────────────────────────────────────────────────────────────────
def _default_pulse():
    try:
        from portfolio import lenses
        return lenses._load("site/basketdata/sector_pulse.json")
    except Exception:  # noqa: BLE001
        return None


def _default_theme_ctx():
    try:
        from portfolio import lenses
        return lenses._load("site/basketdata/theme_context.json")
    except Exception:  # noqa: BLE001
        return None


def _default_contagion():
    """The NW contagion lobe from mastermind_context (fail-soft {})."""
    try:
        from brain import neural_web_context as nwc
        lobes = (nwc.context() or {}).get("lobes") or {}
        c = lobes.get("contagion")
        return c if isinstance(c, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _default_momentum():
    try:
        from portfolio import lenses
        return lenses._load("site/factordata/momentum_display.json")
    except Exception:  # noqa: BLE001
        return None


def _default_risk():
    """{state} from the regime file's risk_radar (canonical), else the live risk_state file."""
    try:
        from portfolio import lenses
        r = lenses._load("data/regime/latest.json") or {}
        rr = r.get("risk_radar") or {}
        if isinstance(rr, dict) and rr.get("state"):
            return {"state": str(rr.get("state")).lower(), "source": "risk_radar"}
        live = lenses._load("site/live/risk_state.json") or {}
        if isinstance(live, dict) and live.get("state"):
            return {"state": str(live.get("state")).lower(), "source": "risk_state_live"}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _default_memberships(ticker: str, stockdata: dict | None) -> tuple[list[str], str | None, str | None]:
    """(theme_ids, category, sector) for an equity from its stockdata: every basket-membership slug
    (not just the first — a name can sit in several cohorts), the pulse theme id when stamped, and
    the GICS sector. All fail-open."""
    ids: list[str] = []
    category = sector = None
    try:
        d = stockdata
        if d is None:
            from portfolio import lenses
            d = lenses._load(f"site/stockdata/{ticker}.json")
        if isinstance(d, dict):
            sector = d.get("sector")
            sp = d.get("sector_pulse")
            if isinstance(sp, dict) and sp.get("theme_id"):
                ids.append(str(sp["theme_id"]))
            mem = d.get("baskets_membership")
            if isinstance(mem, dict):
                mem = mem.get("themes")
            if isinstance(mem, list):
                for m in mem:
                    slug = m if isinstance(m, str) else (m or {}).get("slug") or (m or {}).get("id")
                    if isinstance(slug, str) and slug not in ids:
                        ids.append(slug)
    except Exception:  # noqa: BLE001
        pass
    return ids, category, sector


def _theme_rows_by_id(pulse: dict | None) -> dict:
    try:
        return {str(t.get("id")): t for t in ((pulse or {}).get("themes") or [])
                if isinstance(t, dict) and t.get("id")}
    except Exception:  # noqa: BLE001
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# the assessor
# ─────────────────────────────────────────────────────────────────────────────
def assess(ticker: str, *, sector: str | None = None, theme_id: str | None = None,
           entry_verdict: str | None = None, entry_tier_ok: bool | None = None,
           is_etf: bool | None = None, stockdata: dict | None = None,
           pulse: dict | None = None, theme_ctx: dict | None = None,
           contagion: dict | None = None, momentum: dict | None = None,
           risk: dict | None = None, as_of: str | None = None,
           _use_defaults: bool = True) -> dict:
    """The market-weather read for a would-be NEW entry. Returns
    ``{ticker, verdict, context_score, reasons, park_triggers, entry_mult, sources, as_of}``.

    ``entry_verdict`` / ``entry_tier_ok`` couple the risk-regime tightening to the entry class
    (caution demands more confirmation; elevated demands base/pullback + fresh tier). FAIL-OPEN:
    every absent input contributes nothing; the gate can only tighten, never loosen. Never raises."""
    t = (ticker or "").upper().strip()
    reasons: list[str] = []
    sources: list[str] = []
    verdict = "neutral"
    score = 60.0
    blocked = against = False
    park: dict = {}

    if pulse is None and _use_defaults:
        pulse = _default_pulse()
    if theme_ctx is None and _use_defaults:
        theme_ctx = _default_theme_ctx()
    if contagion is None and _use_defaults:
        contagion = _default_contagion()
    if momentum is None and _use_defaults:
        momentum = _default_momentum()
    if risk is None and _use_defaults:
        risk = _default_risk()

    etf = bool(is_etf) if is_etf is not None else (t in _ETF_THEME or t in _ETF_CATEGORY
                                                   or t in _MOMENTUM_VEHICLES)
    # ── resolve the candidate's cohort: theme ids + category + sector ──
    if etf:
        theme_ids = [x for x in (theme_id, _ETF_THEME.get(t)) if x]
        category = _ETF_CATEGORY.get(t)
        sec = sector
    else:
        m_ids, category, m_sector = _default_memberships(t, stockdata) if _use_defaults or stockdata \
            else ([], None, None)
        theme_ids = [x for x in ([theme_id] if theme_id else []) + m_ids if x]
        sec = sector or m_sector
    theme_ids = list(dict.fromkeys(theme_ids))      # de-dup, order-preserving (one read per theme)

    rows = _theme_rows_by_id(pulse)
    if rows:
        sources.append("sector_pulse")

    # 1) THEME HEALTH (theme_context strength/breaking + pulse heat) ------------------------------
    ld = (theme_ctx or {}).get("leadership") or {}
    if ld:
        sources.append("theme_context")
    strength_ids = {str(s.get("id")) for s in (ld.get("strength") or []) if isinstance(s, dict)}
    breaking_ids = {str(b.get("id")) for b in (ld.get("breaking") or []) if isinstance(b, dict)}
    breaking_cats = {str(b.get("category")) for b in (ld.get("breaking") or [])
                     if isinstance(b, dict) and b.get("category")}
    trailing_id = str(((ld.get("trailing_leader") or {}).get("id")) or "")
    lstate = str(ld.get("state") or "").lower()

    in_breaking = any(i in breaking_ids for i in theme_ids) or (category and category in breaking_cats)
    in_strength = any(i in strength_ids for i in theme_ids)
    if in_breaking:
        blocked = True
        reasons.append("theme health BROKEN for this cohort (leadership.breaking) — no new entries "
                       "into a breaking theme")
        park["want"] = "theme_heal"
    elif lstate in ("strain", "rotating") and trailing_id and trailing_id in theme_ids:
        against = True
        reasons.append(f"leadership {lstate} and this is the OLD leader's cohort "
                       f"({trailing_id}) — don't chase the trailing leader mid-rotation")
    if in_strength and not (blocked or against):
        score += 10.0
        reasons.append("theme in confirmed leadership strength")

    # pulse heat per membership theme: broken (backtested grade) blocks; cooling penalizes only.
    for tid in theme_ids:
        row = rows.get(tid)
        if not isinstance(row, dict):
            continue
        heat = row.get("heat")
        if heat == "broken":
            blocked = True
            reasons.append(f"sector_pulse heat BROKEN for {tid}")
            park.setdefault("want", "theme_heal")
        elif heat == "cooling":
            score -= _COOLING_PENALTY
            reasons.append(f"sector_pulse heat cooling for {tid}")
        elif heat in ("heating", "hot") and (row.get("clean_entry") or {}).get("flag") is True \
                and not (blocked or against):
            score += 5.0
            reasons.append(f"{tid} heating with a clean entry — the tailwind case")

    # 2) CONTAGION (NW lobe; tighten-only, composed from the lobe's own hard fields) --------------
    cg = contagion if isinstance(contagion, dict) else {}
    if cg:
        sources.append("contagion")
    origin = str(cg.get("origin_complex") or "").lower()
    downstream = _CONTAGION_DOWNSTREAM.get(origin, set())
    in_complex = bool(downstream and ((sec and sec in downstream) or
                                      (category and category in downstream)))
    if in_complex:
        mature_intl = sum(1 for m in (cg.get("intl_markets_in_alert") or [])
                          if isinstance(m, dict) and m.get("mature"))
        pressure = sum([
            1 if str(cg.get("leadership_state") or "").upper() == "BROKEN" else 0,
            1 if mature_intl >= 2 else 0,
            1 if (cg.get("n_alert") or 0) > 0 or (cg.get("d3_alert") or 0) > 0 else 0,
            1 if str(cg.get("us_spillover") or "contained").lower() != "contained" else 0,
        ])
        if pressure >= 2:
            blocked = True
            reasons.append(f"contagion pressure on the {origin} complex "
                           f"(leadership {cg.get('leadership_state')}, {mature_intl} mature intl "
                           "risk-off markets) — no new entries in/downstream of the origin")
            park.setdefault("want", "contagion_clear")
        elif pressure == 1:
            against = True
            reasons.append(f"contagion watch on the {origin} complex — reduced entry size")

    # 3) MOMENTUM UNWIND (descriptive X-ray; tighten-only) ----------------------------------------
    md = momentum if isinstance(momentum, dict) else {}
    mtum20 = ((md.get("pulse") or {}).get("mtum_spy_20d_pct"))
    unwinding = isinstance(mtum20, (int, float)) and mtum20 <= _UNWIND_MTUM_SPY_20D
    if md:
        sources.append("momentum_display")
    if unwinding:
        if t in _MOMENTUM_VEHICLES:
            blocked = True
            reasons.append(f"momentum unwind in force (MTUM/SPY 20d {mtum20:+.1f}pp) — no new "
                           "momentum-vehicle entries mid-unwind")
            park.setdefault("want", "unwind_end")
        else:
            sample = {str(s).upper() for s in ((md.get("top_decile") or {}).get("sample") or [])}
            if t in sample:
                score -= _UNWIND_MEMBER_PENALTY
                against = True
                reasons.append(f"top-decile momentum name during an unwind "
                               f"(MTUM/SPY 20d {mtum20:+.1f}pp) — reduced entry size")

    # 4) RISK REGIME (graduated confirmation demands; never loosens) ------------------------------
    rstate = str((risk or {}).get("state") or "").lower()
    if rstate:
        sources.append("risk_regime")
    ev = (entry_verdict or "").lower()
    confirmed_entry = ev in ("base_turn", "pullback_in_trend")
    if rstate == "caution" and not confirmed_entry and entry_tier_ok is False:
        against = True
        reasons.append("risk caution — a plain entry needs tier/clean-entry confirmation it lacks")
    elif rstate == "elevated" and not confirmed_entry:
        blocked = True
        reasons.append("risk elevated — only base-turn / pullback entries with fresh tiers")
        park.setdefault("want", "risk_ease")
    elif rstate in ("risk_off", "risk-off") and t not in _DEFENSIVE:
        blocked = True
        reasons.append("risk_off — no new non-defensive entries")
        park.setdefault("want", "risk_ease")

    # ── compose (tighten-only precedence: blocked > against > favorable > neutral) ──
    if blocked:
        verdict = "blocked"
        score = min(score, 20.0)
    elif against:
        verdict = "against"
        score = min(score, 45.0)
    elif score > 65.0:
        verdict = "favorable"
    score = max(0.0, min(100.0, score))

    return {
        "ticker": t, "verdict": verdict, "context_score": round(score, 1),
        "reasons": reasons,
        "park_triggers": park or None,
        "entry_mult": _CTX_AGAINST_MULT if verdict == "against" else 1.0,
        "sources": sorted(set(sources)), "as_of": as_of,
    }


def still_blocked_reason(ticker: str, *, is_etf: bool | None = None) -> str | None:
    """Watchlist re-review predicate leg: None when the weather has cleared (verdict no longer
    'blocked' — fail-open on any error), else a short reason. Never raises."""
    try:
        rep = assess(ticker, is_etf=is_etf)
        if rep.get("verdict") != "blocked":
            return None
        rs = rep.get("reasons") or []
        return "context blocked" + (f" — {rs[0]}" if rs else "")
    except Exception:  # noqa: BLE001
        return None
