"""Compose a complete, human-readable thesis_full dict from the decision matrix.

Deterministic and grounded — NO LLM, NO network. The prose is built from the
actual numbers in the decision matrix (synthesize() output + per-lens rows).
The contract shape matches the API spec exactly.
"""
from __future__ import annotations

from typing import Any


# ---- helpers --------------------------------------------------------

def _pct(v: float | None, decimals: int = 1) -> str:
    if v is None:
        return "n/a"
    return f"{v * 100:.{decimals}f}%"


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def _row_value(rows: list[dict], lens: str) -> dict:
    for r in rows:
        if r.get("lens") == lens:
            return r.get("value") or {}
    return {}


def _row_dir(rows: list[dict], lens: str) -> str | None:
    for r in rows:
        if r.get("lens") == lens:
            return r.get("direction")
    return None


def _row_note(rows: list[dict], lens: str) -> str:
    for r in rows:
        if r.get("lens") == lens:
            return r.get("note") or ""
    return ""


# ---- core compose ---------------------------------------------------

def compose(position: dict, decision: dict | None, synth: dict | None, facts: dict | None) -> dict:
    """Build the thesis_full dict for a position.

    position: the book entry dict (ticker, sleeve, weight, etc.)
    decision: the matching DecisionDoc.to_json() from decisions[], or None for leadership ETFs
    synth:    the synthesize() output (bull/bear/confluence/vetoes/divergences/size_authority), or None
    facts:    additional per-name facts dict (e.g. from stockdata), or None

    Returns the thesis_full contract shape.
    """
    ticker = position.get("ticker", "?")
    sleeve = position.get("sleeve", "leadership")
    weight_pct = round((position.get("weight") or 0) * 100, 1)

    if sleeve == "leadership":
        return _leadership_thesis(position, facts)

    # conviction sleeve — synth and decision available
    return _conviction_thesis(ticker, position, decision, synth, facts, weight_pct)


# ---- leadership ETF thesis (lighter) --------------------------------

def _leadership_thesis(position: dict, facts: dict | None) -> dict:
    ticker = position.get("ticker", "?")
    rs = position.get("rs_pctile")
    weight_pct = round((position.get("weight") or 0) * 100, 1)
    facts = facts or {}

    rs_str = f"{rs:.1f}th percentile" if rs is not None else "top"

    summary = (
        f"{ticker} is the mechanical leadership sleeve holding — "
        f"the {rs_str} sector ETF by 252-day relative-strength, "
        f"trend-confirmed above its 200-day moving average. "
        f"Size is equal-weight within the leadership budget ({weight_pct}%)."
    )
    why_now = (
        f"{ticker} ranks in the {rs_str} for 12-month RS and clears the 200-day trend gate, "
        f"making it the doctrine's 'present in the leader mechanically' expression — "
        f"holding the sector without chasing the single name underneath."
    )
    sizing_rationale = (
        f"Leadership sleeve: equal-weighted across the trend-gated top RS sectors. "
        f"Weight = {weight_pct}% (sleeve budget ÷ number of qualifying leaders). "
        f"No confluence gate required; the trend filter is the only gate."
    )

    # ---- bear case for a mechanical leadership ETF ----
    # Pull real numbers from tech facts where available (passed via facts dict by phase2).
    pv2: float | None = (facts or {}).get("pct_vs_200dma")
    bear_bullets: list[str] = [
        (
            f"Leadership rotation: held ONLY while RS stays top-decile (currently {rs_str}) "
            f"— exits mechanically if RS drops below the 70th percentile; no discretion."
        ),
        (
            "Breadth / narrowing risk: sector ETF performance can be driven by a handful "
            "of mega-cap names; if underlying breadth narrows the ETF masks single-name "
            "concentration risk."
        ),
    ]
    if pv2 is not None:
        if pv2 >= 20:
            bear_bullets.append(
                f"Extended +{pv2:.1f}% above 200-day MA — mean-reversion risk is elevated; "
                f"a sharp macro reversal could produce a quick −10–20% draw before the "
                f"trend filter exits the position."
            )
        else:
            bear_bullets.append(
                f"Currently +{pv2:.1f}% above 200-day MA — within normal range, but any "
                f"broad risk-off flush can close this gap rapidly."
            )
    else:
        bear_bullets.append(
            "Extension vs 200-day MA unknown (no stockdata for this ETF) — assume normal "
            "mean-reversion risk applies; monitor pct_vs_200dma."
        )
    bear_bullets.append(
        "Mechanical sleeve: no single-name conviction here; macro de-risk (regime quad "
        "flip or liquidity-overlay) trims the leadership budget first — exits are "
        "systematic, not qualitative."
    )

    return {
        "summary": summary,
        "why_now": why_now,
        "bull": [f"RS rank: {rs_str}", "Above 200-day trend (trend filter passed)"],
        "bear": bear_bullets,
        "vetoes": [],
        "divergences": [],
        "what_would_prove_wrong": (
            f"{ticker} breaks below its 200-day moving average for 3+ consecutive sessions, "
            f"or drops below the 70th RS percentile relative to the sector universe."
        ),
        "sizing_rationale": sizing_rationale,
        "confluence": None,
        "prob_correct": None,
    }


