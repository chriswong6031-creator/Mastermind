"""The multi-sided decision matrix — approach every decision from all sides.

NOT a weighted blend (the repo proved blended thematic momentum is rank-IC ~ 0 — averaging
24 lenses would be the worst overfit in the building). Instead three rules:
  (a) DECISION MATRIX — every lens shown as a row with its value + honest status tag.
  (b) CONFLUENCE gates SIZE — how many honest lenses agree, subject to hard vetoes.
  (c) DIVERGENCE is the edge or the trap — where lenses disagree, name which side leads.

Validated lenses (drawdown cone, extension veto) hold decision authority; context lenses
inform conviction but can't drive size alone; a hard veto (parabolic / Altman distress /
cycle-blocked) caps size at 0 no matter how many lenses are bullish. Reads the real engine
outputs under vendor/macro (graceful: a missing field is shown as absent, never imputed).
"""
from __future__ import annotations

import json
from pathlib import Path

import bot  # noqa: F401

_V = Path(__file__).resolve().parent.parent / "vendor" / "macro"


def _load(rel: str):
    p = _V / rel
    return json.loads(p.read_text()) if p.exists() else None


def _g(d, path, default=None):
    cur = d
    for k in path.split("."):
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


def _row(lens, value, status, direction, note=""):
    return {"lens": lens, "value": value, "status": status, "direction": direction, "note": note}


# ---------------- alt-data flow lens (Signal Intelligence Desk / Quiver / TrumpFlow) ----------------
def _intelligence() -> dict:
    """The unified per-ticker News+Intelligence bundle (macro engine.intelligence) — one
    file, two facts per name ({news, alt}). Falls back to the standalone feeds when absent."""
    return ((_load("site/intelligence/by_ticker.json") or {}).get("tickers")
            or (_load("data/intelligence/by_ticker.json") or {}).get("tickers") or {})


def _altdata_by_ticker() -> dict:
    """Per-ticker alt-data substrate (engine/altdata_signals, by_ticker.v2)."""
    return _load("site/altdata/by_ticker.json") or _load("data/altdata/by_ticker.json") or {}


def _altdata_mastermind() -> list:
    """The Signal Intelligence Desk's scored emit (engine/altdata_emit, mastermind.v1)."""
    return (_load("site/altdata/mastermind.json") or _load("data/altdata/mastermind.json") or {}).get("signals") or []


def _alt_record(t: str):
    """Best alt-data read for a name: the unified bundle's alt sub-object → the scored
    mastermind signal → the unscored by_ticker.v2 record (only if it has a real channel)."""
    t = t.upper()
    uni = _intelligence().get(t)
    if uni and uni.get("alt"):
        return uni["alt"]
    for s in _altdata_mastermind():
        if (s.get("ticker") or "").upper() == t:
            return {**s, "scored": True}
    rec = (_altdata_by_ticker().get("tickers") or {}).get(t)
    if rec and (rec.get("channels") or rec.get("convergence_score")):
        return {**rec, "scored": False}
    return None


