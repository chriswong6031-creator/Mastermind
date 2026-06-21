"""The research desk — the marriage of Claude's armed research to the doctrine gates.

run_daily_research(): runs an ARMED Claude session (reads the dashboard, searches the
web/news, reasons 2nd/3rd-order, writes proposals back via the MCP action tools).

ingest_proposals(): turns Claude's free-form proposals into first-class, gated objects —
each becomes a falsifiable brain_decision.v1 whose falsifier the ENGINE derives, clamped
by the risk officer (Claude can't escalate a blocked name), appended to the same ledger +
Brier scorer as the deterministic brain. Sizing still happens downstream via the
confluence scorecard — Claude proposes the hypothesis; it never pushes size. Paper-only.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import bot  # noqa: F401

from brain import cli_bridge, ledger
from brain.decision import DecisionDoc

_ROOT = Path(__file__).resolve().parent.parent
_PROPOSALS = _ROOT / "data" / "brain" / "proposals.jsonl"
_BULLISH = {"add", "overweight", "accumulate", "constructive", "buy"}

RESEARCH_PROMPT = """You are the Brain — the decision-making reasoning layer for the Mastermind, an
autonomous, paper-only narrative-investing bot. Today is {asof}; the macro regime is {quad} ({quad_name}).

WORK IN TWO STAGES.

