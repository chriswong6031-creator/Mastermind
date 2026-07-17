"""The Claude Code reasoning bridge — the web app server talking to Claude CLI.

This is the LLM reasoning layer: instead of (or alongside) the metered Messages API
(brain/client.py), the server drives the locally-installed Claude Code CLI headlessly.
That uses the subscription's included tokens, lets Claude SEE the dashboard + bot context
(cwd + add_dirs over the vendored macro engine), and routes work to model-tiered
subagents (.claude/agents/*.md) per our in-house policy (config/agents.yml).

Primary path: the Claude Agent SDK (`claude_agent_sdk.query`, in-process, async).
Fallback path: shelling out to `claude -p --output-format json`.
Both inherit auth from the environment (keychain login / CLAUDE_CODE_OAUTH_TOKEN /
ANTHROPIC_API_KEY) — no key is handled here.
"""
from __future__ import annotations

import asyncio
import functools
import json
import os
import shutil
from pathlib import Path

import yaml

import bot  # noqa: F401

_ROOT = Path(__file__).resolve().parent.parent
_CFG = _ROOT / "config" / "agents.yml"

try:
    from claude_agent_sdk import query as _sdk_query, ClaudeAgentOptions as _Options
    _SDK = True
except Exception:                       # SDK not installed -> subprocess fallback
    _SDK = False


def _block_kind(b) -> str:
    """The kind of an SDK content block: 'text' | 'tool_use' | 'tool_result' | 'thinking'.

    Newer claude_agent_sdk content blocks are dataclasses (TextBlock / ToolUseBlock /
    ToolResultBlock / ThinkingBlock) with NO `.type` field — so a `b.type == "text"` check
    silently fails and every block is dropped (the live-chat '(no response)' bug). Key off
    the class name, falling back to the legacy `.type` string for older SDKs / raw dicts."""
    cls = type(b).__name__
    if cls == "TextBlock":
        return "text"
    if cls == "ToolUseBlock":
        return "tool_use"
    if cls == "ToolResultBlock":
        return "tool_result"
    if cls == "ThinkingBlock":
        return "thinking"
    legacy = getattr(b, "type", "")
    if not legacy and isinstance(b, dict):
        legacy = b.get("type", "")
    return legacy or ""


@functools.lru_cache(maxsize=1)
def _cfg() -> dict:
    return yaml.safe_load(_CFG.read_text())


def resolve_model(role: str | None = None, model: str | None = None) -> str:
    if model:
        return model
    c = _cfg()
    role = role or c.get("default_role", "pm")
    return c.get("roles", {}).get(role, "sonnet")


def cli_path() -> str | None:
    return shutil.which("claude")


def available() -> bool:
    """True if we can reason via Claude Code (SDK present, CLI binary on PATH)."""
    return bool(cli_path()) and _SDK


def _abs_dirs(dirs: list[str]) -> list[str]:
    return [str((_ROOT / d).resolve()) for d in dirs]


def _subscription_env() -> dict:
    """Env for the spawned claude. To use the SUBSCRIPTION (not the metered API) we must
    NOT expose ANTHROPIC_API_KEY — it wins over the subscription login in the credential
    resolution order. Strip it (and ANTHROPIC_AUTH_TOKEN) when prefer_subscription.

    Token pool selector: the macro-side operator deprecated the bare CLAUDE_CODE_OAUTH_TOKEN
    in favour of a numbered pool (_1.._7; slots 3..7 are currently valid, 1..2 are stale).
    When the env contains numbered slots but no bare token, inject the first non-empty slot
    (preferring 3..7 over 1..2) as CLAUDE_CODE_OAUTH_TOKEN in the subprocess env copy only.
    Keychain login is unchanged — no token wins over keychain when the env var is absent.
    """
    e = dict(os.environ)
    if _cfg().get("reasoning", {}).get("prefer_subscription", True):
        e.pop("ANTHROPIC_API_KEY", None)
        e.pop("ANTHROPIC_AUTH_TOKEN", None)

    # Pool selector: prefer slots 3..7 (valid), then 1..2 (stale fallback), over bare var.
    # Only inject when the bare var is absent or empty — don't override an explicit setting.
    if not e.get("CLAUDE_CODE_OAUTH_TOKEN"):
        for slot in (3, 4, 5, 6, 7, 1, 2):
            val = os.environ.get(f"CLAUDE_CODE_OAUTH_TOKEN_{slot}", "")
            if val:
                e["CLAUDE_CODE_OAUTH_TOKEN"] = val
                break

    return e


