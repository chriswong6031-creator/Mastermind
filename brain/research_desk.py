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

RESEARCH_PROMPT = """You are the deep research desk for an autonomous, paper-only narrative-investing bot.
Today is {asof}; the macro regime is {quad} ({quad_name}).

Use your tools: get_regime / get_themes / get_standouts / get_portfolio / read_signal to read the
LIVE dashboard, and WebSearch / WebFetch to scan recent news, events, filings, and narratives.

Reason through SECOND and THIRD-order effects — supply-chain flow, shortages/overages, earnings and
guidance, accounting events, institutional news and flow. Hunt for: emerging themes, themes rolling
over, and asymmetric single-name edges that others haven't connected yet.

Discipline (the house doctrine): CONFIRMATION over prediction — a story with no price/breadth/flow is
a value trap, not an edge. Tag inferred inputs as (unverified). Be selective.

For each GENUINE edge you'd stake your reputation on:
  - call propose_thesis(subject, lean, conviction, horizon_d, thesis, evidence, prob_correct)
  - if it's a new narrative, call flag_emerging_theme(name, stage, tickers, rationale)
Then call save_research_note to summarize your conclusions and reasoning chain.

Nothing you do executes a trade — the engine gates sizing and the falsifier. Propose, don't size."""


def run_daily_research(asof: str | None = None, *, max_turns: int | None = None) -> dict:
    """Run the armed research session. Needs a subscription credential to reach Claude;
    degrades gracefully (ok=False) when unauthenticated."""
    regime = json.loads((Path(cli_bridge._ROOT) / "vendor" / "macro" / "data" / "regime" / "latest.json").read_text())
    asof = asof or regime["date"]
    prompt = RESEARCH_PROMPT.format(asof=asof, quad=regime["quad"], quad_name=regime.get("quad_name", ""))
    if not cli_bridge.available():
        return {"ok": False, "error": "claude CLI/SDK not available", "asof": asof}
    return cli_bridge.research_sync(prompt, role="deep", max_turns=max_turns)


def _clamp(lean: str, subject: str, blocked: set[str]) -> tuple[str, str]:
    """Risk officer: a bullish lean on a blocked name is clamped to a watch (de-escalate only)."""
    if lean in _BULLISH and subject.upper() in {b.upper() for b in blocked}:
        return "watch", "clamped: subject is engine-blocked (cannot escalate)"
    return lean, ""


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
        lean, clamp_note = _clamp(r.get("lean", "watch"), r.get("subject", ""), blocked)
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
