"""Minimal FastAPI surface for Phase 0.

Not the full app — just enough to prove the service can boot, import the
engine in-process, and serve the regime + a health check. The scheduler,
SSE feed, brain, loop and bridge are added in later phases.

Run:  uvicorn app.main:app --reload
"""
from __future__ import annotations

import json
from pathlib import Path

from app.deps import data_dir

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

    # App-level auth gate — protects the UI + every /api route + the SSE stream.
    # Opt-in via MASTERMIND_PASSWORD; a no-op (auth disabled) when it's unset, so
    # local dev is unchanged. MUST be installed before the server handles requests.
    from app import auth
    auth.install(app)

    # Resolved once at startup: /health is polled by uptime probes and must not
    # spawn a subprocess per request. Fails closed to no version field.
    import subprocess, shlex  # noqa: E401
    try:
        _git_sha = subprocess.check_output(
            shlex.split("git rev-parse --short HEAD"),
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
    except Exception:
        _git_sha = None

    def _startup_flags() -> dict:
        """Snapshot of MASTERMIND_* env vars at startup time — embedded in the app_started event."""
        try:
            from control_plane import flags
            return flags.enumerate_flags()
        except Exception:
            return {}

    @app.get("/health")
    def health() -> dict:
        # Keep only non-sensitive fields; uptime probes check `status == "ok"` only.
        # Filesystem paths and CLI paths are omitted — they leak on an open route.
        return {"status": "ok", "paper_only": True,
                **({"version": _git_sha} if _git_sha else {})}

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

        # MW1: record an app_started event (survives restarts; JSONL is permanent).
        try:
            from control_plane import run_events
            run_events.append({
                "kind": "app_started",
                "job": "startup",
                "book": "",
                "step": "init",
                "status": "ok",
                "actor": "system",
                "extra": {
                    "git_sha": _git_sha or "unknown",
                    "flags": _startup_flags(),
                },
            })
        except Exception:
            pass

        app.state.scheduler = scheduler.start()   # daily loops on cron; None if apscheduler absent
        # First turn-on: let the autonomous book buy right away instead of waiting for the next
        # scheduled close. No-op once it has a track record; runs in a daemon thread (non-blocking).
        try:
            app.state.autonomous_first_run = scheduler.maybe_first_autonomous_run()
        except Exception:
            app.state.autonomous_first_run = False
        # Heavyweight presses Flagship's best — kick its first build too (no-op until Flagship has a
        # non-empty book to constrain against, and once Heavyweight has a track record).
        try:
            app.state.heavyweight_first_run = scheduler.maybe_first_heavyweight_run()
        except Exception:
            app.state.heavyweight_first_run = False
        # All-China book — kick its first build too (no-op once it has a track record). Runs on
        # Asia's clock thereafter via the china_daily cron.
        try:
            app.state.china_first_run = scheduler.maybe_first_china_run()
        except Exception:
            app.state.china_first_run = False
        try:
            app.state.hk_first_run = scheduler.maybe_first_hk_run()
        except Exception:
            app.state.hk_first_run = False
        # ETF rotation book (US-listed ETFs, doctrine + guardrails) — kick its first build too
        # (no-op once it has a track record). Runs on the US evening cadence thereafter.
        try:
            app.state.etf_first_run = scheduler.maybe_first_etf_run()
        except Exception:
            app.state.etf_first_run = False

    @app.post("/daily")
    def daily(force: bool = False) -> dict:
        """Manually fire the daily loop (gated book + armed research + competitor edge note)."""
        from control_plane import run_ledger, locks
        handle = run_ledger.start_run("daily_loop", book="flagship", trigger="http")
        lock = locks.acquire_or_log("book:flagship", job="daily_loop", book="flagship")
        if lock is None:
            run_ledger.end_run(handle, "skip", severity="ADVISORY_ONLY")
            return {"skipped": True, "reason": "lock_held"}
        try:
            with lock:
                from bot.daily import run_daily
                out = run_daily(force=force)
            run_ledger.end_run(handle, "ok")
            return {k: out.get(k) for k in ("asof", "research", "competitor")} | {
                "book_ran": (out.get("book") or {}).get("ran")}
        except Exception as exc:
            run_ledger.end_run(handle, "error")
            raise

    @app.post("/api/autonomous/run")
    async def autonomous_run(force: bool = False, wait: bool = False) -> dict:
        """Manually fire the autonomous Opus-Brain book (research → free-form rebalance).

        The Brain call is long; by default this starts it in the background and returns
        immediately. Pass ?wait=true to block until it finishes (returns the run summary)."""
        from control_plane import run_ledger, locks
        from bot import autonomous
        if wait:
            import asyncio as _aio
            handle = run_ledger.start_run("autonomous_daily", book="autonomous", trigger="http")
            lock = locks.acquire_or_log("book:autonomous", job="autonomous_daily", book="autonomous")
            if lock is None:
                run_ledger.end_run(handle, "skip", severity="ADVISORY_ONLY")
                return {"skipped": True, "reason": "lock_held"}
            try:
                with lock:
                    out = await _aio.to_thread(autonomous.run_autonomous, force=force)
                run_ledger.end_run(handle, "ok")
                return {k: out.get(k) for k in
                        ("asof", "inaugural", "trading_day", "decided", "holdings",
                         "executed", "skipped_unpriceable", "nav", "brain")}
            except Exception:
                run_ledger.end_run(handle, "error")
                raise

        def _run():
            handle = run_ledger.start_run("autonomous_daily", book="autonomous", trigger="http")
            lock = locks.acquire_or_log("book:autonomous", job="autonomous_daily", book="autonomous")
            if lock is None:
                run_ledger.end_run(handle, "skip", severity="ADVISORY_ONLY")
                return
            try:
                with lock:
                    autonomous.run_autonomous(force=force)
                run_ledger.end_run(handle, "ok")
            except Exception as exc:
                run_ledger.end_run(handle, "error")
                raise

        import threading
        threading.Thread(target=_run, name="autonomous-manual-run", daemon=True).start()
        return {"started": True, "note": "Autonomous Brain run started in the background."}

    @app.post("/api/heavyweight/run")
    async def heavyweight_run(force: bool = False, wait: bool = False) -> dict:
        """Manually fire the Heavyweight Opus-Brain book (study Flagship → concentrated rebalance).

        Long call; by default starts in the background and returns immediately. ?wait=true blocks."""
        from control_plane import run_ledger, locks
        from bot import heavyweight
        if wait:
            import asyncio as _aio
            handle = run_ledger.start_run("heavyweight_daily", book="heavyweight", trigger="http")
            lock = locks.acquire_or_log("book:heavyweight", job="heavyweight_daily", book="heavyweight")
            if lock is None:
                run_ledger.end_run(handle, "skip", severity="ADVISORY_ONLY")
                return {"skipped": True, "reason": "lock_held"}
            try:
                with lock:
                    out = await _aio.to_thread(heavyweight.run_heavyweight, force=force)
                run_ledger.end_run(handle, "ok")
                return {k: out.get(k) for k in
                        ("asof", "inaugural", "trading_day", "decided", "holdings", "flagship_universe_size",
                         "held_prior_book", "enforcement", "executed", "skipped_unpriceable", "nav", "brain")}
            except Exception:
                run_ledger.end_run(handle, "error")
                raise

        def _run():
            handle = run_ledger.start_run("heavyweight_daily", book="heavyweight", trigger="http")
            lock = locks.acquire_or_log("book:heavyweight", job="heavyweight_daily", book="heavyweight")
            if lock is None:
                run_ledger.end_run(handle, "skip", severity="ADVISORY_ONLY")
                return
            try:
                with lock:
                    heavyweight.run_heavyweight(force=force)
                run_ledger.end_run(handle, "ok")
            except Exception:
                run_ledger.end_run(handle, "error")
                raise

        import threading
        threading.Thread(target=_run, name="heavyweight-manual-run", daemon=True).start()
        return {"started": True, "note": "Heavyweight Brain run started in the background."}

    @app.post("/api/china/run")
    async def china_run(force: bool = False, wait: bool = False) -> dict:
        """Manually fire the all-China Opus-Brain book (research the China desks → free-form,
        multi-venue, USD-marked rebalance).

        Long call; by default starts in the background and returns immediately. ?wait=true blocks."""
        from control_plane import run_ledger, locks
        from bot import china
        if wait:
            import asyncio as _aio
            handle = run_ledger.start_run("china_daily", book="china", trigger="http")
            lock = locks.acquire_or_log("book:china", job="china_daily", book="china")
            if lock is None:
                run_ledger.end_run(handle, "skip", severity="ADVISORY_ONLY")
                return {"skipped": True, "reason": "lock_held"}
            try:
                with lock:
                    out = await _aio.to_thread(china.run_china, force=force)
                run_ledger.end_run(handle, "ok")
                return {k: out.get(k) for k in
                        ("asof", "inaugural", "trading_day", "decided", "holdings",
                         "executed", "skipped_unpriceable", "nav", "brain")}
            except Exception:
                run_ledger.end_run(handle, "error")
                raise

        def _run():
            handle = run_ledger.start_run("china_daily", book="china", trigger="http")
            lock = locks.acquire_or_log("book:china", job="china_daily", book="china")
            if lock is None:
                run_ledger.end_run(handle, "skip", severity="ADVISORY_ONLY")
                return
            try:
                with lock:
                    china.run_china(force=force)
                run_ledger.end_run(handle, "ok")
            except Exception:
                run_ledger.end_run(handle, "error")
                raise

        import threading
        threading.Thread(target=_run, name="china-manual-run", daemon=True).start()
        return {"started": True, "note": "China Brain run started in the background."}

    @app.post("/api/hk/run")
    async def hk_run(force: bool = False, wait: bool = False) -> dict:
        """Manually fire the Hong-Kong Opus-Brain book (HK listings only, HKD-marked rebalance).

        Long call; by default starts in the background and returns immediately. ?wait=true blocks."""
        from control_plane import run_ledger, locks
        from bot import hk
        if wait:
            import asyncio as _aio
            handle = run_ledger.start_run("hk_daily", book="hk", trigger="http")
            lock = locks.acquire_or_log("book:hk", job="hk_daily", book="hk")
            if lock is None:
                run_ledger.end_run(handle, "skip", severity="ADVISORY_ONLY")
                return {"skipped": True, "reason": "lock_held"}
            try:
                with lock:
                    out = await _aio.to_thread(hk.run_hk, force=force)
                run_ledger.end_run(handle, "ok")
                return {k: out.get(k) for k in
                        ("asof", "inaugural", "trading_day", "decided", "holdings",
                         "executed", "skipped_unpriceable", "nav", "brain")}
            except Exception:
                run_ledger.end_run(handle, "error")
                raise

        def _run():
            handle = run_ledger.start_run("hk_daily", book="hk", trigger="http")
            lock = locks.acquire_or_log("book:hk", job="hk_daily", book="hk")
            if lock is None:
                run_ledger.end_run(handle, "skip", severity="ADVISORY_ONLY")
                return
            try:
                with lock:
                    hk.run_hk(force=force)
                run_ledger.end_run(handle, "ok")
            except Exception:
                run_ledger.end_run(handle, "error")
                raise

        import threading
        threading.Thread(target=_run, name="hk-manual-run", daemon=True).start()
        return {"started": True, "note": "HK Brain run started in the background."}

    @app.post("/api/etf/run")
    async def etf_run(force: bool = False, wait: bool = False) -> dict:
        """Manually fire the ETF Opus-Brain book (rotate across US-listed ETFs under doctrine +
        risk guardrails).

        Long call; by default starts in the background and returns immediately. ?wait=true blocks."""
        from control_plane import run_ledger, locks
        from bot import etf
        if wait:
            import asyncio as _aio
            handle = run_ledger.start_run("etf_daily", book="etf", trigger="http")
            lock = locks.acquire_or_log("book:etf", job="etf_daily", book="etf")
            if lock is None:
                run_ledger.end_run(handle, "skip", severity="ADVISORY_ONLY")
                return {"skipped": True, "reason": "lock_held"}
            try:
                with lock:
                    out = await _aio.to_thread(etf.run_etf, force=force)
                run_ledger.end_run(handle, "ok")
                return {k: out.get(k) for k in
                        ("asof", "inaugural", "trading_day", "decided", "holdings", "risk_state",
                         "guardrails", "executed", "skipped_unpriceable", "nav", "brain")}
            except Exception:
                run_ledger.end_run(handle, "error")
                raise

        def _run():
            handle = run_ledger.start_run("etf_daily", book="etf", trigger="http")
            lock = locks.acquire_or_log("book:etf", job="etf_daily", book="etf")
            if lock is None:
                run_ledger.end_run(handle, "skip", severity="ADVISORY_ONLY")
                return
            try:
                with lock:
                    etf.run_etf(force=force)
                run_ledger.end_run(handle, "ok")
            except Exception:
                run_ledger.end_run(handle, "error")
                raise

        import threading
        threading.Thread(target=_run, name="etf-manual-run", daemon=True).start()
        return {"started": True, "note": "ETF Brain run started in the background."}

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
            last_sid, acc, tools, papers = resume, "", [], []
            try:
                async for ev in cli_bridge.chat_stream(
                        req.message, resume=resume, append_system=advisor.SYSTEM):
                    et = ev.get("type")
                    if et == "text":
                        acc += ev.get("text") or ""
                    elif et == "tool":
                        tools.append({"name": ev.get("name"), "args": ev.get("args")})
                    elif et == "paper":
                        papers.append({k: ev.get(k) for k in
                                       ("paper_id", "ticker", "title", "combined", "confirmed",
                                        "research_score", "engine_score", "viability")})
                    elif et == "done" and ev.get("session_id"):
                        last_sid = ev["session_id"]
                    yield _sse(ev)
            except Exception as exc:                       # never leave the stream hanging
                yield _sse({"type": "error", "error": repr(exc)[:300]})
            finally:
                advisor.set_session(conv_id, last_sid)
                advisor.append_turn(conv_id, "brain", acc, tools, papers)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.get("/chat/history")
    def chat_history(conversation_id: str) -> dict:
        """The stored transcript for a conversation so the popup rehydrates on reload."""
        from brain import advisor
        return {"conversation_id": conversation_id,
                "turns": advisor.load_history(conversation_id)}

    @app.get("/chat/paper")
    def chat_paper(id: str) -> dict:
        """A filed research paper for the in-chat 'open paper' modal (also on the Research page)."""
        from brain import research_paper
        for p in research_paper.load_papers():
            if p.get("id") == id:
                return {"id": p["id"], "ticker": p.get("ticker"),
                        "title": f"{p.get('ticker')} — Research Report",
                        "report_md": p.get("report_md"), "summary": p.get("summary"),
                        "combined": p.get("combined"), "confirmed": p.get("confirmed"),
                        "research_score": p.get("research_score"),
                        "engine_score": p.get("engine_score"),
                        "viability": p.get("viability"), "key_risks": p.get("key_risks") or []}
        return {"error": "not found", "id": id}
