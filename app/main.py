"""Minimal FastAPI surface for Phase 0.

Not the full app — just enough to prove the service can boot, import the
engine in-process, and serve the regime + a health check. The scheduler,
SSE feed, brain, loop and bridge are added in later phases.

Run:  uvicorn app.main:app --reload
"""
from __future__ import annotations

import json
from pathlib import Path

from app.deps import data_dir, engine_root

try:
    from fastapi import FastAPI
except ImportError:  # FastAPI is optional until Phase 1
    FastAPI = None  # type: ignore


def _regime_payload() -> dict:
    latest = json.loads((Path(data_dir()) / "regime" / "latest.json").read_text())
    return {
        "quad": latest["quad"],
        "quad_name": latest.get("quad_name"),
        "growth_score": latest["growth_score"],
        "inflation_score": latest["inflation_score"],
        "liquidity_overlay": latest["liquidity_overlay"],
        "as_of": latest["date"],
    }


if FastAPI is not None:
    from pydantic import BaseModel
    from brain import cli_bridge

    class ReasonReq(BaseModel):
        prompt: str
        role: str = "pm"          # pm | deep | analyst | scout | fable
        append_system: str | None = None
        max_turns: int | None = None

    app = FastAPI(title="narrator-bot", version="0.0.1")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "engine_root": engine_root(),
                "reasoning": {"claude_cli": cli_bridge.cli_path(), "available": cli_bridge.available()}}

    @app.get("/regime")
    def regime() -> dict:
        return _regime_payload()

    @app.post("/reason")
    async def reason(req: ReasonReq) -> dict:
        """Drive Claude Code headlessly for a reasoning pass over the dashboard context."""
        return await cli_bridge.reason(
            req.prompt, role=req.role, append_system=req.append_system, max_turns=req.max_turns)