def _altdata_row(t: str):
    """Political/insider/government-contract/affiliation SIGNAL on a name — the supply-side
    'what smart money is DOING' read (vs news_flow's demand-side 'what the tape is SAYING').
    CONTEXT-ONLY — informs conviction + the early-edge/crowded-late divergence, never sizes
    alone. Consumes the scored Signal Intelligence Desk emit when present; legacy convergence
    counts otherwise. None when the name isn't flagged."""
    rec = _alt_record(t)
    if not rec:
        return None
    # ---- scored Signal Intelligence Desk read (signal_score 0-100 + action) ----
    if rec.get("signal_score") is not None:
        score = int(rec.get("signal_score") or 0)
        action = (rec.get("action") or "WATCH").upper()
        conviction = rec.get("conviction") or "low"
        extended = bool(rec.get("extended"))
        affs = rec.get("affiliations") or []
        # bull on a strong buy-side signal; bear on an AVOID/weak read. Extension is NOT
        # gated here — the matrix's extension lens + the political_crowd_trap divergence
        # cross-check it, so a strong-flow-into-extended name stays 'bull' for the trap to catch.
        direction = "bull" if (score >= 65 and action != "AVOID") else \
                    "bear" if (action == "AVOID" or score < 35) else "neutral"
        note = f"signal {score}/100 · {conviction} · {action}"
        if affs:
            note += f" · {len(affs)} affiliated"
        if extended:
            note += " · extended"
        return _row("altdata_flow", {
            "signal_score": score, "conviction": conviction, "action": action,
            "scored_direction": rec.get("direction"), "channels": rec.get("channels") or [],
            "weighted_score": rec.get("weighted_score"), "convergence_score": rec.get("convergence_score"),
            "trump_linked": bool(rec.get("trump_linked")), "rs_vs_spy_60d": rec.get("rs_vs_spy_60d"),
            "extended": extended, "affiliations": affs, "thesis": rec.get("thesis"),
            "falsifier": rec.get("falsifier"), "scored": True}, "context", direction, note)
    # ---- legacy unscored convergence-count read (by_ticker v1/v2) ----
    score = int(rec.get("convergence_score") or 0)
    chans = rec.get("channels") or []
    trump = bool(rec.get("trump_linked"))
    insider = rec.get("insider_net_usd") or 0
    congress = rec.get("congress_net") or 0
    bull = score >= 2 or insider > 0 or congress >= 2 or (trump and rec.get("trump_side") == "buy")
    bear = score == 0 and (rec.get("trump_side") == "sell" or insider < 0)
    direction = "bull" if bull else "bear" if bear else "neutral"
    note = (f"{score}-channel convergence: " + ", ".join(chans)) if score >= 2 else (", ".join(chans) or "alt-data flow")
    if trump:
        note += " · Trump-linked"
    return _row("altdata_flow", {
        "convergence_score": score, "channels": chans, "trump_linked": trump,
        "weighted_score": rec.get("weighted_score"),
        "congress_net": rec.get("congress_net"), "gov_contract_usd_30d": rec.get("gov_contract_usd_30d"),
        "insider_net_usd": rec.get("insider_net_usd"), "dpi_lean": rec.get("dpi_lean"),
        "trump_side": rec.get("trump_side"), "scored": False}, "context", direction, note)


# ---------------- news-flow lens (public-record financial news) ----------------
def _news_by_ticker() -> dict:
    """Per-ticker news-flow substrate published by the macro engine
    (engine/news_signals). Tries the site contract, then the data store."""
    return ((_load("site/news/by_ticker.json") or {}).get("tickers")
            or (_load("data/news/by_ticker.json") or {}).get("tickers")
            or {})


def _news_row(t: str):
    """Public-record financial-news flow for a name — recent headline count, aggregated
    sentiment lean, basket/sector membership from the news surface.
    CONTEXT-ONLY (display-only in the macro engine) — informs narrative/conviction,
    never drives size alone. None when the name has no news record."""
    # unified bundle's news sub-object first, else the standalone news feed
    rec = (_intelligence().get(t.upper(), {}) or {}).get("news") or _news_by_ticker().get(t.upper())
    if not rec:
        return None
    n_recent = rec.get("n_recent") or 0
    lean = rec.get("sentiment_lean") or "neutral"
    n_pos = rec.get("n_pos") or 0
    n_neg = rec.get("n_neg") or 0
    baskets = rec.get("baskets") or []
    sectors = rec.get("sectors") or []
    is_mag7 = bool(rec.get("is_mag7"))
    direction = "bull" if lean == "pos" else "bear" if lean == "neg" else "neutral"
    note = f"{n_recent} recent headlines · lean {lean}"
    if baskets:
        note += f" · {baskets[0]}"
    return _row("news_flow", {
        "n_recent": n_recent, "sentiment_lean": lean, "n_pos": n_pos, "n_neg": n_neg,
        "baskets": baskets, "sectors": sectors, "is_mag7": is_mag7},
        "context", direction, note)


