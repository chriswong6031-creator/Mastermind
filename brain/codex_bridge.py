"""OpenAI Codex CLI backend for the Mastermind reasoning contract.

This backend deliberately reuses the existing ``cli_bridge.reason`` result
shape so the deterministic portfolio builders do not know which LLM produced
the recommendation.  Codex runs non-interactively with ChatGPT-managed auth,
in a read-only sandbox.  When a paper-book run needs typed tools, the existing
in-process Claude SDK MCP tools are exposed through ``brain.codex_mcp_stdio``;
only those tools retain their existing, narrowly-scoped write authority.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_CFG = _ROOT / "config" / "agents.yml"


def _cfg() -> dict:
    try:
        return yaml.safe_load(_CFG.read_text()) or {}
    except Exception:
        return {}


def codex_path() -> str | None:
    return shutil.which("codex")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()


def available() -> bool:
    """True when Codex is installed and a cached ChatGPT/API login is present."""
    return bool(codex_path()) and (codex_home() / "auth.json").is_file()


def resolve_model(role: str | None = None, model: str | None = None) -> tuple[str, str]:
    """Return the configured Codex model and reasoning effort for a role."""
    if model:
        selected = model
    else:
        cfg = _cfg().get("codex") or {}
        roles = cfg.get("roles") or {}
        selected = roles.get(role or "pm") or cfg.get("model") or "gpt-5.6-sol"
    effort = str((_cfg().get("codex") or {}).get("reasoning_effort") or "xhigh")
    return str(selected), effort


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _mcp_overrides(mcp_servers: dict | None, *, book: str | None,
                   python: str) -> list[str]:
    """Build per-invocation Codex MCP config overrides.

    The caller's in-process server map tells us the exact server names that
    were authorized by the existing book surface.  The stdio adapter rebuilds
    the same surface in a child process, selected by book and server name.
    """
    if not mcp_servers:
        return []
    out: list[str] = []
    safe_book = str(book or "system")
    for name in sorted(mcp_servers):
        if not name.replace("_", "").replace("-", "").isalnum():
            continue
        args = [
            "-m", "brain.codex_mcp_stdio",
            "--book", safe_book,
            "--server", str(name),
        ]
        out += [
            "-c", f"mcp_servers.{name}.command={json.dumps(python)}",
            "-c", f"mcp_servers.{name}.args={json.dumps(args)}",
            "-c", f"mcp_servers.{name}.cwd={json.dumps(str(_ROOT))}",
            "-c", f"mcp_servers.{name}.startup_timeout_sec=30",
            # The server surface is already book-scoped and allow-listed by
            # the deterministic bot. Non-interactive jobs cannot answer an
            # approval prompt, so pre-approve calls on this trusted local MCP.
            "-c", f'mcp_servers.{name}.default_tools_approval_mode="approve"',
        ]
    return out


def _compose_prompt(prompt: str, *, system: str | None,
                    append_system: str | None, max_turns: int | None) -> str:
    sections: list[str] = []
    if system:
        sections += ["<system_context>", system.strip(), "</system_context>", ""]
    if append_system:
        sections += ["<additional_system_context>", append_system.strip(),
                     "</additional_system_context>", ""]
    sections += ["<task>", prompt.strip(), "</task>"]
    if max_turns:
        sections += [
            "",
            "<completion_boundary>",
            f"Complete this task in at most {int(max_turns)} tool/reasoning rounds. "
            "When the required submission or answer is complete, stop.",
            "</completion_boundary>",
        ]
    return "\n".join(sections)


def _parse_jsonl(raw: str) -> dict[str, Any]:
    text: str | None = None
    thread_id: str | None = None
    usage: dict[str, Any] = {}
    tools: list[str] = []
    errors: list[str] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except Exception:
            continue
        et = event.get("type")
        if et == "thread.started":
            thread_id = event.get("thread_id") or thread_id
        elif et == "item.completed":
            item = event.get("item") or {}
            it = item.get("type")
            if it == "agent_message" and item.get("text"):
                text = str(item["text"])
            elif it in {"mcp_tool_call", "tool_call"}:
                name = item.get("tool") or item.get("name")
                if name:
                    tools.append(str(name))
        elif et == "turn.completed":
            usage = event.get("usage") or usage
        elif et in {"turn.failed", "error"}:
            msg = event.get("error") or event.get("message")
            if msg:
                errors.append(str(msg)[:500])
    return {
        "text": text,
        "session_id": thread_id,
        "usage": usage if isinstance(usage, dict) else {},
        "tools_used": tools,
        "error": "; ".join(errors)[:1000] or None,
    }


async def reason(prompt: str, *, role: str = "pm", model: str | None = None,
                 system: str | None = None, append_system: str | None = None,
                 allowed_tools: list[str] | None = None,
                 add_dirs: list[str] | None = None,
                 max_turns: int | None = None, cwd: str | None = None,
                 arm: bool = False, resume: str | None = None,
                 mcp_servers: dict | None = None,
                 log_run: bool = True, book: str | None = None,
                 seat: str | None = None,
                 record_book: str | None = None) -> dict:
    """Run one non-interactive Codex turn and return the cli_bridge contract."""
    del allowed_tools, add_dirs, resume, log_run, seat, record_book
    selected, effort = resolve_model(role, model)
    base = {
        "model": selected,
        "reasoning_effort": effort,
        "role": role,
        "armed": arm,
    }
    exe = codex_path()
    if not exe:
        return {**base, "ok": False, "backend": "none", "text": None,
                "error": "codex CLI not installed"}
    if not available():
        return {**base, "ok": False, "backend": "none", "text": None,
                "error": f"codex auth unavailable under {codex_home()}"}
    if arm and mcp_servers is None:
        from brain import bot_mcp
        mcp_servers = {bot_mcp.SERVER_NAME: bot_mcp.build_server()}

    python = os.environ.get("MASTERMIND_CODEX_PYTHON") or shutil.which("python") or "python"
    argv = [
        exe, "exec", "--ephemeral", "--json",
        "--model", selected,
        "-c", f"model_reasoning_effort={json.dumps(effort)}",
        "-c", 'approval_policy="never"',
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "--color", "never",
        "-C", str(Path(cwd or _ROOT).resolve()),
    ]
    if _truthy("MASTERMIND_CODEX_WEB_SEARCH"):
        argv += ["-c", 'web_search="live"']
    argv += _mcp_overrides(mcp_servers, book=book, python=python)
    argv.append("-")

    payload = _compose_prompt(
        prompt, system=system, append_system=append_system, max_turns=max_turns
    ).encode()
    timeout = max(60, int(os.environ.get("MASTERMIND_CODEX_TIMEOUT_SEC", "1800")))
    started = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(payload), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return {**base, "ok": False, "backend": "codex", "text": None,
                "error": f"codex timeout after {timeout}s"}
    except Exception as exc:
        return {**base, "ok": False, "backend": "codex", "text": None,
                "error": repr(exc)[:1000]}

    parsed = _parse_jsonl(stdout_b.decode(errors="replace"))
    stderr = stderr_b.decode(errors="replace").strip()
    error = parsed["error"]
    if proc.returncode != 0 and not error:
        error = stderr[-1000:] or f"codex exited {proc.returncode}"
    text = parsed["text"]
    return {
        **base,
        "ok": proc.returncode == 0 and bool(text),
        "text": text,
        "tools_used": parsed["tools_used"],
        "cost_usd": None,
        "session_id": parsed["session_id"],
        "usage": parsed["usage"],
        "backend": "codex",
        "error": error if (proc.returncode != 0 or not text) else None,
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


def reason_sync(prompt: str, **kw) -> dict:
    return asyncio.run(reason(prompt, **kw))