STAGE 1 — SALIENCE (triage, don't research yet). Call get_daily_briefing FIRST: it gives you the macro
frame (regime, cycle, liquidity, Fed stance, posture, catalysts) AND a ranked priority_queue plus the
divergences list (names where the tape and smart-money DISAGREE). Then call get_intake_candidates to see
the full deduped candidate queue with provenance — which independent engines flagged each name (briefing /
radar / alt-data / buy-board / news-surge / open-thesis) and why. Corroboration across independent engines
is the strongest signal. Pick a SHORT list (≈3-6) to research in depth: prioritise the DIVERGENCES and the
high-confidence, multi-engine names. Skip names where everything already agrees and the move is spent.

STAGE 2 — DEPTH (research the shortlist). For each chosen name call get_ticker_package (one call: the
intelligence facets + lens divergences + intake provenance), plus get_quote for a live price check and
WebSearch / WebFetch for recent news, events, filings, and narratives. The remaining tools — get_themes /
get_standouts / get_portfolio / get_altdata / get_news / read_signal — are there when you need to go deeper.

Reason through SECOND and THIRD-order effects — supply-chain flow, shortages/overages, earnings and
guidance, accounting events, institutional news and flow. Hunt for: emerging themes, themes rolling
over, and asymmetric single-name edges that others haven't connected yet.

APPROACH FROM ALL SIDES — before any verdict on a name or theme, call get_decision_matrix(subject).
You MUST address every lens with status in {validated, context, partial} — valuation, quality, growth,
narrative/leadership, asymmetry, risk (drawdown cone + extension), administration/policy tilt, Fed path,
institutional flows (13F + ETF), options positioning, rate sensitivity, cross-asset, conviction — and
say whether it agrees or disagrees. NEVER form a thesis from one side. Call get_divergences and rule on
each divergence: where the lenses disagree is the EDGE (e.g. cheap + flows-in + policy-tailwind before
price moves) or the TRAP (e.g. hot + expensive + smart-money exiting = distribution). Validated lenses
(drawdown cone, extension veto) hold authority; a hard veto (parabolic / Altman distress / cycle-blocked)
caps size at 0 no matter how bullish the rest. Confluence sets size; divergence names the edge.

Discipline (the house doctrine): CONFIRMATION over prediction — a story with no price/breadth/flow is
a value trap, not an edge. Tag inferred inputs as (unverified). Be selective.

For each GENUINE edge you'd stake your reputation on:
  - call propose_thesis(subject, lean, conviction, horizon_d, thesis, evidence, prob_correct)
  - if it's a new narrative, call flag_emerging_theme(name, stage, tickers, rationale)
Then call save_research_note to summarize your conclusions and reasoning chain.

REQUIRED: every save_research_note body MUST be 250-500 words and contain ALL of these sections
(use these exact markdown headings so the parser can find them):

## Thesis
1-3 sentences: the core claim, why NOW, and the expected outcome if correct.

## Mechanism
The causal chain — what is actually happening operationally/financially/structurally that
makes this true. Not the conclusion; the plumbing that produces it.

## 2nd- and 3rd-order effects
Who else is touched? Supply-chain winners/losers, competitors that benefit or get hurt,
credit/refinancing knock-ons, labor / regulatory ripple effects. At least 3 specific points.

## Affected tickers
- Primary: the direct beneficiaries or victims (the subject + close peers).
- Secondary: 1-2 steps removed (suppliers, customers, substitutes, competitive set).
- Short candidates: names that are hurt if the thesis is right.

## What to watch
Specific measurable catalysts with approximate dates or thresholds — earnings dates, policy
announcements, data releases, price/volume triggers. At least 3 concrete watchpoints.

## Risk & invalidation
What specific evidence would prove this WRONG? Be precise — e.g. "revenue guidance cut >10%
in the next quarterly report" or "Fed hikes instead of cutting by September." Vague invalidators
(e.g. 'macro worsens') are not acceptable. The falsifier must be concrete.

## Lens summary
A compact table of the key lenses you checked (at minimum the validated + context lenses):
| Lens | Direction | Key value | Note |

Nothing you do executes a trade — the engine gates sizing and the falsifier. Propose, don't size."""


def _intake_brief(limit: int = 12) -> str:
    """Pre-context: the ranked intake worklist injected into the prompt so the brain starts
    FROM the dashboard's salience ranking instead of a cold hunt. Degrade-safe (returns '')."""
    try:
        from brain import intake
        q = intake.build(limit=limit)
    except Exception:  # noqa: BLE001
        return ""
    cands = q.get("candidates") or []
    if not cands:
        return ""
    lines = []
    for c in cands:
        lean = {1: "↑", -1: "↓", 0: "·"}.get(c.get("lean"), "?")
        flag = " ⚡DIVERGENT" if c.get("divergent") else ""
        why = c["reasons"][0] if c.get("reasons") else ""
        lines.append(f"  {c['ticker']:6} score={c['score']:.2f} {lean} "
                     f"[{','.join(c.get('sources') or [])}]{flag} — {why}")
    mc = q.get("macro_context") or {}
    frame = ", ".join(f"{k}={mc[k]}" for k in ("regime", "cycle", "liquidity", "fed_stance")
                      if mc.get(k))
    return ("\n\n--- TODAY'S INTAKE WORKLIST (pre-loaded from the dashboard signal engines; "
            "verify with get_daily_briefing / get_intake_candidates) ---\n"
            + (f"Macro frame: {frame}\n" if frame else "")
            + "\n".join(lines)
            + "\n(⚡DIVERGENT = tape vs smart-money disagree — highest-information; research these first.)")


def run_daily_research(asof: str | None = None, *, max_turns: int | None = None) -> dict:
    """Run the armed research session. Needs a subscription credential to reach Claude;
    degrades gracefully (ok=False) when unauthenticated."""
    regime = json.loads((Path(cli_bridge._ROOT) / "vendor" / "macro" / "data" / "regime" / "latest.json").read_text())
    asof = asof or regime["date"]
    # NB: targeted replace (not str.format) — the prompt body contains literal
    # braces (e.g. "{validated, context, partial}") that .format() misreads as fields.
    prompt = (RESEARCH_PROMPT
              .replace("{asof}", asof)
              .replace("{quad}", regime["quad"])
              .replace("{quad_name}", regime.get("quad_name", "")))
    prompt += _intake_brief()             # stage-1 salience pre-context (degrade-safe)
    if not cli_bridge.available():
        return {"ok": False, "error": "claude CLI/SDK not available", "asof": asof}
    return cli_bridge.research_sync(prompt, role="deep", max_turns=max_turns)


def _clamp(lean: str, subject: str, blocked: set[str]) -> tuple[str, str]:
    """Risk officer: a bullish lean on a blocked name is clamped to a watch (de-escalate only)."""
    if lean in _BULLISH and subject.upper() in {b.upper() for b in blocked}:
        return "watch", "clamped: subject is engine-blocked (cannot escalate)"
    return lean, ""


def _engine_blocked(subject: str) -> bool:
    """Whether the deterministic engine hard-blocks this name (a hard veto or size_authority
    'blocked'). Lets the risk officer de-escalate a bullish Claude lean on a vetoed name even when
    the caller passes no `blocked` set — the daily loop never did, so the clamp was dead there and a
    bullish thesis on a parabolic / Altman-distress name was ingested un-clamped."""
    try:
        from portfolio import lenses
        syn = lenses.full(subject, "name").get("synthesis", {})
        return bool(syn.get("vetoes")) or syn.get("size_authority") == "blocked"
    except Exception:
        return False


def ingest_proposals(asof: str | None = None, *, blocked: set[str] | None = None,
                     con=None) -> dict:
    """Convert Claude's 'proposed' rows into gated, falsifiable ledger theses. Returns a summary."""
    blocked = blocked or set()
    asof = asof or date.today().isoformat()
    if not _PROPOSALS.exists():
        return {"ingested": 0, "theses": [], "note": "no proposals"}

    rows = [json.loads(l) for l in _PROPOSALS.read_text().splitlines() if l.strip()]
    out, kept = [], []
    for i, r in enumerate(rows):
        if r.get("status") != "proposed":
            kept.append(r)
            continue
        subj = r.get("subject", "")
        # de-escalate a bullish lean on an engine-blocked name whether the caller flagged it OR the
        # engine itself vetoes it (the daily loop passes no `blocked` set, so self-derive it).
        eff_blocked = set(blocked)
        if subj.upper() not in {b.upper() for b in eff_blocked} and _engine_blocked(subj):
            eff_blocked.add(subj)
        lean, clamp_note = _clamp(r.get("lean", "watch"), subj, eff_blocked)
        doc = DecisionDoc(
            id=f"{asof}-{r['subject']}-claude-{i}", subject=r["subject"], lean=lean,
            conviction=("low" if clamp_note else r.get("conviction", "low")),
            prob_correct=float(r.get("prob_correct") or 0.55), horizon_d=int(r.get("horizon_d") or 21),
            thesis=r.get("thesis", ""), state_asof=asof, evidence=r.get("evidence", []),
            dissent=clamp_note, sleeve="conviction",
        ).finalize()                     # engine-derives the falsifier + check_by + time_stop_by
        appended = ledger.append(doc.to_json())
        if con is not None:
            try:
                from data_layer import store
                store.insert_thesis(con, doc.to_json())
            except Exception:
                pass
        out.append({"id": doc.id, "subject": doc.subject, "lean": doc.lean,
                    "clamped": bool(clamp_note), "appended": appended,
                    "falsifier_kind": doc.falsifier["check"]["kind"]})
        kept.append({**r, "status": "ingested", "thesis_id": doc.id})

    _PROPOSALS.write_text("".join(json.dumps(r, default=str) + "\n" for r in kept))
    return {"ingested": len(out), "clamped": sum(1 for o in out if o["clamped"]), "theses": out, "asof": asof}


def daily_research_and_ingest(asof: str | None = None, *, blocked: set[str] | None = None) -> dict:
    """One call: run the armed research session, then gate its proposals into the ledger."""
    res = run_daily_research(asof)
    ing = ingest_proposals(asof, blocked=blocked)
    return {"research": {"ok": res.get("ok"), "tools_used": res.get("tools_used"),
                         "summary": (res.get("text") or "")[:600], "error": res.get("error")},
            "ingest": ing}