async def reason(prompt: str, *, role: str = "pm", model: str | None = None,
                 system: str | None = None, append_system: str | None = None,
                 allowed_tools: list[str] | None = None, add_dirs: list[str] | None = None,
                 max_turns: int | None = None, cwd: str | None = None,
                 arm: bool = False, resume: str | None = None,
                 mcp_servers: dict | None = None,
                 log_run: bool = True,
                 book: str | None = None) -> dict:
    """Run a headless Claude Code reasoning pass. With arm=True, attaches MCP tools (read the
    dashboard + write conclusions back) + WebSearch/WebFetch and runs a multi-turn research loop.
    Pass `mcp_servers` (with a matching `allowed_tools`) to arm a CUSTOM tool surface — e.g. the
    autonomous desk's free-form trade tools — instead of the default gated bot server. Returns
    {ok, text, model, role, armed, tools_used, cost_usd, session_id, usage, backend, error}.

    book: when the caller will record cost against cost_guard themselves (e.g. bot/autonomous.py
    which knows its exact PORTFOLIO_ID book name), pass the book name here so cli_bridge skips
    its own _record_cli_cost call and avoids the double-count. When book=None (the default)
    cli_bridge records exactly once under the role-inferred book (flagship / system)."""
    c = _cfg()
    rc = c.get("reasoning", {})
    if arm:
        if mcp_servers is None:
            from brain import bot_mcp
            mcp_servers = {bot_mcp.SERVER_NAME: bot_mcp.build_server()}
            if allowed_tools is None:
                allowed_tools = bot_mcp.armed_allowed_tools()
        if role == "pm":
            role = "deep"
        max_turns = max_turns or rc.get("research_max_turns", 16)

    mdl = resolve_model(role, model)
    tools = allowed_tools if allowed_tools is not None else rc.get("allowed_tools", ["Read", "Grep", "Glob"])
    dirs = _abs_dirs(add_dirs if add_dirs is not None else rc.get("add_dirs", []))
    turns = max_turns or rc.get("max_turns", 1)
    workdir = cwd or str(_ROOT)
    base = {"model": mdl, "role": role, "armed": arm}

    if not cli_path():
        return {**base, "ok": False, "backend": "none", "text": None,
                "error": "claude CLI not installed (npm i -g @anthropic-ai/claude-code)"}

    # --- run-log: open a new run for this session (skipped for utility calls
    #     like translation — log_run=False — so they don't clutter the activity log) ---
    _run_id: str | None = None
    if log_run:
        try:
            from brain import runlog as _rl
            _kind = "research" if arm else "daily"
            _run_id = _rl.start_run(_kind, title=prompt[:120])
            _rl.log_step(_run_id, "reasoning", "session start",
                         f"prompt={prompt[:300]} role={role} model={mdl} armed={arm} turns={turns}")
        except Exception:
            pass

    if _SDK:
        try:
            result = await _via_sdk(prompt, mdl, role, system, append_system, tools, dirs, turns, workdir,
                                    rc.get("permission_mode", "default"), mcp_servers, resume, arm,
                                    run_id=_run_id)
            # close the run
            try:
                from brain import runlog as _rl
                if _run_id:
                    _rl.end_run(_run_id,
                                summary=str(result.get("text") or "")[:200],
                                cost_usd=result.get("cost_usd"))
            except Exception:
                pass
            if _run_id:
                result["run_id"] = _run_id
            # record cost + token usage in the cost guard (best-effort; never raises)
            # skip when the caller passed book= and will record themselves (avoid double-count)
            try:
                _record_cli_cost(result, role=role, model=mdl, book=book)
            except Exception:
                pass
            return result
        except Exception as e:           # fall through to the CLI subprocess (non-armed only)
            base["sdk_error"] = repr(e)[:200]
            try:
                from brain import runlog as _rl
                if _run_id:
                    _rl.log_step(_run_id, "reasoning", "sdk error", repr(e)[:500])
            except Exception:
                pass

    if arm:
        try:
            from brain import runlog as _rl
            if _run_id:
                _rl.end_run(_run_id, summary="armed research failed — SDK not available")
        except Exception:
            pass
        return {**base, "ok": False, "backend": "none", "text": None,
                "error": base.get("sdk_error") or "armed research needs the Agent SDK + a subscription credential",
                "run_id": _run_id}

    result = await _via_subprocess(prompt, mdl, role, system, append_system, tools, dirs, turns, workdir,
                                   rc.get("permission_mode", "default"), base)
    try:
        from brain import runlog as _rl
        if _run_id:
            _rl.log_step(_run_id, "reasoning", "subprocess result",
                         str(result.get("text") or "")[:1000])
            _rl.end_run(_run_id,
                        summary=str(result.get("text") or "")[:200],
                        cost_usd=result.get("cost_usd"))
            result["run_id"] = _run_id
    except Exception:
        pass
    # record cost + token usage in the cost guard (best-effort; never raises)
    # skip when the caller passed book= and will record themselves (avoid double-count)
    try:
        _record_cli_cost(result, role=role, model=mdl, book=book)
    except Exception:
        pass
    return result