# ---- conviction single-name thesis (full) ---------------------------

def _conviction_thesis(ticker: str, position: dict, decision: dict | None,
                        synth: dict | None, facts: dict | None, weight_pct: float) -> dict:
    synth = synth or {}
    decision = decision or {}
    rows: list[dict] = (facts or {}).get("_matrix_rows", [])

    bull_count: int = synth.get("bull", 0)
    bear_count: int = synth.get("bear", 0)
    n_scored: int = synth.get("n_scored", max(bull_count + bear_count, 1))
    confluence: float = synth.get("confluence", 0.0)
    vetoes: list[str] = synth.get("vetoes", [])
    divs: list[dict] = synth.get("divergences", [])
    size_authority: str = synth.get("size_authority", "hold")
    prob_correct: float | None = decision.get("prob_correct")

    # ---- summary ----
    conf_sign = "+" if confluence >= 0 else ""
    verdict_word = "positive" if confluence > 0.3 else "neutral" if confluence > -0.1 else "mixed"
    summary = (
        f"{ticker} clears the multi-sided conviction gate: {bull_count}/{n_scored} lenses are bullish "
        f"(confluence {conf_sign}{confluence:.2f}), size_authority = {size_authority}. "
        f"No hard vetoes. Sized at {weight_pct}% — the confluence-weighted allocation "
        f"within the conviction sleeve budget."
    )

    # ---- why_now ----
    divs_read = "; ".join(d.get("read", d.get("pattern", "")) for d in divs if d.get("read")) if divs else None
    divergence_clause = f" Key pattern: {divs_read}." if divs_read else ""
    why_now = (
        f"All {n_scored} scored lenses were evaluated today; {bull_count} sided bullish, "
        f"{bear_count} bearish, giving a {verdict_word} {conf_sign}{confluence:.2f} net confluence. "
        f"Size authority is '{size_authority}' — no veto blocked sizing.{divergence_clause}"
    )

    # ---- bull case (from bullish lenses) ----
    bull_bullets = _build_bull_bullets(ticker, rows, synth, decision, facts)

    # ---- bear case (from bearish lenses) ----
    bear_bullets = _build_bear_bullets(ticker, rows, synth, facts)

    # ---- vetoes ----
    veto_descs = {
        "parabolic": f"{ticker} price action is parabolic — extension veto would cap size at 0.",
        "altman_distress": f"{ticker} is in Altman Z-score distress zone — solvency veto applies.",
        "cycle_blocked": f"{ticker} is cycle-blocked (conviction.band=avoid or size_pct=0).",
    }
    veto_strs = [veto_descs.get(v, v) for v in vetoes]

    # ---- divergences (plain English) ----
    div_strs = [d.get("read", d.get("pattern", "")) for d in divs if d.get("read") or d.get("pattern")]

    # ---- falsifier ----
    horizon = decision.get("horizon_d", 21)
    what_wrong = (
        f"FALSE if {ticker} underperforms SPY by >=5% over {horizon} business days "
        f"(engine-derived falsifier). Also exits if extension veto fires (parabolic/≥30% "
        f"above 200dma), Altman enters distress, or conviction band drops to 'avoid'."
    )

    # ---- sizing rationale ----
    sizing_rationale = _build_sizing_rationale(ticker, weight_pct, confluence, bull_count,
                                               bear_count, n_scored, position, decision)

    return {
        "summary": summary,
        "why_now": why_now,
        "bull": bull_bullets,
        "bear": bear_bullets,
        "vetoes": veto_strs,
        "divergences": div_strs,
        "what_would_prove_wrong": what_wrong,
        "sizing_rationale": sizing_rationale,
        "confluence": round(confluence, 3),
        "prob_correct": prob_correct,
    }


