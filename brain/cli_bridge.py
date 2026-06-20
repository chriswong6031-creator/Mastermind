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


async def reason(prompt: str, *, role: str = "pm", model: str | None = None,
                 system: str | None = None, append_system: str | None = None,
                 allowed_tools: list[str] | None = None, add_dirs: list[str] | None = None,
                 max_turns: int | None = None, cwd: str | None = None) -> dict:
    """Run one headless Claude Code reasoning pass. Returns a structured result dict:
    {ok, text, model, role, cost_usd, session_id, usage, backend, error}."""
    c = _cfg()
    rc = c.get("reasoning", {})
    mdl = resolve_model(role, model)
    tools = allowed_tools if allowed_tools is not None else rc.get("allowed_tools", ["Read", "Grep", "Glob"])
    dirs = _abs_dirs(add_dirs if add_dirs is not None else rc.get("add_dirs", []))
    turns = max_turns or rc.get("max_turns", 1)
    workdir = cwd or str(_ROOT)
    base = {"model": mdl, "role": role}

    if not cli_path():
        return {**base, "ok": False, "backend": "none", "text": None,
                "error": "claude CLI not installed (npm i -g @anthropic-ai/claude-code)"}

    if _SDK:
        try:
            return await _via_sdk(prompt, mdl, role, system, append_system, tools, dirs, turns, workdir,
                                  rc.get("permission_mode", "default"))
        except Exception as e:           # fall through to the CLI subprocess
            base["sdk_error"] = repr(e)[:200]
    return await _via_subprocess(prompt, mdl, role, system, append_system, tools, dirs, turns, workdir,
                                 rc.get("permission_mode", "default"), base)


async def _via_sdk(prompt, mdl, role, system, append_system, tools, dirs, turns, workdir, perm) -> dict:
    opts = _Options(model=mdl, allowed_tools=tools, add_dirs=dirs, cwd=workdir,
                    max_turns=turns, permission_mode=perm)
    if system:
        opts.system_prompt = system
    if append_system:
        opts.append_system_prompt = append_system
    text, cost, sid, usage = None, None, None, None
    async for msg in _sdk_query(prompt=prompt, options=opts):
        if hasattr(msg, "result"):                         # ResultMessage
            text = getattr(msg, "result", None)
            cost = getattr(msg, "total_cost_usd", None)
            sid = getattr(msg, "session_id", None)
            usage = getattr(msg, "usage", None)
        elif text is None and hasattr(msg, "content"):     # last-resort: AssistantMessage text
            blocks = getattr(msg, "content", []) or []
            joined = "".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text")
            if joined:
                text = joined
    return {"ok": bool(text), "text": text, "model": mdl, "role": role,
            "cost_usd": cost, "session_id": sid, "usage": _as_dict(usage), "backend": "sdk"}


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
        stderr=asyncio.subprocess.PIPE, cwd=workdir)
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


def reason_sync(prompt: str, **kw) -> dict:
    """Blocking wrapper for the (sync) brain. Do NOT call from inside a running loop."""
    return asyncio.run(reason(prompt, **kw))