async def _via_sdk(prompt, mdl, role, system, append_system, tools, dirs, turns, workdir, perm,
                   mcp_servers, resume, arm, run_id: str | None = None) -> dict:
    opts = _Options(model=mdl, allowed_tools=tools, add_dirs=dirs, cwd=workdir,
                    max_turns=turns, permission_mode=perm, env=_subscription_env())
    if mcp_servers:
        opts.mcp_servers = mcp_servers
    if resume:
        opts.resume = resume
    if system:
        opts.system_prompt = system
    if append_system:
        opts.append_system_prompt = append_system
    text, cost, sid, usage, used = None, None, None, None, []

    # run-log integration — import lazily so the module is optional
    try:
        from brain import runlog as _rl
        _log = _rl.log_step
    except Exception:
        _rl = None
        _log = None

    async for msg in _sdk_query(prompt=prompt, options=opts):
        if hasattr(msg, "result"):                         # ResultMessage
            text = getattr(msg, "result", None) or text
            cost = getattr(msg, "total_cost_usd", None)
            sid = getattr(msg, "session_id", None)
            usage = getattr(msg, "usage", None)
        elif hasattr(msg, "content"):                      # AssistantMessage: collect text + tool calls
            for b in (getattr(msg, "content", []) or []):
                bt = _block_kind(b)
                if bt == "text" and getattr(b, "text", ""):
                    chunk = b.text
                    text = (text or "") + chunk if text is None else chunk
                    # log reasoning chunk
                    if run_id and _log:
                        try:
                            _log(run_id, "reasoning", "assistant text",
                                 chunk[:2000])
                        except Exception:
                            pass
                elif bt == "tool_use":
                    name = getattr(b, "name", "?")
                    used.append(name)
                    # log tool call
                    if run_id and _log:
                        try:
                            inp = getattr(b, "input", {}) or {}
                            _log(run_id, "tool_call", f"call {name}",
                                 json.dumps(inp, default=str)[:2000],
                                 tool=name, args=inp)
                        except Exception:
                            pass
        # ToolResult messages (some SDK versions)
        elif hasattr(msg, "tool_use_id") or (getattr(msg, "type", "") == "tool_result"):
            if run_id and _log:
                try:
                    content = getattr(msg, "content", "") or ""
                    if isinstance(content, list):
                        content = " ".join(
                            getattr(c, "text", str(c)) for c in content)
                    _log(run_id, "tool_result", "tool result",
                         str(content)[:2000], result=str(content)[:2000])
                except Exception:
                    pass

    return {"ok": bool(text), "text": text, "model": mdl, "role": role, "armed": arm,
            "tools_used": used, "cost_usd": cost, "session_id": sid,
            "usage": _as_dict(usage), "backend": "sdk"}