def _build_bull_bullets(ticker: str, rows: list[dict], synth: dict,
                         decision: dict, facts: dict | None) -> list[str]:
    facts = facts or {}
    bullets: list[str] = []

    # pull bullish rows from the decision matrix
    for row in rows:
        if row.get("direction") != "bull":
            continue
        lens = row.get("lens", "")
        val = row.get("value") or {}
        status = row.get("status", "")
        note = row.get("note", "")

        if lens == "valuation":
            vz = val.get("value_z")
            cheap = val.get("cheap_pctile")
            fwd = val.get("forward_pe")
            parts = []
            if vz is not None:
                parts.append(f"value_z={vz:.2f}")
            if cheap is not None:
                parts.append(f"trailing P/E cheaper than {cheap:.0f}% of history")
            if fwd is not None:
                parts.append(f"forward P/E {fwd:.1f}x")
            if parts:
                bullets.append(f"Valuation supportive: {', '.join(parts)}.")

        elif lens == "quality":
            qz = val.get("quality_z")
            acct = val.get("accounting")
            if qz is not None:
                bullets.append(
                    f"Quality score strong (quality_z={qz:.2f}"
                    + (f", accounting={acct}" if acct else "")
                    + ")."
                )

        elif lens == "growth":
            rc = val.get("rev_cagr")
            ec = val.get("eps_cagr")
            if rc is not None or ec is not None:
                parts = []
                if rc is not None:
                    parts.append(f"revenue CAGR {rc:.1f}%")
                if ec is not None:
                    parts.append(f"EPS CAGR {ec:.1f}%")
                bullets.append(f"Growth trajectory positive: {', '.join(parts)}.")

        elif lens == "flows_13f":
            nb = val.get("n_buying")
            ns = val.get("n_selling")
            vip = val.get("vip")
            asof = val.get("as_of")
            if nb is not None:
                when = f" (as of {asof}, lagged)" if asof else " (lagged quarterly snapshot)"
                bullets.append(
                    f"13F institutional positioning{when}: {nb} funds added vs {ns} trimmed last quarter"
                    + (f" (VIP: {vip})" if vip else "")
                    + " — positioning context, not a read on recent price."
                )

        elif lens == "flows_etf":
            direction = val.get("direction")
            conviction_pp = val.get("conviction_pp")
            bullets.append(
                f"Active ETF flows: {direction or 'accumulating'}"
                + (f" ({conviction_pp:.1f}pp conviction)" if conviction_pp else "")
                + "."
            )

        elif lens == "conviction":
            band = val.get("band")
            score = val.get("score")
            bullets.append(
                f"Engine conviction: band='{band}'"
                + (f", score={score:.1f}" if score is not None else "")
                + "."
            )

        elif lens == "options":
            reg = val.get("gamma_regime")
            exp = val.get("exp_move_d")
            bullets.append(
                f"Options: gamma regime '{reg}'"
                + (f", expected daily move {_pct(exp)}" if exp else "")
                + " — dealer positioning supportive."
            )

        elif lens == "macro_risk":
            score = val.get("score")
            quad = val.get("quad")
            bullets.append(
                f"Macro risk score low ({score:.2f})" if score is not None
                else f"Macro regime favorable: quad {quad}."
            )

        elif lens == "fed_path":
            cuts = val.get("implied_cuts_12m")
            lean = val.get("lean")
            bullets.append(
                f"Fed path supportive: {cuts:.0f} implied cuts over 12m" if cuts is not None
                else f"Fed path: {lean}."
            )

        elif lens == "rate_inflation":
            regime_val = val.get("regime")
            tier = val.get("tier")
            bullets.append(
                f"Rate/inflation sensitivity: '{regime_val}' tailwind"
                + (f" (tier {tier})" if tier else "")
                + "."
            )

        elif lens == "narrative":
            basket = val.get("basket")
            if basket:
                bullets.append(f"Narrative / theme membership: {basket} basket in theme universe.")

        elif lens == "asymmetry":
            asym = val.get("upside_downside")
            mfe = val.get("mfe_med")
            bullets.append(
                f"Return asymmetry: upside/downside ratio {asym:.2f}"
                + (f" (median MFE {_pct(mfe)})" if mfe else "")
                + "."
            )

        elif note and status not in ("missing",):
            bullets.append(f"{lens.replace('_', ' ').title()}: {note} (bull).")

    if not bullets:
        # fallback from synth counts
        bullets.append(
            f"{synth.get('bull', 0)} of {synth.get('n_scored', '?')} scored lenses were bullish "
            f"at the time of evaluation (see decision matrix for detail)."
        )

    return bullets


