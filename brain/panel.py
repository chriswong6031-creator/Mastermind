"""Multi-agent panel -> adjudicator (LLM-optional, risk-officer clamped).

When the LLM is available, each candidate gets an analyst pass (opportunity/skeptic)
and an Opus PM adjudication; the result can only DE-ESCALATE the engine lean (mirrors
stock_desk._reconcile — never escalate a gated name). Without a key the adjudicated
lean IS the deterministic engine lean. Either way the falsifier + size are engine-derived.
"""
from __future__ import annotations

import json

from brain import client

_RANK = {"watch": 0, "hold": 1, "add": 2}
_UNRANK = {0: "watch", 1: "hold", 2: "add"}


def _engine_lean(cand: dict) -> str:
    """Deterministic lean from the doctrine: gate 'none' -> watch, 'initial' -> hold(toe), 'full' -> add."""
    return {"none": "watch", "initial": "hold", "full": "add"}[cand["gate"]["size"]]


def adjudicate(state: dict, candidates: list[dict]) -> list[dict]:
    """Return [{ticker, lean, conviction, rationale, llm_used}] — de-escalate only."""
    out = []
    use_llm = client.available()
    for c in candidates:
        engine_lean = _engine_lean(c)
        lean, rationale = engine_lean, "engine doctrine lean"
        if use_llm and engine_lean != "watch":
            sys = ("You are a risk-officer PM. You may only CONFIRM or DE-ESCALATE the engine's "
                   "lean (add->hold->watch), never escalate. Reply JSON {lean, reason}.")
            usr = json.dumps({"ticker": c["ticker"], "engine_lean": engine_lean,
                              "scorecard": c["gate"], "stage": c.get("stage"), "regime": state})
            txt, _ = client.call_model(sys, usr, role="pm")
            if txt:
                try:
                    j = json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
                    cand_lean = j.get("lean", engine_lean)
                    # clamp: cannot escalate
                    lean = _UNRANK[min(_RANK.get(cand_lean, 0), _RANK[engine_lean])]
                    rationale = j.get("reason", rationale)[:200]
                except Exception:
                    pass
        out.append({"ticker": c["ticker"], "lean": lean,
                    "conviction": "low" if lean == "watch" else "medium",
                    "rationale": rationale, "llm_used": use_llm})
    return out
