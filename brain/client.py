"""The brain's LLM client — two backends, one contract.

Default backend = 'cli': drive the locally-installed Claude Code via brain/cli_bridge
(subscription tokens, sees the dashboard context, tiered subagents). Fallback backend =
'api': the metered Anthropic Messages API (needs ANTHROPIC_API_KEY).

Either way `call_model()` returns (text|None, degraded_reason|None) — the same contract as
master_brain._call_model. When neither backend can run, it returns (None, reason) so the
pipeline degrades to the deterministic, engine-derived path: the falsifier and sizing never
depend on the LLM.
"""
from __future__ import annotations

import os

from brain import cli_bridge

TIERS = {
    "pm": {"model": "claude-opus-4-8", "effort": "high"},
    "analyst": {"model": "claude-haiku-4-5", "effort": "low"},
    # deep → opus: matches config/agents.yml roles.deep and the API's expected behaviour.
    # brain.yml previously listed claude-fable-5 here; reconciled to opus so the CLI and
    # API backends agree that role='deep' always resolves to the opus tier.
    "deep": {"model": "claude-opus-4-8", "effort": "high"},
}


def backend() -> str:
    """'cli' (Claude Code) | 'api' (Messages API). Env override > config/agents.yml > 'cli'."""
    env = os.environ.get("BOT_LLM_BACKEND")
    if env in ("cli", "api"):
        return env
    try:
        return cli_bridge._cfg().get("backend", "cli")
    except Exception:
        return "cli"


def api_available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def available() -> bool:
    """Can we reason at all (either backend)?"""
    return cli_bridge.available() or api_available()


def call_model(system: str, user: str, *, role: str = "pm", max_tokens: int = 1500):
    """Return (text|None, degraded_reason|None). Routes CLI-first, then the Messages API."""
    if backend() == "cli" and cli_bridge.available():
        try:
            r = cli_bridge.reason_sync(user, role=role, append_system=system)
            if r.get("ok") and r.get("text"):
                return r["text"], None
            return None, (r.get("error") or "cli_empty")
        except Exception:
            pass  # fall through to the API backend

    if not api_available():
        return None, "no_backend"
    import anthropic
    t = TIERS[role]
    try:
        resp = anthropic.Anthropic().messages.create(
            model=t["model"], max_tokens=max_tokens,
            system=system, messages=[{"role": "user", "content": user}])
        if getattr(resp, "stop_reason", None) == "refusal":
            return None, "stop_refusal"
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        return (text or None), (None if text else "empty_reply")
    except Exception:
        return None, "llm_error"
