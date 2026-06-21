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
    assert "no real money" in s              # paper-only safety
    assert "decision_matrix" in s            # tool playbook present
    # the evaluate -> research-paper -> trade protocol
    assert "evaluate_gate" in s and "file_research_paper" in s and "execute_trade" in s


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


def test_chat_stream_emits_paper_event(monkeypatch):
    from brain import bot_mcp
    import json as _json

    class _Blk:
        def __init__(self, **k):
            self.__dict__.update(k)

    class _Msg:
        def __init__(self, blocks):
            self.content = blocks

    class _Res:
        def __init__(self):
            self.result = "done"
            self.session_id = "sid"
            self.total_cost_usd = 0.0

    meta = {"paper_id": "2026-06-21-NVDA", "ticker": "NVDA", "combined": 71, "confirmed": True}

    async def fake_query(*, prompt, options):
        yield _Msg([_Blk(type="tool_use", name="mcp__bot__file_research_paper", input={"ticker": "NVDA"})])
        yield _Msg([_Blk(type="tool_result", content=bot_mcp.PAPER_MARKER + " " + _json.dumps(meta))])
        yield _Res()

    monkeypatch.setattr(cli_bridge, "_SDK", True)
    monkeypatch.setattr(cli_bridge, "_sdk_query", fake_query)
    monkeypatch.setattr(cli_bridge, "cli_path", lambda: "/usr/bin/claude")
    evs = _drain(cli_bridge.chat_stream("add NVDA"))
    paper = next(e for e in evs if e["type"] == "paper")
    assert paper["ticker"] == "NVDA" and paper["confirmed"] is True and paper["combined"] == 71


def test_execute_fill_never_touches_other_positions(tmp_path, monkeypatch):
    import json as _json
    from portfolio import paper_account as pa
    monkeypatch.setattr(pa, "_DATA", tmp_path)
    monkeypatch.setattr(pa, "_ACCOUNT_PATH", tmp_path / "account.json")
    monkeypatch.setattr(pa, "_FILLS_PATH", tmp_path / "fills.jsonl")
    (tmp_path / "account.json").write_text(_json.dumps({
        "inception_date": "2026-06-20", "starting_nav": 1_000_000.0, "cash": 500_000.0,
        "positions": {"XLK": {"shares": 1000, "avg_cost": 200.0}}, "spy_shares": None}))
    r = pa.execute_fill("NVDA", "buy", weight=0.03, price=100.0, asof="2026-06-21")
    acct = _json.loads((tmp_path / "account.json").read_text())
    assert r["ok"] and "NVDA" in acct["positions"]
    assert acct["positions"]["XLK"]["shares"] == 1000          # untouched
    assert acct["cash"] < 500_000.0
    # exit closes the single name only
    pa.execute_fill("NVDA", "sell", price=110.0, asof="2026-06-21")
    acct = _json.loads((tmp_path / "account.json").read_text())
    assert "NVDA" not in acct["positions"] and acct["positions"]["XLK"]["shares"] == 1000
    fills = [l for l in (tmp_path / "fills.jsonl").read_text().splitlines() if l.strip()]
    assert len(fills) == 2
    # selling a name we don't hold is refused, not an exception
    assert pa.execute_fill("ZZZZ", "sell", price=5.0)["ok"] is False


def test_advisor_trade_records_single_ledger_key(tmp_path, monkeypatch):
    import json as _json
    from portfolio import paper_account as pa, position_log as pl, advisor_trade
    monkeypatch.setattr(pa, "_DATA", tmp_path)
    monkeypatch.setattr(pa, "_ACCOUNT_PATH", tmp_path / "account.json")
    monkeypatch.setattr(pa, "_FILLS_PATH", tmp_path / "fills.jsonl")
    monkeypatch.setattr(pl, "_LEDGER_PATH", tmp_path / "ledger.json")
    res = advisor_trade.execute("AAPL", "add", weight=0.02, price=50.0, asof="2026-06-21")
    assert res["ok"] and res["side"] == "buy"
    led = _json.loads((tmp_path / "ledger.json").read_text())
    assert list(led.keys()) == ["advisor:AAPL"]                # only one key touched
    assert any(o["ticker"] == "AAPL" for o in pl.open_positions())
    advisor_trade.execute("AAPL", "exit", price=55.0, asof="2026-06-22")
    assert not any(o["ticker"] == "AAPL" for o in pl.open_positions())


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
