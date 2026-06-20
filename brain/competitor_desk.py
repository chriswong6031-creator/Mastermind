"""Competitor desk — armed Claude studies Quiver's AI strategies and writes our edge.

Daily: pull Quiver's 4 strategies (PIT snapshot), then run an armed Opus session that
compares their process to ours via the decision matrix + web, and writes a STANDING note:
"what is their process doing now, and where do we have an edge." Paper-only; proposes nothing
that auto-executes. Degrades gracefully without Quiver creds or a Claude credential.
"""
from __future__ import annotations

from datetime import date

import bot  # noqa: F401

from brain import cli_bridge

PROMPT = """You are running competitive intelligence for our narrative-investing bot against Quiver Quant's
four AI strategies (ChatGPT/Claude x Enhanced/Standard). Today is {asof}.

1. Call get_quiver_compare to see all four — model, metrics (Sharpe/alpha/maxDD/win/beta), holdings, turnover.
2. Call get_quiver_strategy('chatgpt_enhanced') (their best) and read the per-pick AI justifications + model.
3. For their top 4-5 holdings, call get_decision_matrix(ticker) and compare OUR multi-sided read to their thesis.
   Flag every place our matrix DISAGREES (e.g. they hold a name our matrix flags 'distribution' or 'extension').
4. Optionally WebSearch for a recent catalyst that confirms or refutes one of their picks.

Then call save_research_note (title "Quiver competitor read - {asof}") with:
 - WHAT their process is doing now: model, concentration, dominant theme, turnover, long-only.
 - WHERE WE HAVE AN EDGE: where our decision matrix / risk discipline / dashboard depth (regime, flows,
   policy, options) sees something they miss; note that their 'Claude' runs claude-haiku-4-5 (a mini model)
   while we reason on Opus 4.8 — a tier above.
 - WHERE THEY HAVE AN EDGE worth adopting: reasoning style, alt-data use, concentration when the regime warrants.
 - HONEST RISK READ: their ChatGPT-Enhanced Sharpe ~16 / beta ~2 is a small-sample, concentrated AI bet — name the fragility.
Be specific, cite tickers + lens reads, tag inferred inputs (unverified). Propose nothing that auto-executes."""


def pull(quiet: bool = True) -> dict:
    """Snapshot Quiver's strategies if QUIVER_USER/QUIVER_PASS are set."""
    try:
        from data_layer import quiver
        out = quiver.pull_all()
        return {"ok": True, "strategies": list(out)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


def analyze(asof: str | None = None, *, do_pull: bool = True) -> dict:
    asof = asof or date.today().isoformat()
    pulled = pull() if do_pull else {"ok": False, "skipped": True}
    if not cli_bridge.available():
        return {"asof": asof, "pull": pulled, "analysis": {"ok": False, "error": "claude CLI/SDK unavailable"}}
    res = cli_bridge.research_sync(PROMPT.format(asof=asof), role="deep", max_turns=12)
    return {"asof": asof, "pull": pulled,
            "analysis": {"ok": res.get("ok"), "tools_used": res.get("tools_used"),
                         "summary": (res.get("text") or "")[:1000], "error": res.get("error")}}