async def _via_subprocess(prompt, mdl, role, system, append_system, tools, dirs, turns, workdir, perm, base) -> dict:
    argv = ["claude", "-p", "--output-format", "json", "--model", mdl,
            "--permission-mode", perm, "--max-turns", str(turns)]
    if tools:
        argv += ["--allowedTools", ",".join(tools)]
    for d in dirs:
        argv += ["--add-dir", d]
    if system:
        argv += ["--system-prompt", system]
    if append_system:
        argv += ["--append-system-prompt", append_system]
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=workdir, env=_subscription_env())
    out, err = await proc.communicate(prompt.encode())
    try:
        j = json.loads(out.decode() or "{}")
    except Exception:
        return {**base, "ok": False, "backend": "cli", "text": None,
                "error": (err.decode()[:300] or "non-JSON output")}
    return {**base, "ok": not j.get("is_error", False), "backend": "cli",
            "text": j.get("result"), "cost_usd": j.get("total_cost_usd"),
            "session_id": j.get("session_id"), "usage": j.get("usage"),
            "error": j.get("error")}


def _as_dict(usage):
    if usage is None or isinstance(usage, dict):
        return usage
    return {k: getattr(usage, k) for k in ("input_tokens", "output_tokens",
            "cache_read_input_tokens", "cache_creation_input_tokens") if hasattr(usage, k)}


# Role -> book mapping for cost_guard recording.  Roles used from the outside:
# "pm"/"deep" = flagship + autonomous (no single book known here); "analyst"/"scout" = system
# utility (translation, loop review).  We use a heuristic "system" book for utility roles and
# "flagship" as the default for heavyweight reasoning roles.  Per-book callers (bot/etf.py etc.)
# that know their book call cost_guard.record directly instead.
_ROLE_BOOK: dict[str, str] = {
    "pm":       "flagship",
    "deep":     "flagship",
    "analyst":  "system",
    "scout":    "system",
}


def _record_cli_cost(result: dict, *, role: str | None = None, model: str | None = None,
                     book: str | None = None) -> None:
    """Record CLI-bridge call cost + tokens in cost_guard. Best-effort; never raises.

    When book is not None the caller (a bot-level _run_brain site) will record against
    cost_guard themselves using the correct per-book PORTFOLIO_ID — so we skip here to
    avoid the double-count.  When book is None we record once under the role-inferred
    book (see _ROLE_BOOK).  Seat is the resolved role name.
    """
    if book is not None:
        # caller records; we are the secondary path — skip to avoid double-count
        return
    try:
        from brain import cost_guard as _cg
        usd = result.get("cost_usd")
        usage = result.get("usage") or {}
        _book = _ROLE_BOOK.get(str(role or "").lower(), "flagship")
        _cg.record(
            _book, usd,
            seat=str(role or "unknown"),
            model=str(model or resolve_model(role)),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens") or 0),
        )
    except Exception:  # noqa: BLE001
        pass


async def research(prompt: str, *, role: str = "deep", max_turns: int | None = None,
                   resume: str | None = None) -> dict:
    """An ARMED, multi-turn research session: Claude reads the dashboard via the bot MCP
    tools, searches the web/news, reasons through 2nd/3rd-order effects, and writes its
    conclusions back to the app (research notes, proposed theses, emerging-theme flags,
    recommendations). Returns the result + the tools it called."""
    return await reason(prompt, role=role, arm=True, max_turns=max_turns, resume=resume)


