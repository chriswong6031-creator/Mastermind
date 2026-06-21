"""Advisor chat: persona + session glue + the SSE streaming transform.

These run WITHOUT a Claude credential — the live SDK call is monkeypatched with a fake
message stream so we can assert that cli_bridge.chat_stream maps SDK messages
(AssistantMessage text/tool_use blocks, ResultMessage) to the right SSE event dicts.
"""
from __future__ import annotations

import asyncio

from brain import advisor, cli_bridge


# --------------------------------------------------------------------------- #
# persona + session store
# --------------------------------------------------------------------------- #
def test_persona_encodes_doctrine():
    s = advisor.SYSTEM.lower()
    assert "the brain" in s
    assert "paper-only" in s                 # never executes
    assert "decision_matrix" in s            # tool playbook present
    assert "review queue" in s               # staging, not executing


def test_session_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(advisor, "_SESSIONS", tmp_path / "chat_sessions.json")
    cid = advisor.new_conversation_id()
    assert advisor.get_session(cid) is None
    advisor.set_session(cid, "sess-1")
    assert advisor.get_session(cid) == "sess-1"
    advisor.set_session(cid, None)           # no-op: never clobber with empty
    assert advisor.get_session(cid) == "sess-1"
    advisor.set_session(cid, "sess-2")       # next turn updates the resume token
    assert advisor.get_session(cid) == "sess-2"


# --------------------------------------------------------------------------- #
# the streaming transform
# --------------------------------------------------------------------------- #
class _Block:
    def __init__(self, type, text=None, name=None, input=None):
        self.type = type
        if text is not None:
            self.text = text
        if name is not None:
            self.name = name
        if input is not None:
            self.input = input


class _Assistant:                            # has .content, no .result
    def __init__(self, content):
        self.content = content


class _Result:                               # has .result -> end of turn
    def __init__(self, result, session_id, cost):
        self.result = result
        self.session_id = session_id
        self.total_cost_usd = cost


def _drain(agen):
    async def run():
        return [ev async for ev in agen]
    return asyncio.run(run())


def test_chat_stream_maps_messages_to_events(monkeypatch):
    async def fake_query(*, prompt, options):
        # the advisor persona must be wired onto the SDK options
        assert options.append_system_prompt and "the Brain" in options.append_system_prompt
        yield _Assistant([
            _Block("text", text="Regime is Goldilocks."),
            _Block("tool_use", name="mcp__bot__get_regime", input={}),
        ])
        yield _Result("final", "sess-xyz", 0.012)

    monkeypatch.setattr(cli_bridge, "_SDK", True)
    monkeypatch.setattr(cli_bridge, "_sdk_query", fake_query)
    monkeypatch.setattr(cli_bridge, "cli_path", lambda: "/usr/bin/claude")

    evs = _drain(cli_bridge.chat_stream("hi", append_system=advisor.SYSTEM))
    types = [e["type"] for e in evs]
    assert "text" in types and "tool" in types
    assert types[-1] == "done"
    assert next(e for e in evs if e["type"] == "text")["text"] == "Regime is Goldilocks."
    assert next(e for e in evs if e["type"] == "tool")["name"] == "mcp__bot__get_regime"
    done = evs[-1]
    assert done["session_id"] == "sess-xyz"
    assert done["tools_used"] == ["mcp__bot__get_regime"]


def test_chat_stream_surfaces_sdk_exception(monkeypatch):
    async def boom(*, prompt, options):
        if False:
            yield {}
        raise RuntimeError("cli exploded")

    monkeypatch.setattr(cli_bridge, "_SDK", True)
    monkeypatch.setattr(cli_bridge, "_sdk_query", boom)
    monkeypatch.setattr(cli_bridge, "cli_path", lambda: "/usr/bin/claude")

    evs = _drain(cli_bridge.chat_stream("hi"))
    assert evs[-1]["type"] == "error"
    assert "cli exploded" in evs[-1]["error"]


def test_chat_stream_errors_without_sdk(monkeypatch):
    monkeypatch.setattr(cli_bridge, "_SDK", False)
    evs = _drain(cli_bridge.chat_stream("hi"))
    assert len(evs) == 1 and evs[0]["type"] == "error"


# --------------------------------------------------------------------------- #
# Phase 3: typed convenience tools + transcript persistence
# --------------------------------------------------------------------------- #
def test_typed_tools_registered_and_armed():
    from brain import bot_mcp
    for nm in ("get_fundamentals", "get_options", "get_anticipation"):
        full = "mcp__bot__" + nm
        assert full in bot_mcp.TOOL_NAMES
        assert full in bot_mcp.armed_allowed_tools()


def test_history_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(advisor, "_HISTORY", tmp_path)
    cid = advisor.new_conversation_id()
    assert advisor.load_history(cid) == []
    advisor.append_turn(cid, "user", "should we add NVDA?")
    advisor.append_turn(cid, "brain", "ADD — starter only.",
                        [{"name": "mcp__bot__get_decision_matrix", "args": {"subject": "NVDA"}}])
    advisor.append_turn(cid, "brain", "", None)            # empty turn is a no-op
    h = advisor.load_history(cid)
    assert len(h) == 2
    assert h[0]["role"] == "user" and h[0]["content"] == "should we add NVDA?"
    assert h[1]["tools"][0]["name"].endswith("get_decision_matrix")
    assert advisor.load_history(None) == []                # missing id never throws