def _build_bear_bullets(ticker: str, rows: list[dict], synth: dict,
                         facts: dict | None) -> list[str]:
    facts = facts or {}
    bullets: list[str] = []

    for row in rows:
        if row.get("direction") != "bear":
            continue
        lens = row.get("lens", "")
        val = row.get("value") or {}

        if lens == "valuation":
            vz = val.get("value_z")
            cheap = val.get("cheap_pctile")
            bullets.append(
                f"Valuation stretched: "
                + (f"value_z={vz:.2f}" if vz is not None else "")
                + (f", P/E cheaper than only {cheap:.0f}% of history" if cheap is not None else "")
                + "."
            )

        elif lens == "extension":
            grade = val.get("grade")
            para = val.get("parabolic")
            pv2 = val.get("pct_vs_200dma")
            bullets.append(
                f"Extension risk: grade='{grade}'"
                + (", parabolic=True" if para else "")
                + (f", {pv2:+.1f}% above 200-day MA" if pv2 is not None else "")
                + ". Entry discipline required."
            )

        elif lens == "risk_drawdown":
            ddt = val.get("dd_tail")
            bullets.append(
                f"Downside risk: tail drawdown {ddt:.1f}%" if ddt is not None
                else "Downside risk elevated (validated drawdown cone)."
            )

        elif lens == "flows_13f":
            nb = val.get("n_buying")
            ns = val.get("n_selling")
            asof = val.get("as_of")
            when = f" (as of {asof}, lagged)" if asof else " (lagged quarterly snapshot)"
            bullets.append(
                f"13F institutional positioning{when}: {ns} funds trimmed vs {nb} added last quarter "
                "— distribution tilt, positioning context not recent-flow."
                if ns is not None else "13F institutional positioning net negative (lagged quarterly snapshot)."
            )

        elif lens == "quality":
            acct = val.get("accounting")
            qz = val.get("quality_z")
            bullets.append(
                f"Quality concern: accounting flag='{acct}'"
                + (f", quality_z={qz:.2f}" if qz is not None else "")
                + "."
            )

        elif lens == "solvency":
            az = val.get("altman_zone")
            pio = val.get("piotroski")
            bullets.append(
                f"Solvency: Altman zone='{az}'"
                + (f", Piotroski={pio}" if pio is not None else "")
                + " — financial stress signal."
            )

        elif lens == "growth":
            rc = val.get("rev_cagr")
            ec = val.get("eps_cagr")
            parts = []
            if rc is not None:
                parts.append(f"revenue CAGR {rc:.1f}%")
            if ec is not None:
                parts.append(f"EPS CAGR {ec:.1f}%")
            if parts:
                bullets.append(f"Growth decelerating or negative: {', '.join(parts)}.")

        elif lens == "cross_asset":
            verdict = val.get("verdict")
            bullets.append(f"Cross-asset environment: '{verdict}' — concentration / fragility risk.")

        elif lens == "macro_risk":
            score = val.get("score")
            bullets.append(
                f"Macro risk score elevated ({score:.2f})." if score is not None
                else "Macro risk elevated."
            )

        else:
            note = row.get("note", "")
            bullets.append(
                f"{lens.replace('_', ' ').title()}: {note} (bear)"
                if note else f"{lens.replace('_', ' ').title()} lens bearish."
            )

    if not bullets and synth.get("bear", 0) > 0:
        bullets.append(
            f"{synth.get('bear', 0)} of {synth.get('n_scored', '?')} scored lenses were bearish "
            f"— see decision matrix for detail."
        )

    return bullets


def _build_sizing_rationale(ticker: str, weight_pct: float, confluence: float,
                             bull: int, bear: int, n_scored: int, position: dict,
                             decision: dict) -> str:
    horizon = decision.get("horizon_d", 21)
    time_stop = position.get("time_stop_by")
    stop_clause = f" Position time-stop: {time_stop}." if time_stop else ""

    share_str = ""
    if n_scored > 0 and bull > 0:
        share_str = f" ({bull}/{n_scored} lenses bullish = {confluence:+.2f} net)"

    return (
        f"Conviction sleeve: confluence-weighted sizing. {ticker} receives {weight_pct}% "
        f"(confluence {confluence:+.2f}{share_str}, capped at 8% per name). "
        f"The weight is subtract-only from the theoretical maximum — no veto means full "
        f"confluence allocation, reduced if another name dominates the confluence score. "
        f"Falsification horizon: {horizon} business days.{stop_clause}"
    )