# ---------------- per-NAME lenses ----------------
def _name_rows(t: str) -> list[dict]:
    d = _load(f"site/stockdata/{t}.json")
    flows = (_load("site/stockdata/fund_flows.json") or {}).get(t)
    gx = _load(f"site/gex/{t}.json")
    ad = _altdata_row(t)                 # political/insider/contract flow — independent of stockdata
    nw = _news_row(t)                    # public-record financial news flow — context only
    rows = []
    if not d:
        # a Trump-linked entity (ABTC/DJT/...) may carry alt-data flow but no S&P stockdata
        base = ([ad] if ad else []) + ([nw] if nw else [])
        return base + [_row("conviction", None, "missing", None, "no stockdata")]

    # valuation
    vz = _g(d, "valuation.value_z")
    cheap = _g(d, "valuation.trailing_pe.cheap")
    fwd = _g(d, "valuation.forward_pe")
    dirv = "bull" if (vz or 0) > 0.3 or (cheap or 50) > 65 else "bear" if (vz or 0) < -0.3 or (cheap or 50) < 35 else "neutral"
    rows.append(_row("valuation", {"value_z": vz, "cheap_pctile": cheap, "forward_pe": fwd,
                                   "basis": "forward" if fwd else "trailing-only"}, "context", dirv))

    # quality / accounting
    qz = _g(d, "conviction.axes.quality.z")
    acct = _g(d, "conviction.axes.quality.flags.accounting") or _g(d, "accounting_quality.verdict")
    dirq = "bear" if acct == "warn" else "bull" if (qz or 0) > 0.3 else "neutral"
    rows.append(_row("quality", {"quality_z": qz, "accounting": acct}, "context", dirq))

    # growth
    rc, ec = _g(d, "financials.multiyear.rev_cagr"), _g(d, "financials.multiyear.eps_cagr")
    g = rc if rc is not None else ec
    dirg = "bull" if (g or 0) > 10 else "bear" if (g is not None and g < 0) else "neutral"
    rows.append(_row("growth", {"rev_cagr": rc, "eps_cagr": ec}, "partial" if g is not None else "missing", dirg))

    # solvency (Altman / Piotroski) — feeds the distress veto
    az = _g(d, "financials.multiyear.altman.zone")
    rows.append(_row("solvency", {"altman_zone": az, "piotroski": _g(d, "financials.multiyear.piotroski.score")},
                     "partial" if az else "missing", "bear" if az == "distress" else "neutral"))

    # potential / asymmetry (upside vs downside cone) — derived
    mfe = _g(d, "anticipation.horizons.medium.mfe_med")
    dda = _g(d, "anticipation.horizons.medium.dd_avg")
    asym = (mfe / abs(dda)) if (mfe is not None and dda) else None
    dira = "bull" if (asym or 0) > 1.5 else "bear" if (asym is not None and asym < 0.8) else "neutral"
    rows.append(_row("asymmetry", {"upside_downside": round(asym, 2) if asym else None, "mfe_med": mfe, "dd_avg": dda},
                     "partial" if asym else "missing", dira))

    # risk — drawdown cone (VALIDATED)
    ddt = _g(d, "anticipation.horizons.medium.dd_tail")
    rows.append(_row("risk_drawdown", {"dd_tail": ddt, "p_up": _g(d, "anticipation.horizons.medium.p_up"),
                                       "thin": _g(d, "anticipation.horizons.medium.thin")},
                     "validated", "bear" if (ddt or 0) < -25 else "neutral"))

    # risk — extension veto (VALIDATED)
    grade = _g(d, "conviction.ext.grade") or _g(d, "conviction.extension.grade")
    para = bool(_g(d, "conviction.ext.parabolic") or _g(d, "conviction.extension.parabolic"))
    pv2 = _g(d, "tech.pct_vs_200dma")
    rows.append(_row("extension", {"grade": grade, "parabolic": para, "pct_vs_200dma": pv2}, "validated",
                     "bear" if (para or grade in ("stretched", "parabolic") or (pv2 or 0) >= 30) else "neutral"))

    # flows — 13F smart money
    nb, ns = _g(d, "smart_money.n_buying"), _g(d, "smart_money.n_selling")
    dirf = "bull" if (nb or 0) > (ns or 0) else "bear" if (ns or 0) > (nb or 0) else "neutral"
    rows.append(_row("flows_13f", {"n_buying": nb, "n_selling": ns, "vip": _g(d, "smart_money.vip")},
                     "context" if (nb is not None) else "missing", dirf if nb is not None else None))

    # flows — active ETF accumulation
    if flows:
        sig = flows[0] if isinstance(flows, list) else flows
        fd = _g(sig, "direction")
        dire = "bull" if fd in ("add", "accumulate", "accumulating") else "bear" if fd in ("trim", "exit", "distributing") else "neutral"
        rows.append(_row("flows_etf", {"direction": fd, "conviction_pp": _g(sig, "conviction_pp")}, "context", dire))
    else:
        rows.append(_row("flows_etf", None, "missing", None))

    # options positioning
    if gx:
        reg = _g(gx, "summary.gamma_regime") or _g(gx, "summary.regime")
        rows.append(_row("options", {"gamma_regime": reg, "call_wall": _g(gx, "summary.call_wall"),
                                     "put_wall": _g(gx, "summary.put_wall"), "exp_move_d": _g(gx, "expected_move.daily_pct")},
                         "context", "bull" if reg in ("positive", "long") else "bear" if reg in ("negative", "short") else "neutral"))
    else:
        rows.append(_row("options", None, "missing", None))

    # rate/inflation sensitivity
    ms = _g(d, "macro_sensitivity")
    if ms:
        rg = _g(ms, "regime")
        rows.append(_row("rate_inflation", {"tier": _g(ms, "tier"), "duration": _g(ms, "duration"), "regime": rg},
                        "context", "bull" if rg == "tailwind" else "bear" if rg == "headwind" else "neutral"))
    else:
        rows.append(_row("rate_inflation", None, "missing", None))

    # conviction composite (the net per-name read)
    band = _g(d, "conviction.band")
    sz = _g(d, "conviction.size.pct")
    rows.append(_row("conviction", {"band": band, "score": _g(d, "conviction.score"), "size_pct": sz,
                                    "verdict": _g(d, "conviction.verdict"),
                                    "cycle_blocked": bool(_g(d, "conviction.cycle_blocked"))},
                     "partial", "bull" if band in ("strong", "high") else "bear" if band == "avoid" else "neutral"))

    # alt-data political/insider/contract flow (context)
    if ad:
        rows.append(ad)
    # news-flow: public-record financial news (context)
    if nw:
        rows.append(nw)
    # name -> theme via basket membership: narrative + policy
    rows += _theme_context_for_name(d)
    rows += _macro_rows()
    return rows


