"""The single Anthropic Messages-API client (LLM-optional).

Tiered: Opus 4.8 = PM/adjudicator, Haiku 4.5 = analyst fan-out, Fable 5 = gated.
Uses ANTHROPIC_API_KEY (never a subscription token). When the key/lib is absent it
returns (None, 'no_key') so the whole pipeline degrades to the deterministic,
engine-derived path — the falsifier and sizing never depend on the LLM.
"""
from __future__ import annotations

import os

TIERS = {
    "pm": {"model": "claude-opus-4-8", "effort": "high"},
    "analyst": {"model": "claude-haiku-4-5", "effort": "low"},
    "deep": {"model": "claude-fable-5", "effort": "high"},
}


def available() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


def call_model(system: str, user: str, *, role: str = "pm", max_tokens: int = 1500):
    """Return (text|None, degraded_reason|None) — mirrors master_brain._call_model's contract."""
    if not available():
        return None, "no_key"
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
