"""AI thinking-trace capture (surface "bot") — the bot extension of the macro
response-log program (macro PR #3781).

Covers, WITHOUT any Claude credential (SDK calls are monkeypatched):
  1. the mirrored `mastermind.response_log.v1` row/sanitizer contract in
     brain/thinking_log (caps, FIRST-(N-1)+LAST truncation, redacted segments,
     surface "bot" + additive attribution keys, local sink layout, kill-switch);
  2. SDK-path capture through cli_bridge.reason(): ThinkingBlock/RedactedThinkingBlock
     content becomes ledger segments (tool rounds vs final synthesis) while the
     RETURNED result dict stays thinking-free (the leak law);
  3. seat/book attribution parity with the cost contract (book= / record_book=);
  4. chat_stream: thinking is captured to the ledger but NEVER yielded as an event;
  5. the subprocess backend logs the turn with thinking=[] (no blocks available);
  6. the Messages-API fallback helper (brain/client._log_api_turn).

All sinks are pointed at tmp_path via MASTERMIND_BOT_RESPONSE_LOG_DIR and the async
writer is de-threaded (log_turn_async → log_turn) so assertions are race-free.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from brain import cli_bridge, thinking_log


# --------------------------------------------------------------------------- #
# doubles — class NAMES matter (_block_kind keys off type(b).__name__)
# --------------------------------------------------------------------------- #
class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, name, input=None, id="t1"):
        self.id, self.name, self.input = id, name, input or {}


class ThinkingBlock:
    def __init__(self, thinking):
        self.thinking = thinking


class RedactedThinkingBlock:
    def __init__(self):
        self.data = "opaque"


class _Assistant:              # has .content, no .result
    def __init__(self, content):
        self.content = content


class _Result:                 # has .result -> end of turn
    def __init__(self, result, session_id="sess-t", cost=0.01, usage=None):
        self.result = result
        self.session_id = session_id
        self.total_cost_usd = cost
        self.usage = usage or {"input_tokens": 11, "output_tokens": 7}


def _drain(agen):
    async def run():
        return [ev async for ev in agen]
    return asyncio.run(run())


@pytest.fixture
def ledger_dir(tmp_path, monkeypatch):
    """Point the bot response ledger at tmp_path and make writes synchronous."""
    d = tmp_path / "response_logs"
    monkeypatch.setenv("MASTERMIND_BOT_RESPONSE_LOG_DIR", str(d))
    monkeypatch.delenv("MASTERMIND_RESPONSE_LOG_DISABLED", raising=False)
    monkeypatch.delenv("MASTERMIND_BOT_RESPONSE_LOG_DISABLED", raising=False)
    # No R2 in tests — ensure the local mirror is the only sink.
    for k in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(thinking_log, "log_turn_async", thinking_log.log_turn)
    return d


def _rows(d):
    return [json.loads(p.read_text()) for p in sorted(d.rglob("*.json"))]


# --------------------------------------------------------------------------- #
# 1. the mirrored row/sanitizer contract
# --------------------------------------------------------------------------- #
class TestRowContract:
    def test_row_shape_surface_bot_plus_additive_keys(self):
        row = thinking_log.build_row(
            question="q", answer="a", model="claude-opus-4-8",
            seat="sentinel", book="flagship", role="analyst", mode="research",
            backend="sdk", armed=True, run_id="r1", key_id="k1", thread_id="s1",
            latency_ms=1234, input_tokens=10, output_tokens=5,
            tools=["Read"], thinking=[{"round": 1, "phase": "synthesis",
                                       "model": "m", "text": "because"}],
        )
        assert row["schema"] == "mastermind.response_log.v1"
        assert row["surface"] == "bot"
        assert row["user_ref"] == "bot"
        assert row["lane"] is None and row["citations"] == []
        # additive bot keys the admin preserves
        assert (row["seat"], row["book"], row["role"]) == ("sentinel", "flagship", "analyst")
        assert row["armed"] is True and row["backend"] == "sdk"
        assert (row["run_id"], row["key_id"]) == ("r1", "k1")
        assert row["provider"] == "claude_code"          # sdk backend
        assert row["thinking"] == [{"round": 1, "phase": "synthesis",
                                    "model": "m", "text": "because"}]

    def test_thinking_truncation_keeps_first_n1_plus_last(self):
        segs = [{"round": i, "phase": "tool", "model": "m", "text": f"t{i}"}
                for i in range(1, 31)]
        segs[-1]["phase"] = "synthesis"
        out = thinking_log._clean_thinking(segs)
        assert len(out) == thinking_log._THINKING_MAX_SEGMENTS
        assert out[-1]["phase"] == "synthesis" and out[-1]["text"] == "t30"
        assert [s["text"] for s in out[:-1]] == [f"t{i}" for i in range(1, 24)]

    def test_segment_text_cap_and_redacted_rules(self):
        long = "x" * (thinking_log._THINKING_TEXT_CAP + 100)
        out = thinking_log._clean_thinking([
            {"round": 1, "phase": "tool", "model": "m", "text": long},
            {"round": 2, "phase": "tool", "model": "m", "text": ""},                     # dropped
            {"round": 3, "phase": "tool", "model": "m", "text": "", "redacted": True},   # kept
            "not-a-dict",                                                                # dropped
        ])
        assert len(out) == 2
        assert out[0]["text"].endswith(" …[truncated]")
        assert out[1] == {"round": 3, "phase": "tool", "model": "m",
                          "text": "", "redacted": True}

    def test_local_sink_layout_and_kill_switch(self, ledger_dir, monkeypatch):
        assert thinking_log.log_turn(question="q", answer="a", model="m") is True
        rows = _rows(ledger_dir)
        assert len(rows) == 1
        p = next(ledger_dir.rglob("*.json"))
        # <dir>/bot/<YYYY-MM-DD>/<id>.json — same layout as the R2 prefix
        assert p.parent.parent.name == "bot"
        assert rows[0]["id"] in p.name
        monkeypatch.setenv("MASTERMIND_BOT_RESPONSE_LOG_DISABLED", "1")
        assert thinking_log.log_turn(question="q2", answer="a2", model="m") is False
        assert len(_rows(ledger_dir)) == 1


# --------------------------------------------------------------------------- #
# 2+3. SDK capture through reason(): segments to the ledger, none in the result
# --------------------------------------------------------------------------- #
def _isolate_side_ledgers(tmp_path, monkeypatch):
    """Keep reason()'s other writers (runlog, cost_guard, key pool) out of data/."""
    from brain import runlog, cost_guard, key_rotor
    monkeypatch.setattr(runlog, "_RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runlog, "_INDEX", tmp_path / "runs" / "index.jsonl")
    monkeypatch.setattr(cost_guard, "_DIR", tmp_path / "cost", raising=False)
    monkeypatch.setattr(cost_guard, "_ROOT", tmp_path, raising=False)
    monkeypatch.setattr(key_rotor, "candidates", lambda *a, **k: [])


def _fake_query_with_thinking():
    async def fake_query(*, prompt, options):
        yield _Assistant([
            ThinkingBlock("round-1 reasoning: signals disagree"),
            TextBlock("Checking the tape."),
            ToolUseBlock("mcp__bot__get_regime", {}),
        ])
        yield _Assistant([
            ThinkingBlock("synthesis: divergence is real, not a data bug"),
            RedactedThinkingBlock(),
            TextBlock("Final answer."),
        ])
        yield _Result("Final answer.")
    return fake_query


class TestReasonCapture:
    def test_sdk_thinking_reaches_ledger_not_result(self, ledger_dir, tmp_path, monkeypatch):
        _isolate_side_ledgers(tmp_path, monkeypatch)
        monkeypatch.setattr(cli_bridge, "_SDK", True)
        monkeypatch.setattr(cli_bridge, "_sdk_query", _fake_query_with_thinking())
        monkeypatch.setattr(cli_bridge, "cli_path", lambda: "/usr/bin/claude")

        result = asyncio.run(cli_bridge.reason("are utilities and yields contradicting?"))

        # LEAK LAW: the returned dict must not expose the trace to decision paths.
        assert "thinking" not in result
        assert result["ok"] is True

        rows = _rows(ledger_dir)
        assert len(rows) == 1
        row = rows[0]
        assert row["surface"] == "bot" and row["backend"] == "sdk"
        assert row["question"].startswith("are utilities")
        assert row["answer"] == "Final answer."
        segs = row["thinking"]
        assert [s["phase"] for s in segs] == ["tool", "synthesis", "synthesis"]
        assert segs[0]["text"].startswith("round-1 reasoning")
        assert segs[1]["text"].startswith("synthesis: divergence")
        assert segs[2] == {"round": 2, "phase": "synthesis",
                           "model": row["model"], "text": "", "redacted": True}
        # default attribution: role pm → seat "pm", book "flagship" (mirrors _ROLE_BOOK)
        assert row["seat"] == "pm" and row["book"] == "flagship"
        assert row["run_id"] and row["thread_id"] == "sess-t"

    def test_attribution_overrides_mirror_cost_contract(self, ledger_dir, tmp_path, monkeypatch):
        _isolate_side_ledgers(tmp_path, monkeypatch)
        monkeypatch.setattr(cli_bridge, "_SDK", True)
        monkeypatch.setattr(cli_bridge, "_sdk_query", _fake_query_with_thinking())
        monkeypatch.setattr(cli_bridge, "cli_path", lambda: "/usr/bin/claude")

        # sentinel pattern: analyst role, named seat, record_book override
        asyncio.run(cli_bridge.reason("q1", role="analyst", seat="sentinel",
                                      record_book="flagship"))
        # per-book caller pattern: book= (caller records cost; the LOG still lands here)
        asyncio.run(cli_bridge.reason("q2", role="deep", seat="etf_brain", book="etf"))

        rows = sorted(_rows(ledger_dir), key=lambda r: r["question"])
        assert (rows[0]["seat"], rows[0]["book"], rows[0]["role"]) == ("sentinel", "flagship", "analyst")
        assert (rows[1]["seat"], rows[1]["book"]) == ("etf_brain", "etf")

    def test_log_run_false_stays_out_of_corpus(self, ledger_dir, tmp_path, monkeypatch):
        _isolate_side_ledgers(tmp_path, monkeypatch)
        monkeypatch.setattr(cli_bridge, "_SDK", True)
        monkeypatch.setattr(cli_bridge, "_sdk_query", _fake_query_with_thinking())
        monkeypatch.setattr(cli_bridge, "cli_path", lambda: "/usr/bin/claude")
        asyncio.run(cli_bridge.reason("bulk translate", log_run=False))
        assert _rows(ledger_dir) == []

    def test_subprocess_backend_logs_turn_with_empty_thinking(self, ledger_dir, tmp_path, monkeypatch):
        _isolate_side_ledgers(tmp_path, monkeypatch)
        monkeypatch.setattr(cli_bridge, "_SDK", False)
        monkeypatch.setattr(cli_bridge, "cli_path", lambda: "/usr/bin/claude")

        async def fake_sub(prompt, mdl, role, system, append_system, tools, dirs, turns,
                           workdir, perm, base, env_name=None):
            return {**base, "ok": True, "backend": "cli", "text": "cli answer",
                    "cost_usd": 0.01, "session_id": "s-cli",
                    "usage": {"input_tokens": 3, "output_tokens": 2}, "error": None}

        monkeypatch.setattr(cli_bridge, "_via_subprocess", fake_sub)
        result = asyncio.run(cli_bridge.reason("q-cli"))
        assert result["backend"] == "cli" and "thinking" not in result
        rows = _rows(ledger_dir)
        assert len(rows) == 1
        assert rows[0]["thinking"] == [] and rows[0]["backend"] == "cli"


# --------------------------------------------------------------------------- #
# 4. chat_stream — captured for the ledger, never yielded (the leak law)
# --------------------------------------------------------------------------- #
class TestChatStreamLeakLaw:
    def test_thinking_never_streams_but_lands_in_ledger(self, ledger_dir, monkeypatch):
        async def fake_query(*, prompt, options):
            yield _Assistant([
                ThinkingBlock("private: the boards contradict each other"),
                TextBlock("Here's the picture."),
                ToolUseBlock("mcp__bot__get_regime", {}),
            ])
            yield _Assistant([
                ThinkingBlock("private synthesis"),
                TextBlock(" More."),
            ])
            yield _Result("ignored-final")

        monkeypatch.setattr(cli_bridge, "_SDK", True)
        monkeypatch.setattr(cli_bridge, "_sdk_query", fake_query)
        monkeypatch.setattr(cli_bridge, "cli_path", lambda: "/usr/bin/claude")

        evs = _drain(cli_bridge.chat_stream("what do you see?"))

        # No event of any type may carry thinking text — the SSE wire stays clean.
        blob = json.dumps(evs)
        assert "private:" not in blob and "private synthesis" not in blob
        assert all(ev["type"] in ("text", "tool", "tool_result", "paper", "done", "error")
                   for ev in evs)
        assert evs[-1]["type"] == "done"

        rows = _rows(ledger_dir)
        assert len(rows) == 1
        row = rows[0]
        assert row["seat"] == "advisor_chat" and row["mode"] == "chat"
        assert row["book"] == "system" and row["surface"] == "bot"
        assert row["answer"] == "Here's the picture. More."
        texts = [s["text"] for s in row["thinking"]]
        assert texts == ["private: the boards contradict each other", "private synthesis"]
        assert row["thinking"][-1]["phase"] == "synthesis"


# --------------------------------------------------------------------------- #
# 5. Messages-API fallback helper (brain/client)
# --------------------------------------------------------------------------- #
class TestApiFallbackCapture:
    def test_log_api_turn_extracts_thinking_blocks(self, ledger_dir):
        from brain import client as brain_client

        class _B:
            def __init__(self, type, **kw):
                self.type = type
                for k, v in kw.items():
                    setattr(self, k, v)

        class _Resp:
            content = [_B("thinking", thinking="api reasoning"),
                       _B("redacted_thinking"),
                       _B("text", text="api answer")]
            usage = {"input_tokens": 9, "output_tokens": 4}

        brain_client._log_api_turn(_Resp(), user="api q", role="analyst",
                                   model="claude-haiku-4-5", seat="risk_officer",
                                   record_book=None, text="api answer")
        rows = _rows(ledger_dir)
        assert len(rows) == 1
        row = rows[0]
        assert row["backend"] == "api" and row["provider"] == "claude_api"
        assert row["seat"] == "risk_officer"
        assert row["book"] == "system"            # _ROLE_BOOK: analyst → system
        assert [s["phase"] for s in row["thinking"]] == ["synthesis", "synthesis"]
        assert row["thinking"][0]["text"] == "api reasoning"
        assert row["thinking"][1]["redacted"] is True