def _theme_context_for_name(d) -> list[dict]:
    mem = _g(d, "baskets_membership") or _g(d, "baskets_membership.themes")
    theme_id = None
    if isinstance(mem, list) and mem:
        theme_id = mem[0] if isinstance(mem[0], str) else _g(mem[0], "id")
    return [_row("narrative", {"basket": theme_id}, "partial" if theme_id else "missing", None,
                 "see get_decision_matrix(theme) for the theme read")]


# ---------------- per-THEME lenses ----------------
def _theme_rows(theme_id: str) -> list[dict]:
    alloc = _load("site/allocationdata/allocation.json") or {}
    rank = next((r for r in (alloc.get("ranks") or []) if r.get("id") == theme_id or r.get("theme") == theme_id), None)
    rows = []
    if rank:
        elig = rank.get("eligible")
        rows.append(_row("narrative", {"rank": rank.get("rank"), "score": rank.get("score"),
                                       "eligible": elig, "durability": rank.get("durability")}, "partial",
                         "bull" if elig else "bear" if elig is False else "neutral"))
    else:
        rows.append(_row("narrative", None, "missing", None))
    rows.append(_policy_row(theme_id))
    rows += _macro_rows()
    return rows


def _policy_row(theme_id: str) -> dict:
    intel = _load("data/policy/intel.json") or {}
    rot = intel.get("rotation") or {}
    targ = {(_g(x, "theme") or _g(x, "sector") or str(x)).lower() for x in (rot.get("targeted") or [])}
    starv = {(_g(x, "theme") or _g(x, "sector") or str(x)).lower() for x in (rot.get("starved") or [])}
    tid = (theme_id or "").lower()
    d = "bull" if tid in targ else "bear" if tid in starv else "neutral"
    return _row("policy_tilt", {"targeted": tid in targ, "starved": tid in starv,
                                "grand_strategy": _g(intel, "administration.grand_strategy")},
                "context", d if (targ or starv) else None)


