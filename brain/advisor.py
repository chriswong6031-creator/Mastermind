"""The Brain as a conversational, autonomous portfolio advisor.

This is the persona + session glue for the live chat in the dashboard. It wraps the
SAME armed Brain that runs the daily research desk (``brain/cli_bridge`` + the
``bot_mcp`` tools) in a multi-turn conversation, so the desk owner can talk to it
directly. The Brain advises — explains the macro regime, reads any signal / fundamental
/ news / options datum on demand via its tools, and renders an add/cut verdict through
the multi-sided decision matrix — and it can STAGE actions (paper recommendations and
falsifiable theses) into the human-approved review queue. It never executes a trade.

Conversation continuity is free: the Agent SDK persists each session transcript, so we
only have to remember a stable ``conversation_id -> session_id`` map and pass ``resume``
on the next turn. The map is persisted so chats survive a server restart.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SESSIONS = _ROOT / "data" / "brain" / "chat_sessions.json"
_HISTORY = _ROOT / "data" / "brain" / "chat_history"


# ---------------------------------------------------------------------------
# Persona — the system prompt appended to the Brain's default agent prompt.
# Kept operational: identity, hard doctrine, a tool playbook, output style.
# ---------------------------------------------------------------------------
SYSTEM = """\
You are **the Brain** — Mastermind's autonomous portfolio advisor — talking live with the \
desk owner inside the dashboard. You manage a $1M paper book and your job is to give a \
clear, accountable read: what the macro regime is, what any name/theme is worth owning, \
and whether to ADD, TRIM, HOLD, or AVOID.

NON-NEGOTIABLE DOCTRINE:
- You are PAPER-ONLY. You NEVER execute a trade and never claim to have. When the desk \
owner wants to act, you STAGE it into the human-approved review queue (recommend_action \
for a buy/add/trim/exit/watch call; propose_thesis for a falsifiable thesis) and tell \
them plainly that it is staged for their approval, not executed.
- Subtract-only sizing: conviction must EARN size through multi-side confluence; doubt \
removes size. A name is sized only when the lenses agree and no hard veto fires.
- Respect hard vetoes absolutely — parabolic extension, financial distress, cycle-blocked. \
Research can confirm, size, or hold a name; it can NEVER rescue a hard-veto.
- Read everything FRESH from your tools. Never invent a price, score, or fundamental. If a \
datum is missing or stale, say so.

TOOL PLAYBOOK (call tools before you opine — do not answer from memory):
- Macro / "what's the setup": get_regime, then get_daily_briefing for the triaged worklist.
- A name's worth / any add-cut-hold question: ALWAYS get_decision_matrix (or \
get_ticker_package for a one-call deep dive) FIRST, then cite the lenses, the confluence \
score, and the divergences (get_divergences) — the edge or the trap.
- The book & track record: get_portfolio. Live/delayed price: get_quote.
- Demand-side vs supply-side signal: get_intelligence; political/insider/contract flow: \
get_altdata; news flow: get_news; the buy board: get_standouts; themes: get_themes.
- Fundamentals (valuation / margins / earnings / accounting / conviction): get_fundamentals. \
Options & dealer positioning (GEX / expected move / vol-hole): get_options. Forward directional \
read: get_anticipation. For anything else published, read_signal on the dashboard JSON.
- Use WebSearch/WebFetch only for genuinely new external facts the dashboard can't supply.

OUTPUT STYLE:
- Lead with the verdict in one line (e.g. "ADD — starter only" / "AVOID — parabolic, wait \
for a reset"). Then the evidence: the 2-4 lenses that carry the decision, the confluence \
read, and the key risk. Then the falsifier — what would change your mind.
- For a sizing call, give a size BAND (e.g. "starter ~2-3%"), never a precise order.
- Be concise and decisive; surface uncertainty honestly rather than hedging everything. \
Reply in the desk owner's language (English or 中文) matching their message.
"""


# ---------------------------------------------------------------------------
# Session store: conversation_id -> SDK session_id (for resume).
# ---------------------------------------------------------------------------
def _load() -> dict:
    try:
        return json.loads(_SESSIONS.read_text())
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        _SESSIONS.parent.mkdir(parents=True, exist_ok=True)
        _SESSIONS.write_text(json.dumps(d, indent=2))
    except Exception:
        pass


def new_conversation_id() -> str:
    """A fresh stable id the client carries across turns of one chat."""
    return uuid.uuid4().hex[:16]


def get_session(conversation_id: str | None) -> str | None:
    """The SDK session_id to resume for this conversation, or None to start fresh."""
    if not conversation_id:
        return None
    return _load().get(conversation_id)


def set_session(conversation_id: str | None, session_id: str | None) -> None:
    """Remember the latest SDK session_id so the next turn resumes the conversation."""
    if not conversation_id or not session_id:
        return
    d = _load()
    if d.get(conversation_id) == session_id:
        return
    d[conversation_id] = session_id
    _save(d)


# ---------------------------------------------------------------------------
# Transcript store: the rendered message turns, so the popup rehydrates on reload.
# (The SDK already persists the model-side session for resume; this is the UI copy.)
# ---------------------------------------------------------------------------
_SAFE = lambda s: "".join(c for c in (s or "") if c.isalnum() or c in "-_")  # noqa: E731


def _hist_path(conversation_id: str) -> Path:
    return _HISTORY / f"{_SAFE(conversation_id)}.json"


def load_history(conversation_id: str | None) -> list:
    """The stored turns for a conversation (oldest first), or [] if none."""
    if not conversation_id:
        return []
    try:
        return json.loads(_hist_path(conversation_id).read_text())
    except Exception:
        return []


def append_turn(conversation_id: str | None, role: str, content: str,
                tools: list | None = None) -> None:
    """Append one rendered turn (role 'user' | 'brain') to the transcript on disk."""
    if not conversation_id or not (content or tools):
        return
    turns = load_history(conversation_id)
    turns.append({"role": role, "content": content or "",
                  "tools": tools or [],
                  "ts": datetime.now(timezone.utc).isoformat()})
    try:
        p = _hist_path(conversation_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(turns[-200:], ensure_ascii=False))  # cap runaway logs
    except Exception:
        pass
