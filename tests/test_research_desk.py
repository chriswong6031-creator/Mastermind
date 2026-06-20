"""The marriage: Claude's free-form proposals -> gated, falsifiable ledger theses.

No Claude auth needed — we write proposals directly (as the MCP action tool would) and
prove the doctrine gates turn them into engine-derived, Brier-scorable theses, clamping a
bullish lean on an engine-blocked name.
"""
import asyncio
import json
from pathlib import Path

import bot  # noqa: F401

from brain import bot_mcp, research_desk, ledger

_BRAIN = Path(__file__).resolve().parent.parent / "data" / "brain"
_PROPOSALS = _BRAIN / "proposals.jsonl"
_THESES = _BRAIN / "theses.jsonl"


def _clear():
    for p in (_PROPOSALS, _THESES):
        if p.exists():
            p.unlink()


def test_proposals_gate_into_falsifiable_ledger():
    _clear()
    # Claude proposes two theses via the MCP action tool
    asyncio.run(bot_mcp.propose_thesis.handler(
        {"subject": "NVDA", "lean": "add", "conviction": "high", "horizon_d": 21,
         "thesis": "AI-infra capex re-accelerating", "evidence": ["breadth+"], "prob_correct": 0.95}))
    asyncio.run(bot_mcp.propose_thesis.handler(
        {"subject": "SMCI", "lean": "add", "conviction": "high",
         "thesis": "adjacency to AI", "prob_correct": 0.8}))

    out = research_desk.ingest_proposals("2026-06-17", blocked={"SMCI"})
    assert out["ingested"] == 2 and out["clamped"] == 1

    theses = {t["subject"]: t for t in ledger.all_theses()}
    # engine derives the falsifier for BOTH (not the model)
    assert theses["NVDA"]["falsifier"]["check"]["kind"] == "rel_return"
    assert theses["NVDA"]["check_by"] and theses["NVDA"]["time_stop_by"]
    # prob_correct clamped into [0.50, 0.85] regardless of Claude's 0.95
    assert 0.50 <= theses["NVDA"]["prob_correct"] <= 0.85
    # risk officer clamped the bullish lean on the engine-blocked name
    assert theses["SMCI"]["lean"] == "watch"

    # re-ingest is idempotent (rows now marked 'ingested')
    assert research_desk.ingest_proposals("2026-06-17")["ingested"] == 0
    _clear()