# ---------------- macro lenses (shared) ----------------
def _macro_rows() -> list[dict]:
    r = _load("data/regime/latest.json") or {}
    mr = _g(r, "macro_risk.score")
    cuts = _g(r, "fed_path.implied_cuts_12m")
    ca = _g(r, "cross_asset.verdict")
    return [
        _row("macro_risk", {"score": mr, "quad": r.get("quad"), "liquidity": r.get("liquidity_overlay")},
             "context", "bear" if (mr or 0) > 0.66 else "bull" if (mr or 0) < 0.34 else "neutral"),
        _row("fed_path", {"implied_cuts_12m": cuts, "lean": _g(r, "fed_path.headline")}, "context",
             "bull" if (cuts or 0) > 0 else "bear" if (cuts or 0) < 0 else "neutral"),
        _row("cross_asset", {"verdict": ca, "absorption": _g(r, "cross_asset.absorption_ratio")}, "context",
             "bear" if ca in ("concentrated", "one-trade", "fragile") else "neutral"),
    ]


# ---------------- the public API ----------------
def decision_matrix(subject: str, kind: str = "name") -> dict:
    rows = _name_rows(subject.upper()) if kind == "name" else _theme_rows(subject)
    return {"subject": subject, "kind": kind, "rows": rows}


def _hard_vetoes(rows: list[dict]) -> list[str]:
    v = []
    for r in rows:
        val = r.get("value") or {}
        if r["lens"] == "extension" and val.get("parabolic"):
            v.append("parabolic")
        if r["lens"] == "solvency" and val.get("altman_zone") == "distress":
            v.append("altman_distress")
        if r["lens"] == "conviction" and (val.get("cycle_blocked") or val.get("band") == "avoid" or val.get("size_pct") == 0):
            v.append("cycle_blocked")
    return v


def _divergences(rows: list[dict]) -> list[dict]:
    g = {r["lens"]: r for r in rows}

    def d(lens):
        return (g.get(lens) or {}).get("direction")
    # name-level: conviction band proxies "is this a hot leader/story"; theme-level uses narrative
    lead = d("narrative") or d("conviction")
    out = []
    if lead == "bull" and d("valuation") == "bear" and d("flows_13f") == "bear":
        out.append({"pattern": "distribution",
                    "read": "story loudest as smart money leaves — hot/high-conviction but expensive + 13F-selling → late-stage, trim/avoid"})
    if d("flows_13f") == "bull" and d("valuation") == "bull" and lead != "bull":
        out.append({"pattern": "early_edge",
                    "read": "price hasn't moved but smart money + value are in — highest-asymmetry early-follow"})
    if lead == "bull" and d("valuation") == "bull" and d("flows_13f") == "bull" and "parabolic" not in _hard_vetoes(rows):
        out.append({"pattern": "high_confluence_buy",
                    "read": "all sides align and risk vetoes pass → full-size candidate"})
    if d("extension") == "bear" and d("flows_etf") == "bear":
        out.append({"pattern": "crowded_top",
                    "read": "extended + flows rolling over → avoid regardless of narrative heat"})
    if d("policy_tilt") == "bull" and d("valuation") == "bull" and lead != "bull":
        out.append({"pattern": "policy_early",
                    "read": "policy tailwind + cheap before the crowd notices — early thematic edge"})
    # alt-data flow (political/insider/contract convergence) — the edge or the trap
    ad = d("altdata_flow")
    if ad == "bull" and d("extension") == "bear":
        out.append({"pattern": "political_crowd_trap",
                    "read": "political/insider/contract money piling into an already-EXTENDED name — late; the political crowd may be the exit liquidity, not the edge"})
    elif ad == "bull" and lead != "bull" and d("valuation") != "bear":
        out.append({"pattern": "political_flow_early",
                    "read": "political/insider/contract flow converging on a name the tape hasn't recognized yet — early alt-data edge (context, not a size driver)"})
    return out


def synthesize(matrix: dict) -> dict:
    rows = matrix["rows"]
    scored = [r for r in rows if r["direction"] in ("bull", "bear")]
    bull = sum(1 for r in scored if r["direction"] == "bull")
    bear = sum(1 for r in scored if r["direction"] == "bear")
    vetoes = _hard_vetoes(rows)
    confluence = round((bull - bear) / max(len(scored), 1), 3)
    return {"bull": bull, "bear": bear, "n_scored": len(scored), "confluence": confluence,
            "vetoes": vetoes, "divergences": _divergences(rows),
            "size_authority": "blocked" if vetoes else ("up" if confluence > 0.3 else "down" if confluence < -0.3 else "hold")}


def full(subject: str, kind: str = "name") -> dict:
    m = decision_matrix(subject, kind)
    return {**m, "synthesis": synthesize(m)}