async def chat_stream(prompt: str, *, resume: str | None = None,
                      append_system: str | None = None, role: str = "deep",
                      max_turns: int | None = None):
    """An ARMED, multi-turn ADVISOR chat turn, STREAMED.

    Same armed Brain as research() (bot MCP tools + web + read-only files), but instead of
    buffering to a dict this yields events as the SDK produces them, so the web layer can
    push them to the browser over SSE. Conversation continuity is via `resume` (the SDK
    persists the session transcript). Events:
        {"type": "text", "text": str}                 # an assistant text chunk
        {"type": "tool", "name": str, "args": dict}   # the Brain calling a tool
        {"type": "tool_result"}                       # a tool returned (chip can settle)
        {"type": "done", "session_id", "cost_usd", "tools_used"}
        {"type": "error", "error": str}
    """
    if not _SDK:
        yield {"type": "error", "error": "reasoning needs the Claude Agent SDK + a subscription credential"}
        return
    if not cli_path():
        yield {"type": "error", "error": "claude CLI not installed (npm i -g @anthropic-ai/claude-code)"}
        return

    from brain import bot_mcp
    rc = _cfg().get("reasoning", {})
    mdl = resolve_model(role)
    opts = _Options(
        model=mdl,
        allowed_tools=bot_mcp.armed_allowed_tools(),
        add_dirs=_abs_dirs(rc.get("add_dirs", [])),
        cwd=str(_ROOT),
        max_turns=max_turns or rc.get("research_max_turns", 16),
        permission_mode=rc.get("permission_mode", "default"),
        env=_subscription_env(),
    )
    opts.mcp_servers = {bot_mcp.SERVER_NAME: bot_mcp.build_server()}
    if resume:
        opts.resume = resume
    if append_system:
        opts.append_system_prompt = append_system

    sid, cost, used = resume, None, []

    def _result_event(raw):
        """A tool result -> a 'paper' event if it carries the marker, else 'tool_result'."""
        if isinstance(raw, (list, tuple)):
            raw = " ".join(getattr(c, "text", "") or (c.get("text", "") if isinstance(c, dict) else str(c))
                           for c in raw)
        raw = str(raw or "")
        if bot_mcp.PAPER_MARKER in raw:                    # a research paper was filed
            try:
                frag = raw.split(bot_mcp.PAPER_MARKER, 1)[1]
                return {"type": "paper", **json.loads(frag[frag.find("{"):frag.rfind("}") + 1])}
            except Exception:
                pass
        return {"type": "tool_result"}

    emitted_text = False
    try:
        async for msg in _sdk_query(prompt=prompt, options=opts):
            if hasattr(msg, "result"):                         # ResultMessage (end of turn)
                sid = getattr(msg, "session_id", None) or sid
                cost = getattr(msg, "total_cost_usd", None)
                # Fallback: if the per-block text events never fired (e.g. a future SDK shape
                # we don't recognise), surface the buffered final text so the chat is never
                # silently empty — this is the '(no response)' backstop.
                final = getattr(msg, "result", None)
                if not emitted_text and final:
                    emitted_text = True
                    yield {"type": "text", "text": final}
                continue
            blocks = getattr(msg, "content", None)
            if isinstance(blocks, (list, tuple)):              # Assistant/User message: content blocks
                for b in blocks:
                    bt = _block_kind(b)
                    if bt == "text" and getattr(b, "text", ""):
                        emitted_text = True
                        yield {"type": "text", "text": b.text}
                    elif bt == "tool_use":
                        name = getattr(b, "name", "?")
                        used.append(name)
                        yield {"type": "tool", "name": name, "args": getattr(b, "input", {}) or {}}
                    elif bt == "tool_result":                  # tool results arrive as blocks
                        yield _result_event(getattr(b, "content", "") or "")
            elif hasattr(msg, "tool_use_id") or getattr(msg, "type", "") == "tool_result":
                yield _result_event(getattr(msg, "content", "") or "")
            elif isinstance(blocks, str) and blocks:           # bare-string assistant text
                emitted_text = True
                yield {"type": "text", "text": blocks}
    except Exception as e:                                      # surface, don't crash the stream
        yield {"type": "error", "error": repr(e)[:300]}
        return

    yield {"type": "done", "session_id": sid, "cost_usd": cost, "tools_used": used}


def reason_sync(prompt: str, **kw) -> dict:
    """Blocking wrapper for the (sync) brain. Do NOT call from inside a running loop."""
    return asyncio.run(reason(prompt, **kw))


def research_sync(prompt: str, **kw) -> dict:
    return asyncio.run(research(prompt, **kw))
