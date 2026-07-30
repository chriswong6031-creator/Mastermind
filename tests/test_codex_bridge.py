from __future__ import annotations

import json
from pathlib import Path


def test_codex_role_mapping_is_sol_xhigh():
    from brain import codex_bridge

    for role in ("pm", "deep", "analyst", "scout", "fable"):
        assert codex_bridge.resolve_model(role) == ("gpt-5.6-sol", "xhigh")


def test_codex_jsonl_contract_parser():
    from brain.codex_bridge import _parse_jsonl

    raw = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
        json.dumps({
            "type": "item.completed",
            "item": {"type": "mcp_tool_call", "tool": "submit_book"},
        }),
        json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "done"},
        }),
        json.dumps({
            "type": "turn.completed",
            "usage": {"input_tokens": 12, "output_tokens": 3},
        }),
    ])
    parsed = _parse_jsonl(raw)
    assert parsed["text"] == "done"
    assert parsed["session_id"] == "thread-1"
    assert parsed["tools_used"] == ["submit_book"]
    assert parsed["usage"]["input_tokens"] == 12
    assert parsed["error"] is None


def test_codex_response_ledger_provider_attribution():
    from brain import thinking_log

    row = thinking_log.build_row(
        question="q",
        answer="a",
        model="gpt-5.6-sol",
        backend="codex",
    )
    assert row["provider"] == "openai_codex"


def test_mcp_overrides_rebuild_only_authorized_server_names():
    from brain.codex_bridge import _mcp_overrides

    args = _mcp_overrides(
        {"bot": {"type": "sdk"}, "desk": {"type": "sdk"}},
        book="autonomous",
        python="/venv/bin/python",
    )
    rendered = " ".join(args)
    assert "mcp_servers.bot.command" in rendered
    assert "mcp_servers.desk.command" in rendered
    assert "brain.codex_mcp_stdio" in rendered
    assert "autonomous" in rendered
    assert 'default_tools_approval_mode="approve"' in rendered


def test_codex_mcp_surface_matches_existing_book_surface():
    from brain.codex_mcp_stdio import server_instance

    bot = server_instance("autonomous", "bot")
    desk = server_instance("autonomous", "desk")
    assert bot.create_initialization_options().server_name == "bot"
    assert desk.create_initialization_options().server_name == "desk"


def test_codex_available_requires_cli_and_auth(tmp_path, monkeypatch):
    from brain import codex_bridge

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(codex_bridge.shutil, "which", lambda name: "/usr/bin/codex")
    assert not codex_bridge.available()
    (tmp_path / "auth.json").write_text("{}")
    assert codex_bridge.available()


def test_external_macro_plane_skips_git_refresh(monkeypatch):
    from data_layer import macro_refresh

    monkeypatch.setenv("MASTERMIND_MACRO_MANAGED_EXTERNALLY", "1")
    monkeypatch.setattr(
        macro_refresh,
        "refresh",
        lambda log=print: (_ for _ in ()).throw(AssertionError("must not refresh")),
    )
    monkeypatch.setattr(
        macro_refresh,
        "check_and_warn",
        lambda block=False, log=print: {"asof": "2026-07-29", "freeze": False},
    )
    out = macro_refresh.refresh_and_check(log=lambda _: None)
    assert out["asof"] == "2026-07-29"
    assert out["refreshed_to"] is None
