"""Shared Mastermind provider waterfall backed by Macro Dashboard state.

Mastermind and Macro run on the same VPS.  Macro owns provider discovery,
cooling, load ordering, and the admin-panel ledger; Mastermind owns its richer
typed MCP execution paths.  This adapter joins those two boundaries without
ever returning, logging, or persisting credential values.

Routing policy:
    1. Codex subscription (ChatGPT-managed auth), unless shared cooling says
       the account is currently exhausted.
    2. Claude OAuth slots allowed by Macro's ``brain-*`` capability lanes.
    3. Cooling providers as last-resort probes, in the same stable order.

Only authentication/quota failures advance from an available Codex provider
to Claude.  A non-provider execution failure is returned immediately to avoid
replaying a partially completed paper-tool turn.
"""
from __future__ import annotations

from typing import Any

import bot  # noqa: F401  # makes the vendored Macro engine importable

_CODEX_ID = "codex_account"


def _lane(role: str | None) -> str:
    return "brain-fast" if str(role or "").lower() in {"analyst", "scout"} else "brain-pro"


def _shared_modules():
    from engine import llm_auth
    from engine.neuralweb import key_pool

    return llm_auth, key_pool


def _local_cooling() -> dict[str, bool]:
    """Return Mastermind-local cooling flags keyed by capability id."""
    try:
        from brain import key_rotor

        return {
            str(c["key_id"]): bool(c.get("cooling"))
            for c in key_rotor.candidates()
            if c.get("key_id")
        }
    except Exception:
        return {}


def provider_rungs(role: str | None = None) -> list[dict[str, Any]]:
    """Return the active provider order using identifiers/env names only."""
    llm_auth, key_pool = _shared_modules()
    local_cool = _local_cooling()
    rungs: list[dict[str, Any]] = []

    try:
        from brain import codex_bridge

        if codex_bridge.available():
            rungs.append({
                "provider": "codex",
                "key_id": _CODEX_ID,
                "env_name": None,
                "cooling": bool(key_pool.is_cooling(_CODEX_ID)),
            })
    except Exception:
        pass

    try:
        for key_id, env_name in llm_auth._oauth_pool_candidates(_lane(role)):
            rungs.append({
                "provider": "oauth",
                "key_id": str(key_id),
                "env_name": str(env_name),
                "cooling": bool(
                    key_pool.is_cooling(str(key_id))
                    or local_cool.get(str(key_id), False)
                ),
            })
    except Exception:
        pass

    # Keep configured priority inside both groups; only active cooling pushes a
    # provider behind every healthy option.
    return sorted(rungs, key=lambda rung: bool(rung["cooling"]))


def available() -> bool:
    try:
        return bool(provider_rungs())
    except Exception:
        return False


def _failure_kind(error: object) -> str | None:
    """Classify only failures that authorize provider failover."""
    try:
        llm_auth, _ = _shared_modules()
        exc = RuntimeError(str(error or ""))
        if llm_auth._is_auth_error(exc):
            return "auth"
        if llm_auth._is_rate_limit_error(exc):
            msg = str(error or "").lower()
            return "weekly" if "weekly" in msg or "week limit" in msg else "window"
    except Exception:
        pass
    return None


def _usage_tokens(result: dict) -> int:
    usage = result.get("usage") or {}
    if not isinstance(usage, dict):
        return 0
    return int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)


def _note_codex(result: dict, *, role: str) -> None:
    """Mirror Codex health into Macro's shared admin/rotation ledger."""
    try:
        _, key_pool = _shared_modules()
        if result.get("ok"):
            key_pool.record_session(
                _CODEX_ID,
                est_tokens=_usage_tokens(result),
                cycle_id=str(result.get("run_id") or result.get("session_id") or ""),
                stage=f"mastermind:{role}",
                outcome="ok",
            )
            return
        kind = _failure_kind(result.get("error"))
        if kind:
            key_pool.mark_cooling(_CODEX_ID, cool_kind=kind)
    except Exception:
        pass


async def reason(prompt: str, **kwargs) -> dict:
    """Run one Mastermind turn through the shared Codex→Claude waterfall."""
    # Imported lazily to avoid a module cycle: cli_bridge dispatches here, and
    # this adapter deliberately reuses cli_bridge's audited provider execution.
    from brain import cli_bridge

    role = str(kwargs.get("role") or "pm")
    rungs = provider_rungs(role)
    attempts: list[dict[str, Any]] = []

    if not rungs:
        return {
            "ok": False,
            "text": None,
            "backend": "none",
            "provider": None,
            "shared_pool": True,
            "provider_attempts": attempts,
            "error": "no shared Codex or Claude OAuth provider is available",
        }

    last: dict | None = None
    for rung in rungs:
        if rung["provider"] == "codex":
            result = await cli_bridge._reason(
                prompt, **kwargs, _backend_override="codex"
            )
            _note_codex(result, role=role)
        else:
            candidate = {
                "key_id": rung["key_id"],
                "env_name": rung["env_name"],
                "cooling": rung["cooling"],
            }
            result = await cli_bridge._reason(
                prompt,
                **kwargs,
                _backend_override="cli",
                _oauth_candidates=[candidate],
            )

        result["provider"] = rung["provider"]
        result["shared_pool"] = True
        attempt = {
            "provider": rung["provider"],
            "key_id": rung["key_id"],
            "ok": bool(result.get("ok")),
            "cooling": bool(rung["cooling"]),
        }
        attempts.append(attempt)
        result["provider_attempts"] = list(attempts)
        last = result

        if result.get("ok") and result.get("text"):
            return result

        # Claude's own candidate execution is scoped to one slot here. Advance
        # only when it classified that slot as auth/quota-dead; do not replay a
        # potentially partial typed-tool turn after an unrelated failure.
        if rung["provider"] == "oauth":
            err = str(result.get("error") or "")
            if "all pool keys cooling/dead" in err or _failure_kind(err):
                continue
            return result

        # Codex only hands off on account/auth/quota exhaustion.  Other errors
        # may have occurred after typed tools ran, so replay would be unsafe.
        if _failure_kind(result.get("error")) is None:
            return result

    return last or {
        "ok": False,
        "text": None,
        "backend": "none",
        "provider": None,
        "shared_pool": True,
        "provider_attempts": attempts,
        "error": "shared provider waterfall exhausted",
    }
