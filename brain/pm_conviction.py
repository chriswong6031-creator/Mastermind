"""PM-CONVICTION — the armed deep-reasoning portfolio-manager seat.

This is the judgment half of the Flagship deep-reasoning buy layer. Where the engine
(``portfolio.conviction.build``) is a disciplined gate that only DE-escalates, this seat is a
genuine buy-side PM: it takes the engine's confirmed candidates, its rejected pool, the Macro
Strategist's confirmed themes, and the FORGE research summaries, then ANTICIPATES and CHAMPIONS
confirmed-leadership thematic names with conviction — the same posture as the proven autonomous
desk, but constrained to Flagship discipline.

It reuses the PROVEN autonomous tool surface (``brain/autonomous_mcp.py``): identical
regime/themes/baskets/divergences/decision-matrix/quote tools and the ``submit_book`` flow, so the
PM gets the full thematic read. The PM may ADD high-conviction thematic names the engine missed and
DROP engine names that lack a live thesis (real autonomy) — but every name it champions is then run
through the existing blind SENTINEL adversary + a NEXUS-style subtract-only cap pass (orchestrated
by the caller in ``brain/judgment_book.py``; this module exposes the helpers and the no-leverage /
concentration enforcement so the PM's reshaped book is checked, never blindly trusted).

Additive + reversible: gated behind ``MASTERMIND_FLAGSHIP_JUDGMENT`` (default OFF). Returns a book
with ``ran=False`` (or ``None``) on any failure; never raises. Submits to a Flagship-scoped path
(``portfolio_id="flagship_judgment"``) so it can NEVER collide with the live autonomous book.
Model-agnostic via ``role="deep"`` (Opus/Fable resolved by config/agents.yml).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from brain import client

PORTFOLIO_ID = "flagship_judgment"


def enabled() -> bool:
    """The PM seat runs only when the Flagship judgment layer is armed AND an LLM is reachable."""
    flag = os.environ.get("MASTERMIND_FLAGSHIP_JUDGMENT", "0").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    try:
        return client.available()
    except Exception:  # noqa: BLE001
        return False


# The Self-Directed defensive book — the EXPLICIT named benchmark the PM must beat in risk-off
# regimes (W4 prompt edit 3b). This is the SAME four-ETF book the re-audit found beating every Brain
# (+2.92%, zero drawdown, over the 07-01 window). Naming it turns "hold cash and do nothing" from an
# unpriceable non-decision into a concrete, beatable bogey.
_DEFENSIVE_BENCHMARK_BASKET: tuple[str, ...] = ("XLU", "XLV", "XLF", "XLP")


def _read_market_view() -> dict | None:
    """Read data/market_view/latest.json, returning None on any miss/error.

    E1.1 — the view artifact is the single source of truth for the perception enrichment the
    seats receive.  Lazy: returns None while the organ is unbuilt (W-E.0 is a prior wave;
    graceful degrade is the invariant).  Never raises.
    """
    try:
        p = Path(__file__).resolve().parent.parent / "data" / "market_view" / "latest.json"
        if p.exists():
            d = json.loads(p.read_text())
            return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        pass
    return None


def _market_view_enrichment(view: dict | None) -> dict:
    """Extract the perception enrichment the seats need from the market_view artifact.

    E1.1 — compose the view's brief + PlaneRecord summaries + label_vs_planes line into a
    compact, prompt-ready sub-dict.  Returns {} when the view is absent (degrade silently;
    the seats already render W4's enriched frame read — this is ADDITIVE).

    Returned keys:
      market_view_brief     — the deterministic brief dict {what_changed, whats_rotating,
                              wheres_the_risk, posture_implication}
      plane_summaries       — list of {name, direction, status, reading} for every
                              directional plane (advisory + validated, non-absent), compact
                              for prompt injection (no raw/freshness bulk)
      label_vs_planes_line  — a human-readable sentence for the prompt ("3 validated planes
                              dissent from the risk_on label — no semis seed" pattern)
    """
    if not isinstance(view, dict):
        return {}
    brief = view.get("brief")
    lvp = view.get("label_vs_planes") or {}
    planes = view.get("planes") or {}

    # compact plane summaries — include every plane with a determinable direction
    summaries = []
    for name, rec in planes.items():
        if not isinstance(rec, dict):
            continue
        d = rec.get("direction")
        if d is None:
            continue
        summaries.append({
            "name": name,
            "direction": d,
            "status": rec.get("status"),
            "reading": str(rec.get("reading") or "")[:120],
        })

    # label_vs_planes human-readable line
    conflict = lvp.get("conflict")
    dissenting = lvp.get("dissenting_planes") or []
    n_dissent = len(dissenting)
    label_dir = lvp.get("label_direction")
    consensus = lvp.get("plane_consensus_direction")
    if conflict and n_dissent:
        label_vs_planes_line = (
            f"{n_dissent} validated plane{'s' if n_dissent != 1 else ''} dissent "
            f"from the {label_dir} label (consensus: {consensus}) — "
            f"{', '.join(dissenting)}"
        )
    elif conflict:
        label_vs_planes_line = (
            f"label-vs-planes conflict: label {label_dir} vs plane consensus {consensus}"
        )
    else:
        label_vs_planes_line = "Label and validated planes agree — no conflict."

    out: dict = {}
    if isinstance(brief, dict) and any(brief.values()):
        out["market_view_brief"] = {k: str(v or "")[:400]
                                    for k, v in brief.items() if v is not None}
    if summaries:
        out["plane_summaries"] = summaries
    out["label_vs_planes_line"] = label_vs_planes_line
    return out


def _full_regime_slice(regime: dict | None) -> dict:
    """The FULL regime read the seats receive (W4 prompt edit 3a + E1.1 view enrichment).

    Upgrades from the confidence-blind 3-field lens_row to the enriched regime_frame.frame()
    (confidence / transition_state / contradicting / flip_margin / flag_confidence_decay) PLUS the
    dwell risk-state. Best-effort layering: start from whatever `regime` dict the caller passed
    (keeps today's fields), overlay frame() where it has richer values, then attach the cycles()
    summary + the dwell risk state. Any failure degrades to the plain 3-field slice — the invariant
    holds (missing data COARSENS the read, never inflates it).

    E1.1 (W-E.1): EXTENDS (does not duplicate) with the market_view brief + PlaneRecord summaries
    + label_vs_planes line from data/market_view/latest.json.  Lazy — missing view degrades to
    current W4 behavior; the organ was not yet built when the W4 reads were established.
    """
    reg = {k: (regime or {}).get(k) for k in
           ("quad", "quad_name", "growth_score", "inflation_score", "liquidity_overlay",
            "cycle_tag", "sector_rs_top")}
    try:
        from brain import regime_frame as _rf
        fr = _rf.frame("us") or {}
        for k in ("confidence", "transition_state", "contradicting", "flip_margin",
                  "flag_confidence_decay"):
            if fr.get(k) is not None:
                reg[k] = fr[k]
    except Exception:  # noqa: BLE001 — degrade to the 3-field slice
        pass
    # cycles() summary — a compact per-sector entry/late-cycle read (the sole freshness-gated reader;
    # stale/absent → {} → this key is simply omitted, never a fabricated cycle read).
    try:
        from brain import regime_frame as _rf
        _cyc = _rf.cycles() or {}
        entry = sorted(t for t, r in _cyc.items() if isinstance(r, dict) and r.get("entry_favored"))
        late = sorted(t for t, r in _cyc.items() if isinstance(r, dict) and r.get("late_cycle"))
        if entry or late:
            reg["cycles"] = {"entry_favored": entry, "late_cycle": late}
    except Exception:  # noqa: BLE001
        pass
    # dwell risk state — the persistent memory read (state/fragility/gross_cap/allow_adds). Best-effort;
    # a caller-supplied macro_risk in portfolio_ctx is preferred by _pm_input, this is the fallback.
    # READ-ONLY: we pass a no-op state_saver so this display-read never DOUBLE-ADVANCES the dwell state
    # machine (the authoritative risk_state call happens once per build in phase2 / the macro-risk block).
    try:
        from brain import macro_risk as _mr
        rs = _mr.risk_state(str(regime.get("date") if isinstance(regime, dict) else "")[:10]
                            or None, regime, state_saver=lambda rec: None)
        reg["risk_state"] = {k: rs.get(k) for k in ("state", "fragility", "gross_cap", "allow_adds")}
    except Exception:  # noqa: BLE001
        pass
    # E1.1 — market_view enrichment: brief + PlaneRecord summaries + label_vs_planes line.
    # Lazy: missing/unbuilt view degrades to {} so the W4 slice is byte-identical in that case.
    try:
        mv = _read_market_view()
        enrichment = _market_view_enrichment(mv)
        if enrichment:
            reg["market_view"] = enrichment
    except Exception:  # noqa: BLE001 — additive; never break the seat
        pass
    return reg


# ─────────────────────────────────────────────────────────────────────────────
# input — everything the PM sees, seeded with the engine candidates, the rejected
# thematic pool it may champion, the Strategist themes, and the FORGE summaries.
# W4 B1: leadership legs (sleeve-tagged) + defensive candidates are now piped in too,
# and the engine's proposed weights are DE-ANCHORED to the end of the payload.
# ─────────────────────────────────────────────────────────────────────────────
def _pm_input(sized: list[dict], rejected: list[dict], strategist: dict | None,
              regime: dict | None, gate_info: dict | None, asof: str, *,
              leadership: list[dict] | None = None,
              defensive: list[dict] | None = None,
              risk_state: dict | None = None) -> dict:
    sized = sized or []
    rejected = rejected or []
    gate_info = gate_info or {}
    leadership = leadership or []
    defensive = defensive or []

    engine_candidates = [{
        "ticker": c.get("ticker"),
        "confluence": c.get("confluence"),
        "bull": str(c.get("bull") or "")[:240],
        "bear": str(c.get("bear") or "")[:240],
        "divergences": (c.get("divergences") or [])[:6],
        "retained": bool(c.get("retained")),
        "sleeve": c.get("sleeve") or "conviction",
    } for c in sized if c.get("ticker")][:40]

    # LEADERSHIP legs (W4 B1 leadership pipe) — the 40-60% NAV sleeve the PM could NOT see before.
    # sleeve-tagged so the PM (and the deterministic authority clamp) know these are equal-weight
    # leadership legs it may DROP (→ cash/defensive) but MUST NOT re-weight while surviving.
    leadership_legs = [{
        "ticker": c.get("ticker"),
        "sleeve": "leadership",
        "rs_pctile": c.get("rs_pctile"),
    } for c in leadership if c.get("ticker")][:20]

    # DEFENSIVE candidates (the ONE canonical generator) — the champion pool for a risk-off rotate.
    defensive_candidates = [{
        "ticker": d.get("ticker"),
        "archetype": d.get("archetype"),
        "source": d.get("source"),
        "note": str(d.get("note") or "")[:120],
    } for d in defensive if isinstance(d, dict) and d.get("ticker")][:30]

    engine_rejected = [{
        "ticker": r.get("ticker"),
        "confluence": r.get("confluence"),
        "vetoes": (r.get("vetoes") or [])[:4],
        "bear": (r.get("bear") or [])[:3],
    } for r in rejected if r.get("ticker")][:40]

    forge_summaries = []
    for t, info in gate_info.items():
        block = (info or {}).get("research_block") or {}
        if not block:
            continue
        forge_summaries.append({
            "ticker": t,
            "combined": block.get("combined"),
            "viability": block.get("viability"),
            "confirmed": block.get("confirmed"),
            "summary": str(block.get("summary") or "")[:280],
        })
    forge_summaries = forge_summaries[:40]

    reg = _full_regime_slice(regime)
    # prefer a caller-supplied (already-computed) dwell risk state over the fallback in _full_regime_slice
    if isinstance(risk_state, dict) and risk_state:
        reg["risk_state"] = {k: risk_state.get(k) for k in
                             ("state", "fragility", "gross_cap", "allow_adds")}

    # DE-ANCHOR (W4 prompt edit 3c): the engine's proposed WEIGHTS are moved OUT of the candidate
    # rows above (which now carry no weight) into this separate, clearly-labelled block placed LAST
    # in the payload — the engine's view, not a target. The PM sizes from conviction, not by anchoring
    # on the engine's number.
    engine_proposed_weights = {c.get("ticker"): c.get("weight")
                               for c in sized if c.get("ticker")}
    for c in leadership:
        if c.get("ticker"):
            engine_proposed_weights.setdefault(c["ticker"], c.get("weight"))

    # E2.5 — posture line in the payload: the shadow posture artifact (read-only, additive).
    # The PM sees the shadow posture in the structured payload so it can reference the context.
    # Missing/absent artifact → key omitted (degrade silently).
    posture_line: dict | None = None
    try:
        from brain import posture_decider as _pd
        _art = _pd.latest()
        if isinstance(_art, dict) and _art.get("posture_class"):
            posture_line = {
                "posture_class": _art.get("posture_class"),
                "offense_budget": _art.get("offense_budget"),
                "defense_floor": _art.get("defense_floor"),
                "shadow": _art.get("shadow", True),
                "why": _art.get("why"),
            }
    except Exception:  # noqa: BLE001 — additive; never break the seat
        pass

    payload: dict = {
        "asof": str(asof)[:10],
        "regime": reg,
        "defensive_benchmark": list(_DEFENSIVE_BENCHMARK_BASKET),
        "strategist": strategist or {},
        "engine_candidates": engine_candidates,
        "leadership_legs": leadership_legs,
        "defensive_candidates": defensive_candidates,
        "engine_rejected": engine_rejected,
        "forge_summaries": forge_summaries,
        # DE-ANCHORED — LAST in the payload, after every candidate/defensive/regime section.
        "engine_proposed_weights_ADVISORY": engine_proposed_weights,
    }
    if posture_line is not None:
        payload["posture_ADVISORY"] = posture_line
    return payload


_PM_PERSONA = (
    "You are the PORTFOLIO MANAGER of the FLAGSHIP $1,000,000 paper book. Like the autonomous desk "
    "you ANTICIPATE and CHAMPION confirmed-leadership thematic names with conviction — concentrate "
    "where the leadership narrative has ALREADY turned (let the dashboard's confirmed themes and the "
    "Macro Strategist read tell you WHICH themes those are today — do not assume any in advance), and "
    "sidestep crowded late themes. \n\n"
    "EXPRESS EACH THEME THROUGH SINGLE NAMES, NOT ETFs. Prefer the highest-conviction individual "
    "equities that capture a confirmed theme; broad sector/index/factor ETFs (e.g. SMH, XLK, MTUM, "
    "IWM) are a LAST-RESORT placeholder only when no single name cleanly expresses the theme — they "
    "must never be the core of the book. Single-name selection is where the alpha is. \n\n"
    "IDLE CASH EARNS ~4% ANNUALIZED (a money-market sweep), so holding cash when you lack "
    "high-conviction ideas is a REWARDED choice, not dead money. NEVER dilute the book with marginal "
    "names just to stay fully invested — a smaller book of your best ideas plus paid cash beats a "
    "padded one. \n\n"
    "You are SEEDED with: the engine's confirmed candidates, its rejected pool (names you MAY "
    "champion if they have a live thesis), the Macro Strategist's confirmed themes + backdrop "
    "stance, and FORGE research summaries. You have FULL discretion within paper cash (NO leverage): "
    "you MAY add high-conviction thematic names the engine missed and DROP engine names that lack a "
    "live thesis. \n\n"
    "Confirmation over prediction — every name needs a why-now, not a prophecy. Use the mcp__bot__* "
    "tools (regime, themes, decision matrix, divergences, intel hub, fundamentals) and "
    "mcp__bot__get_quote to confirm a name is priceable before you rely on it, and the open web if "
    "useful. When done, submit your COMPLETE Flagship target book via mcp__desk__submit_book ONCE — "
    "every name you want to hold, its weight (fraction of NAV), and a one-paragraph rationale per "
    "holding (why you own it, now). Anything you omit is SOLD. Your champions are then checked by a "
    "BLIND adversary and HARD risk caps (no-leverage, per-name + concentration limits) — size with "
    "that in mind; conviction leads, the adversary checks. \n\n"
    "THE LEADERSHIP SLEEVE (leadership_legs in the payload) is the engine's equal-weight top-RS "
    "rotation book — 40-60% of NAV. You have exactly TWO powers over a leadership leg: KEEP it "
    "(then it stays at the engine's equal weight — you may NOT up-weight or down-weight a leg you "
    "keep; equal-weight is validated doctrine, rank-IC≈0) or DROP it (omit it — the freed budget "
    "goes to CASH or to a DEFENSIVE candidate, never to topping-up your conviction names). A leg you "
    "re-weight but keep will be restored to the engine weight by a deterministic clamp, so do not "
    "bother — DROP or KEEP. \n\n"
    "YOU MUST BEAT A STATIC DEFENSIVE BOOK. In risk-off / weakening regimes your book must beat "
    "max(SPY, the defensive_benchmark basket XLU/XLV/XLF/XLP) — that static defensive book is "
    "currently beating every Brain, so holding leadership into a weakening tape while it bleeds is "
    "a LOSS even if you beat SPY. The defensive_candidates pool is your rotation ammunition: DROP a "
    "leadership leg and rotate its budget into a defensive name when the regime turns. \n\n"
    "THREE QUESTIONS (required in your submit): beyond the book, answer own_more (names you'd size "
    "UP if you could), own_less (names you're trimming/exiting and why), and — most important — "
    "not_holding_should (names you do NOT hold but the regime says you SHOULD be rotating into: for "
    "each give ticker, why_now, a probability 0-1, and check_by date). These rotation CALLS are "
    "graded 21 trading days forward even when you place no trade, so a defensive rotation you name "
    "but don't buy still earns (or costs) you credit. Name them honestly."
)


def _build_prompt(payload: dict, directive: str | None = None) -> str:
    strat = payload.get("strategist") or {}
    reg = payload.get("regime") or {}
    lines = [f"# Flagship book — deep-reasoning decision for {payload.get('asof')}", ""]
    if directive:
        lines += ["## ⚠ PRIORITY DIRECTIVE FOR THIS RUN", directive.strip(), ""]
    if reg.get("quad_name") or reg.get("quad"):
        lines += [f"Macro regime (in-house read): quad {reg.get('quad')} "
                  f"({reg.get('quad_name')}), liquidity {reg.get('liquidity_overlay')}, "
                  f"cycle {reg.get('cycle_tag')}.",
                  # W4 3a — the confidence-blind funnel is replaced by the full frame:
                  f"Regime confidence {reg.get('confidence')}, transition {reg.get('transition_state')}, "
                  f"flip_margin {reg.get('flip_margin')}"
                  + (f", contradicting: {reg.get('contradicting')}" if reg.get('contradicting') else "")
                  + ".",
                  ""]
    _rs = reg.get("risk_state") or {}
    if _rs.get("state"):
        lines += [f"Macro Risk Officer (dwell) state: {str(_rs.get('state')).upper()} "
                  f"(fragility {_rs.get('fragility')}; gross cap {_rs.get('gross_cap')}; "
                  f"adds {'BLOCKED' if _rs.get('allow_adds') is False else 'allowed'}).", ""]
    # E1.1 — market_view perception enrichment (ADDITIVE; absent view degrades silently).
    # The ad-hoc W4 regime slices above are NOT removed — this section EXTENDS them with the
    # validated-plane disagreement layer from the E0.3 organ.  Degrade = section omitted.
    _mv = reg.get("market_view") or {}
    if _mv.get("label_vs_planes_line"):
        lines += ["## Perception layer (market_view — validated-plane read)"]
        lines += [f"Plane disagreement: {_mv['label_vs_planes_line']}"]
        _mvb = _mv.get("market_view_brief") or {}
        if _mvb.get("wheres_the_risk"):
            lines += [f"Where the risk is: {_mvb['wheres_the_risk']}"]
        if _mvb.get("whats_rotating"):
            lines += [f"Rotating: {_mvb['whats_rotating']}"]
        if _mvb.get("posture_implication"):
            lines += [f"Posture implication: {_mvb['posture_implication']}"]
        _ps = _mv.get("plane_summaries") or []
        if _ps:
            risk_off_planes = [p for p in _ps if p.get("direction") == "risk_off"]
            if risk_off_planes:
                lines += ["Risk-off planes: " + "; ".join(
                    f"{p['name']}({'V' if p.get('status') == 'validated' else 'A'}): {p['reading']}"
                    for p in risk_off_planes[:6]
                )]
        lines += [""]
    # E2.5 — POSTURE block (flag-independent read-only prompt enrichment).
    # The Flagship PM sees the shadow posture so it can observe whether it would have agreed.
    # Missing/absent artifact → section omitted (degrade silently; no block added).
    try:
        from brain import posture_decider as _pd
        _posture_block = _pd.render_directive()
        if _posture_block:
            lines += [_posture_block]
    except Exception:  # noqa: BLE001 — additive; never break the seat
        pass
    # W-NW.1 — NEURAL WEB CONTEXT block (flag-gated; dark-ship §1.7).
    # Appended as bounded TEXT (≤1200 chars) after E2.5 — NO new top-level payload key
    # (payload JSON is hard-truncated at 9000 chars; test at test_pm_conviction.py:141
    # asserts engine_proposed_weights_ADVISORY is last key — this section is text, not a key).
    # Cortex prose is structurally excluded by seat_prompt_block().
    try:
        from brain.neural_web_context import seat_prompt_block as _nw_seat, nw_prompts_enabled as _nw_flag
        if _nw_flag():
            _cand_tickers = [r.get("ticker") for r in (payload.get("engine_candidates") or [])
                             if isinstance(r, dict) and r.get("ticker")]
            _nw_text = _nw_seat(_cand_tickers, max_chars=1200)
            if _nw_text:
                lines += [
                    "## NEURAL WEB CONTEXT (context-only, not validated for sizing)",
                    _nw_text,
                    "",
                ]
    except Exception:  # noqa: BLE001 — additive; never break the seat
        pass
    lines += ["BENCHMARK YOU MUST BEAT (risk-off / weakening regimes): "
              f"max(SPY, the defensive basket {', '.join(payload.get('defensive_benchmark') or [])}). "
              "That static defensive book is currently beating every Brain — a book that holds "
              "leadership into a weakening tape and merely beats SPY still LOSES to it.", ""]
    if strat.get("confirmed_themes"):
        lines += ["Macro Strategist — CONFIRMED leadership themes right now:"]
        for th in strat["confirmed_themes"][:12]:
            nm = ", ".join(th.get("names", [])[:8])
            lines.append(f"  • {th.get('theme')} [{th.get('stage')}, lead "
                         f"{th.get('leadership')}] — {nm} — {th.get('why')}")
        lines += [f"Backdrop stance: {strat.get('backdrop_stance')} "
                  f"(supportive={strat.get('supportive')}). "
                  f"Crowding flags: {', '.join(strat.get('crowding_flags', []) or ['none'])}.", ""]
    lines += [
        "You are seeded below (JSON) with: the engine's confirmed candidate NAMES (no weights — you "
        "size from conviction), its LEADERSHIP legs (you may KEEP-at-engine-weight or DROP each, "
        "never re-weight a survivor), the DEFENSIVE candidate pool (your risk-off rotation ammo), "
        "the rejected pool (names you may champion), the full Strategist read, and FORGE research "
        "summaries. The engine's OWN proposed weights are at the very END of the JSON, labelled "
        "ADVISORY — they are the engine's view, NOT a target; do not anchor on them. Research with "
        "the in-house tools and/or the web, then submit your COMPLETE Flagship target book via "
        "mcp__desk__submit_book — plus the three required rotation-call fields (own_more / own_less "
        "/ not_holding_should).",
        "",
        "```json",
        json.dumps(payload, indent=2, default=str)[:9000],
        "```",
    ]
    return "\n".join(lines)


def build_book(sized: list[dict], rejected: list[dict], *, regime: dict | None, asof: str,
               strategist: dict | None, gate_info: dict | None,
               portfolio_ctx: dict | None = None,
               directive: str | None = None,
               leadership: list[dict] | None = None,
               defensive: list[dict] | None = None) -> dict | None:
    """Run the armed Opus PM. Returns the target book
    ``{holdings:[{ticker,weight,thesis,conviction,sleeve}], cash, summary, sold_note, ran,
    own_more, own_less, not_holding_should}`` — or a stub with ``ran=False`` when no LLM is
    reachable / the PM did not submit. Additive; never raises.

    W4 B1: ``leadership`` (the engine's sleeve-tagged leadership legs) and ``defensive`` (the
    canonical defensive-candidate pool) are piped into the payload so the PM can DROP a leadership
    leg and rotate its budget into a defensive name — it could see neither before. The three
    rotation-call fields (own_more / own_less / not_holding_should) are read back from the
    submission and passed through for the judgment book's Brier-graded shadow entries.

    The PM submits via ``mcp__desk__submit_book``; the submission is read back from the
    Flagship-scoped path (``portfolio_id="flagship_judgment"``) so it never collides with the live
    autonomous book."""
    stub = {"holdings": [], "cash": 1.0, "summary": "", "sold_note": "", "ran": False,
            "own_more": [], "own_less": [], "not_holding_should": []}
    if not client.available():
        return stub
    try:
        # FLAGSHIP-scoped desk (aliased): get_my_book shows the real Flagship book and submit_book
        # writes to the isolated "flagship_judgment" path this function reads back. Reusing the
        # autonomous desk here was a bug (live dry-run): its submit_book wrote to the AUTONOMOUS
        # book, so build_book always read nothing → ran=False → P1 silently no-op'd in production.
        from brain import flagship_desk_mcp as autonomous_mcp, cli_bridge
    except Exception:  # noqa: BLE001
        return stub

    _rs = (portfolio_ctx or {}).get("macro_risk") if isinstance(portfolio_ctx, dict) else None
    payload = _pm_input(sized, rejected, strategist, regime, gate_info, asof,
                        leadership=leadership, defensive=defensive, risk_state=_rs)
    prompt = _build_prompt(payload, directive=directive)

    # self-mirror: append the PM's own champion track record to its persona (flag-gated; OFF →
    # the persona is byte-identical to P1/P2).
    try:
        from datetime import date as _date
        from brain import self_mirror
        try:
            _asof = _date.fromisoformat(str(asof)[:10])
        except Exception:  # noqa: BLE001
            _asof = None
        persona = self_mirror.inject(_PM_PERSONA, "pm", _asof)
    except Exception:  # noqa: BLE001 — self-mirror is additive; never break the seat
        persona = _PM_PERSONA

    # clear any stale Flagship-scoped submission first so a no-submit turn can't replay yesterday
    try:
        autonomous_mcp.clear_submission(PORTFOLIO_ID)
    except Exception:  # noqa: BLE001
        pass

    try:
        coro = cli_bridge.reason(
            prompt,
            role="deep",
            arm=True,
            append_system=persona,
            mcp_servers=autonomous_mcp.build_servers(),
            allowed_tools=autonomous_mcp.allowed_tools(),
            max_turns=int(os.environ.get("FLAGSHIP_PM_MAX_TURNS", "30")),
        )
        # cli_bridge.reason is a coroutine; run it on a fresh loop (or a thread if one is live).
        _res = _run_coro(coro)
        # record the armed PM seat's known cost against the nightly per-book ledger under
        # "flagship" (the seat builds the Flagship judgment book). No-op when cost is unknown
        # or the cap is OFF; never raises.
        try:
            from brain import cost_guard
            cost_guard.record("flagship", (_res or {}).get("cost_usd"), asof)
        except Exception:  # noqa: BLE001 — additive; never break the build
            pass
    except Exception:  # noqa: BLE001 — the seat is additive; never break the build
        return stub

    sub = None
    try:
        sub = autonomous_mcp.read_submission(PORTFOLIO_ID)
    except Exception:  # noqa: BLE001
        sub = None
    if not sub:
        return stub

    # PACKET GATE (ruling R6, Charter P2/P3/P8) — at the PM submission boundary.
    # Prior book for the flagship layer is the engine conviction rows (sized).
    _pgr_pm = None
    try:
        from control_plane.packet_gate import process as _packet_process_pm
        _prior_flagship = {"holdings": [{"ticker": c.get("ticker"), "weight": c.get("weight")}
                                         for c in (sized or []) if c.get("ticker")]}
        _pgr_pm = _packet_process_pm(
            PORTFOLIO_ID, sub, _prior_flagship,
            extras={
                "run_id": "",
                "asof": str(asof or ""),
                "mandate": (sub.get("mandate") or "Build the Flagship conviction book."),
                "evidence_planes": sub.get("evidence_planes") or [],
                "source_provenance": sub.get("source_provenance") or [],
                "falsifiers": sub.get("falsifiers") or [],
                "liquidity_notes": sub.get("liquidity_notes") or "<not provided>",
                "expected_failure_mode": sub.get("expected_failure_mode") or "<not provided>",
            },
        )
        if _pgr_pm is not None and not _pgr_pm.ok:
            # enforce mode + invalid packet → degrade to engine path (same as PM no-submit)
            return stub
    except Exception:  # noqa: BLE001 — never-raise; gate failure never blocks the build
        pass

    # the leadership tickers the engine passed in — a KEPT leg with one of these tickers is tagged
    # sleeve='leadership' on the way out so the judgment book's authority clamp can enforce the
    # equal-weight (never re-weight a survivor) rule deterministically.
    _lead_tickers = {str(c.get("ticker") or "").upper().strip()
                     for c in (leadership or []) if c.get("ticker")}

    holdings = []
    gross = 0.0
    seen: set[str] = set()
    for h in (sub.get("holdings") or []):
        t = str(h.get("ticker") or "").upper().strip()
        try:
            w = float(h.get("weight") or 0.0)
        except (TypeError, ValueError):
            w = 0.0
        thesis = str(h.get("rationale") or h.get("thesis") or "").strip()
        if not t or t in seen or w <= 0 or not thesis:
            continue
        seen.add(t)
        holdings.append({"ticker": t, "weight": w, "thesis": thesis,
                         "conviction": (h.get("conviction") or "medium"),
                         "sleeve": ("leadership" if t in _lead_tickers else "conviction")})
        gross += w

    # no-leverage scale-down (defence-in-depth; submit_book already enforces this).
    if gross > 1.0 and holdings:
        scale = 1.0 / gross
        for h in holdings:
            h["weight"] = round(h["weight"] * scale, 6)
        gross = 1.0

    # publish the PM's realised de-confidence multiplier (shrink-only; 1.0 until graded → no change
    # OFF). Read by the caller/CIO; the seat never auto-acts on it.
    try:
        from brain import calibration as _calib
        pm_mult = round(_calib.multiplier("pm"), 3)
    except Exception:  # noqa: BLE001
        pm_mult = 1.0

    return {
        "holdings": holdings,
        "cash": round(max(0.0, 1.0 - gross), 4),
        "summary": str(sub.get("summary") or "").strip()[:2000],
        "sold_note": str(sub.get("sold_note") or "").strip()[:1000],
        "ran": bool(holdings),
        "calibration_multiplier": pm_mult,
        # THREE-QUESTIONS rotation calls (W4 B1). Normalised + defensively coerced; missing → [].
        # The judgment book turns each not_holding_should entry into a shadow entry + 21d falsifier,
        # so a defensive rotation the PM NAMES but doesn't trade is still Brier-graded.
        "own_more": _norm_calls(sub.get("own_more")),
        "own_less": _norm_calls(sub.get("own_less")),
        "not_holding_should": _norm_calls(sub.get("not_holding_should")),
        # W-L / L2 — the PM's JOURNAL DUTY completions, passed through verbatim (the judgment book
        # records them via brain.journal.complete). Missing → []; an incomplete lesson is accepted +
        # logged 'journal_incomplete', never rejected (add-only, mirrors three_questions_incomplete).
        "journal_lessons": list(sub.get("journal_lessons") or [])[:40],
    }


def _norm_calls(rows) -> list[dict]:
    """Normalise a three-questions field to a list of {ticker, why_now, probability, check_by}.
    Defensive: accepts a list of dicts (or bare ticker strings); drops rows with no ticker; coerces
    probability into [0,1] (None if absent/bad). Never raises → [] on any failure."""
    out: list[dict] = []
    try:
        for r in (rows or []):
            if isinstance(r, str):
                r = {"ticker": r}
            if not isinstance(r, dict):
                continue
            t = str(r.get("ticker") or "").upper().strip()
            if not t:
                continue
            try:
                p = float(r.get("probability")) if r.get("probability") is not None else None
                if p is not None:
                    p = max(0.0, min(1.0, p))
            except (TypeError, ValueError):
                p = None
            out.append({
                "ticker": t,
                "why_now": str(r.get("why_now") or r.get("why") or "")[:300],
                "probability": p,
                "check_by": str(r.get("check_by") or "")[:10] or None,
            })
    except Exception:  # noqa: BLE001
        return []
    return out[:20]


def _run_coro(coro):
    """Run an async coroutine from a sync caller — directly if no loop is running, else on a
    worker thread (mirrors ``bot/autonomous._run_coro``)."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # a loop is already running — dispatch to a fresh loop on a worker thread
    import concurrent.futures

    def _runner():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(_runner).result()


# ─────────────────────────────────────────────────────────────────────────────
# discipline — the PM's champions are checked by the existing blind SENTINEL +
# a NEXUS-style subtract-only / no-leverage / concentration pass. Exposed here so
# the orchestrator (brain/judgment_book.py) can reuse it; this seat never trusts
# the PM blindly. A truly novel PM-added name (no engine decision matrix) gets a
# synthetic breakdown={"confirmed": True} so NEXUS treats it as a confirmed buy
# SENTINEL can still veto — "judgment leads, adversary checks".
# ─────────────────────────────────────────────────────────────────────────────
def adversary_check(ticker: str, asof: str, *, engine_full: dict | None, breakdown: dict | None,
                    regime: dict | None, portfolio_ctx: dict | None) -> dict:
    """Run one PM holding through the existing blind SENTINEL + subtract-only NEXUS pass via
    ``committee.assess``. Returns the committee decision (action/scale/lean/rationale/sentinel).
    PM-added names with no engine matrix get a synthetic confirmed breakdown so SENTINEL can still
    oppose them. Never raises — degrades to a pass-through confirm on any failure."""
    from brain import committee
    bd = breakdown or {"confirmed": True}
    try:
        return committee.assess(ticker, asof, engine_full=(engine_full or {}), breakdown=bd,
                                regime=regime or {}, portfolio_ctx=portfolio_ctx or {})
    except Exception:  # noqa: BLE001 — additive; never break the build
        return {"action": "confirm", "scale": 1.0, "lean": "add",
                "rationale": "committee unavailable — PM decision stands.",
                "sentinel_stance": None, "sentinel": None, "artifacts": None, "ran": False}


def enforce_no_leverage(holdings: list[dict], *, name_cap: float = 0.08) -> list[dict]:
    """Final NEXUS-style cap pass on the PM's reshaped book (subtract-only, in place semantics on a
    copy): clamp each weight to ``name_cap`` then scale the whole book down so gross ≤ 1.0. Reuses
    the same no-leverage discipline as ``submit_book`` / the conviction sleeve. Never raises."""
    out = []
    for h in (holdings or []):
        try:
            w = min(float(name_cap), float(h.get("weight") or 0.0))
        except (TypeError, ValueError):
            w = 0.0
        if w <= 0:
            continue
        out.append({**h, "weight": round(w, 4)})
    gross = sum(h["weight"] for h in out)
    if gross > 1.0 and out:
        scale = 1.0 / gross
        for h in out:
            h["weight"] = round(h["weight"] * scale, 4)
    return out
