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

    from app import web

    app = FastAPI(title="Mastermind", version="0.0.1")

    app.include_router(web.router)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "engine_root": engine_root(),
                "reasoning": {"claude_cli": cli_bridge.cli_path(), "available": cli_bridge.available()}}

    @app.get("/regime")
    def regime() -> dict:
        return _regime_payload()

    class ResearchReq(BaseModel):
        prompt: str
        role: str = "deep"
        max_turns: int | None = None
        resume: str | None = None     # continue a prior research session by id

    @app.post("/reason")
    async def reason(req: ReasonReq) -> dict:
        """Drive Claude Code headlessly for a reasoning pass over the dashboard context."""
        return await cli_bridge.reason(
            req.prompt, role=req.role, append_system=req.append_system, max_turns=req.max_turns)

    @app.on_event("startup")
    def _start_scheduler():
        from app import scheduler
        app.state.scheduler = scheduler.start()   # daily loop on cron; None if apscheduler absent

    @app.post("/daily")
    def daily(force: bool = False) -> dict:
        """Manually fire the daily loop (gated book + armed research + competitor edge note)."""
        from bot.daily import run_daily
        out = run_daily(force=force)
        return {k: out.get(k) for k in ("asof", "research", "competitor")} | {
            "book_ran": (out.get("book") or {}).get("ran")}

    @app.post("/research")
    async def research(req: ResearchReq) -> dict:
        """An ARMED, multi-turn research session: Claude reads the dashboard (MCP tools),
        searches the web/news, reasons through 2nd/3rd-order effects, and writes conclusions
        back to the app (research notes, proposed theses, emerging-theme flags). Paper-only —
        nothing auto-executes."""
        return await cli_bridge.research(
            req.prompt, role=req.role, max_turns=req.max_turns, resume=req.resume)

    # ---- Live advisor chat (streamed) -------------------------------------
    from fastapi.responses import StreamingResponse

    class ChatReq(BaseModel):
        message: str
        conversation_id: str | None = None   # carry across turns to keep context

    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj, default=str)}\n\n"

    @app.post("/chat")
    async def chat(req: ChatReq) -> StreamingResponse:
        """Talk to the Brain live. Streams the armed advisor's reasoning, tool use, and
        verdict back over SSE. The Brain reads the dashboard / book on demand and can stage
        recommendations + theses into the review queue — it never executes a trade.

        Each SSE frame is a JSON event: start | text | tool | tool_result | done | error.
        Pass back `conversation_id` (from the `start` frame) on the next turn for continuity.
        """
        from brain import advisor

        conv_id = req.conversation_id or advisor.new_conversation_id()
        resume = advisor.get_session(conv_id)

        async def gen():
            yield _sse({"type": "start", "conversation_id": conv_id})
            advisor.append_turn(conv_id, "user", req.message)
            last_sid, acc, tools = resume, "", []
            try:
                async for ev in cli_bridge.chat_stream(
                        req.message, resume=resume, append_system=advisor.SYSTEM):
                    et = ev.get("type")
                    if et == "text":
                        acc += ev.get("text") or ""
                    elif et == "tool":
                        tools.append({"name": ev.get("name"), "args": ev.get("args")})
                    elif et == "done" and ev.get("session_id"):
                        last_sid = ev["session_id"]
                    yield _sse(ev)
            except Exception as exc:                       # never leave the stream hanging
                yield _sse({"type": "error", "error": repr(exc)[:300]})
            finally:
                advisor.set_session(conv_id, last_sid)
                advisor.append_turn(conv_id, "brain", acc, tools)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.get("/chat/history")
    def chat_history(conversation_id: str) -> dict:
        """The stored transcript for a conversation so the popup rehydrates on reload."""
        from brain import advisor
        return {"conversation_id": conversation_id,
                "turns": advisor.load_history(conversation_id)}
