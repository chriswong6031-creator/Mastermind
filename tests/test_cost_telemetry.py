"""Tests for granular per-agent token/cost logging (Task: fable/ai-cost-telemetry).

Covers:
  1. cost_guard legacy + new-schema round-trip
  2. over_budget behaviour unchanged by new schema
  3. summary() return shape unaffected by new schema
  4. brain/client.py usage capture (mock anthropic)
  5. bot/etf.py cost_guard.record call with token fields
  6. cost_summary builder (30-day rolling summary, tmp dirs)
  7. estimate_cost pricing table
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import bot  # noqa: F401 — vendor/macro onto sys.path

CAP_ENV = "MASTERMIND_NIGHTLY_USD_CAP"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def cg(tmp_path, monkeypatch):
    """Fresh cost_guard whose ledger dir is redirected into tmp_path, cap OFF."""
    monkeypatch.delenv(CAP_ENV, raising=False)
    import brain.cost_guard as _cg
    _cg = importlib.reload(_cg)
    monkeypatch.setattr(_cg, "_DIR", tmp_path / "cost")
    monkeypatch.setattr(_cg, "_ROOT", tmp_path)
    return _cg


# ---------------------------------------------------------------------------
# 1. Legacy + new-schema round-trip
# ---------------------------------------------------------------------------

def test_record_new_schema_creates_dict(cg):
    """record() with token args creates a per-book dict in the ledger."""
    cg.record("flagship", 0.5, "2026-07-16",
               seat="strategist", model="claude-opus-4-8",
               input_tokens=1000, output_tokens=200)
    raw = json.loads(cg._path("2026-07-16").read_text())
    bk = raw["flagship"]
    assert isinstance(bk, dict)
    assert bk["usd"] == pytest.approx(0.5)
    assert bk["input_tokens"] == 1000
    assert bk["output_tokens"] == 200
    assert bk["calls"] == 1
    assert "strategist" in bk["seats"]
    seat = bk["seats"]["strategist"]
    assert seat["usd"] == pytest.approx(0.5)
    assert seat["calls"] == 1


def test_record_legacy_float_read_transparently(cg):
    """A legacy float-only file is read and spent() returns the float value."""
    p = cg._path("2026-07-10")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"autonomous": 1.25, "flagship": 0.75}))
    assert cg.spent("autonomous", "2026-07-10") == pytest.approx(1.25)
    assert cg.spent("flagship", "2026-07-10") == pytest.approx(0.75)


def test_record_upgrades_legacy_on_new_write(cg):
    """When record() touches a legacy-float file, the book entry is upgraded to dict."""
    p = cg._path("2026-07-10")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"autonomous": 1.00}))
    # add tokens to the same book
    cg.record("autonomous", 0.50, "2026-07-10",
               seat="autonomous_brain", input_tokens=500)
    raw = json.loads(p.read_text())
    bk = raw["autonomous"]
    assert isinstance(bk, dict)
    # original 1.00 + new 0.50 = 1.50
    assert bk["usd"] == pytest.approx(1.50)
    assert bk["input_tokens"] == 500


def test_record_accumulates_token_fields(cg):
    """Multiple record() calls accumulate all token fields."""
    cg.record("flagship", 0.5, "2026-07-16", seat="s1",
               input_tokens=1000, output_tokens=300,
               cache_read_tokens=50, cache_creation_tokens=20)
    cg.record("flagship", 0.3, "2026-07-16", seat="s2",
               input_tokens=400, output_tokens=100)
    bk = json.loads(cg._path("2026-07-16").read_text())["flagship"]
    assert bk["input_tokens"] == 1400
    assert bk["output_tokens"] == 400
    assert bk["cache_read_tokens"] == 50
    assert bk["cache_creation_tokens"] == 20
    assert bk["calls"] == 2


def test_record_tokens_only_no_usd(cg):
    """record() with no positive usd but non-zero tokens still writes token counts."""
    cg.record("flagship", None, "2026-07-16",
               input_tokens=100, output_tokens=50)
    bk = json.loads(cg._path("2026-07-16").read_text())["flagship"]
    assert bk["usd"] == 0.0
    assert bk["input_tokens"] == 100
    assert bk["output_tokens"] == 50
    assert bk["calls"] == 1


def test_record_zero_usd_zero_tokens_is_noop(cg):
    """record() with zero cost and zero tokens is silently ignored."""
    cg.record("flagship", 0, "2026-07-16")
    assert not cg._path("2026-07-16").exists()


# ---------------------------------------------------------------------------
# 2. over_budget — unchanged behaviour
# ---------------------------------------------------------------------------

def test_over_budget_false_when_cap_off_new_schema(cg):
    cg.record("flagship", 999.0, "2026-07-16",
               input_tokens=99999, output_tokens=9999)
    assert cg.over_budget("flagship", "2026-07-16") is False


def test_over_budget_true_at_cap_new_schema(cg, monkeypatch):
    monkeypatch.setenv(CAP_ENV, "1.0")
    cg.record("flagship", 1.0, "2026-07-16",
               input_tokens=1000, output_tokens=200)
    assert cg.over_budget("flagship", "2026-07-16") is True


def test_over_budget_independent_books_new_schema(cg, monkeypatch):
    monkeypatch.setenv(CAP_ENV, "2.0")
    cg.record("flagship", 2.5, "2026-07-16", input_tokens=100)
    cg.record("autonomous", 0.5, "2026-07-16", input_tokens=50)
    assert cg.over_budget("flagship", "2026-07-16") is True
    assert cg.over_budget("autonomous", "2026-07-16") is False


# ---------------------------------------------------------------------------
# 3. summary() shape — unaffected by new schema
# ---------------------------------------------------------------------------

def test_summary_shape_legacy_file(cg):
    """summary() returns the same shape for legacy float files."""
    p = cg._path("2026-07-10")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"autonomous": 1.5, "china": 0.5}))
    s = cg.summary("2026-07-10")
    assert s["asof"] == "2026-07-10"
    assert s["total"] == pytest.approx(2.0)
    assert s["books"]["autonomous"]["spent"] == pytest.approx(1.5)
    assert s["books"]["autonomous"]["over"] is False


def test_summary_shape_new_schema(cg, monkeypatch):
    """summary() returns the same contract after the new schema is written."""
    cg.record("flagship", 1.5, "2026-07-16", seat="s",
               input_tokens=1000, output_tokens=300)
    cg.record("autonomous", 0.5, "2026-07-16", seat="s2")
    s = cg.summary("2026-07-16")
    assert s["asof"] == "2026-07-16"
    assert s["total"] == pytest.approx(2.0)
    assert s["books"]["flagship"]["spent"] == pytest.approx(1.5)
    assert s["books"]["autonomous"]["spent"] == pytest.approx(0.5)
    # no cap set → over is always False
    assert s["books"]["flagship"]["over"] is False
    assert "cap" in s
    assert "enabled" in s


# ---------------------------------------------------------------------------
# 4. brain/client.py usage capture (mock anthropic)
# ---------------------------------------------------------------------------

def test_client_api_path_records_tokens(tmp_path, monkeypatch):
    """brain.client.call_model() records tokens via cost_guard when using API backend."""
    monkeypatch.setenv("BOT_LLM_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

    import brain.cost_guard as _cg
    _cg = importlib.reload(_cg)
    monkeypatch.setattr(_cg, "_DIR", tmp_path / "cost")
    monkeypatch.setattr(_cg, "_ROOT", tmp_path)

    # Patch anthropic.Anthropic so no real network call is made
    fake_usage = SimpleNamespace(
        input_tokens=500, output_tokens=100,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    fake_content = [SimpleNamespace(type="text", text="hello")]
    fake_resp = SimpleNamespace(
        stop_reason="end_turn",
        content=fake_content,
        usage=fake_usage,
        model="claude-opus-4-8",
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp

    import brain.client as _client
    with patch("anthropic.Anthropic", return_value=fake_client):
        text, err = _client.call_model("system prompt", "user message", role="pm")

    assert err is None
    assert text == "hello"

    # check tokens were recorded
    day = __import__("datetime").date.today().isoformat()
    s = _cg.summary(day)
    assert s["total"] > 0
    bk_data = json.loads(_cg._path(day).read_text())
    flagship_bk = bk_data.get("flagship") or {}
    assert isinstance(flagship_bk, dict)
    assert flagship_bk.get("input_tokens", 0) == 500
    assert flagship_bk.get("output_tokens", 0) == 100


def test_client_estimate_cost_pricing(cg):
    """estimate_cost returns expected USD for known models."""
    # claude-opus-4-8: 5.00/MTok input, 25.00/MTok output
    cost = cg.estimate_cost("claude-opus-4-8", 1_000_000, 1_000_000)
    assert cost == pytest.approx(30.0)  # 5+25

    # claude-haiku-4-5: 1.00/MTok input, 5.00/MTok output
    cost_h = cg.estimate_cost("claude-haiku-4-5", 1_000_000, 0)
    assert cost_h == pytest.approx(1.0)

    # unknown model → 0.0
    cost_u = cg.estimate_cost("unknown-model", 1_000_000, 1_000_000)
    assert cost_u == 0.0


def test_client_estimate_cost_cache(cg):
    """Cache read/write tokens are accounted for correctly."""
    # opus: input $5/MTok, cache_read $0.50/MTok, cache_write $6.25/MTok
    cost = cg.estimate_cost("claude-opus-4-8",
                             input_tokens=0, output_tokens=0,
                             cache_read_tokens=1_000_000,
                             cache_creation_tokens=0)
    assert cost == pytest.approx(0.50)
    cost2 = cg.estimate_cost("claude-opus-4-8",
                              input_tokens=0, output_tokens=0,
                              cache_read_tokens=0,
                              cache_creation_tokens=1_000_000)
    assert cost2 == pytest.approx(6.25)


# ---------------------------------------------------------------------------
# 5. bot/etf.py calls cost_guard.record with token fields
# ---------------------------------------------------------------------------

def test_etf_record_pattern_with_tokens(cg):
    """The record call pattern used in bot/etf.py (after _run_brain) passes token fields.

    This verifies the exact call site shape rather than running the full run_etf() pipeline
    (which would require all ETF infrastructure).
    """
    fake_brain = {
        "ok": True, "cost_usd": 1.23, "model": "claude-opus-4-8",
        "usage": {"input_tokens": 800, "output_tokens": 200,
                  "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10},
    }
    _usg = fake_brain.get("usage") or {}
    # This mirrors the exact code in bot/etf.py run_etf() armed block.
    cg.record(
        "etf",
        fake_brain.get("cost_usd"),
        "2026-07-16",
        seat="etf_brain",
        model=str(fake_brain.get("model") or ""),
        input_tokens=int(_usg.get("input_tokens") or 0),
        output_tokens=int(_usg.get("output_tokens") or 0),
        cache_read_tokens=int(_usg.get("cache_read_input_tokens") or 0),
        cache_creation_tokens=int(_usg.get("cache_creation_input_tokens") or 0),
    )
    raw = json.loads(cg._path("2026-07-16").read_text())
    bk = raw["etf"]
    assert bk["input_tokens"] == 800
    assert bk["output_tokens"] == 200
    assert bk["cache_read_tokens"] == 50
    assert bk["cache_creation_tokens"] == 10
    assert "etf_brain" in bk["seats"]
    assert bk["usd"] == pytest.approx(1.23)


# ---------------------------------------------------------------------------
# 6. cost_summary builder
# ---------------------------------------------------------------------------

def test_build_cost_summary_empty(cg):
    """build_cost_summary returns a valid empty dict when no cost files exist."""
    s = cg.build_cost_summary("2026-07-16")
    assert s["schema"] == "mastermind.cost_summary.v1"
    assert s["as_of"] == "2026-07-16"
    assert s["days"] == {}
    assert s["totals_30d"]["usd"] == 0.0
    assert s["totals_30d"]["by_book"] == {}
    assert s["totals_30d"]["by_seat"] == {}


def test_build_cost_summary_legacy_file(cg):
    """Legacy float-only files appear in the summary with usd-only (tokens=0)."""
    p = cg._path("2026-07-16")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"flagship": 1.5, "autonomous": 0.75}))
    s = cg.build_cost_summary("2026-07-16")
    day = s["days"].get("2026-07-16") or {}
    assert day["flagship"]["usd"] == pytest.approx(1.5)
    assert day["flagship"]["input_tokens"] == 0
    assert day["autonomous"]["usd"] == pytest.approx(0.75)
    assert s["totals_30d"]["usd"] == pytest.approx(2.25)
    assert s["totals_30d"]["by_book"]["flagship"] == pytest.approx(1.5)


def test_build_cost_summary_new_schema_with_seats(cg):
    """New-schema files appear in the summary with token fields and seat breakdown."""
    cg.record("flagship", 2.0, "2026-07-16",
               seat="strategist", model="claude-opus-4-8",
               input_tokens=2000, output_tokens=400)
    cg.record("flagship", 0.5, "2026-07-16",
               seat="gate_officer", model="claude-opus-4-8",
               input_tokens=500, output_tokens=100)
    cg.record("autonomous", 1.0, "2026-07-16",
               seat="autonomous_brain",
               input_tokens=1000, output_tokens=200)
    s = cg.build_cost_summary("2026-07-16")
    day = s["days"]["2026-07-16"]
    fl = day["flagship"]
    assert fl["usd"] == pytest.approx(2.5)
    assert fl["input_tokens"] == 2500
    assert "strategist" in fl["seats"]
    assert "gate_officer" in fl["seats"]
    tot = s["totals_30d"]
    assert tot["usd"] == pytest.approx(3.5)
    assert tot["by_book"]["flagship"] == pytest.approx(2.5)
    assert tot["by_seat"]["strategist"] == pytest.approx(2.0)
    assert tot["by_seat"]["gate_officer"] == pytest.approx(0.5)
    assert tot["by_seat"]["autonomous_brain"] == pytest.approx(1.0)


def test_build_cost_summary_30d_window(cg):
    """Files older than 30 days are excluded from the summary."""
    from datetime import date, timedelta
    today = date(2026, 7, 16)
    # write a file within window (29 days ago) and one outside (31 days ago)
    within = (today - timedelta(days=29)).isoformat()
    outside = (today - timedelta(days=31)).isoformat()
    cg.record("flagship", 1.0, within)
    cg.record("flagship", 5.0, outside)
    s = cg.build_cost_summary("2026-07-16")
    assert within in s["days"]
    assert outside not in s["days"]
    # total only includes within-window
    assert s["totals_30d"]["usd"] == pytest.approx(1.0)


def test_write_cost_summary(cg, tmp_path):
    """write_cost_summary() writes the JSON to the given path."""
    cg.record("flagship", 1.5, "2026-07-16", seat="s", input_tokens=500)
    dest = tmp_path / "out" / "cost_summary.json"
    p = cg.write_cost_summary(dest, asof="2026-07-16")
    assert p == dest
    assert dest.exists()
    loaded = json.loads(dest.read_text())
    assert loaded["schema"] == "mastermind.cost_summary.v1"
    assert loaded["totals_30d"]["usd"] == pytest.approx(1.5)


def test_build_cost_summary_never_raises(cg, monkeypatch):
    """build_cost_summary swallows exceptions and returns a valid stub."""
    # corrupt the _DIR so glob fails
    monkeypatch.setattr(cg, "_DIR", Path("/nonexistent_dir_xyz_9999999"))
    s = cg.build_cost_summary("2026-07-16")
    assert s["schema"] == "mastermind.cost_summary.v1"
    assert s["days"] == {}


# ---------------------------------------------------------------------------
# 8. cli_bridge double-count fix: book= kwarg skips _record_cli_cost
# ---------------------------------------------------------------------------

def test_record_cli_cost_skipped_when_book_set(tmp_path, monkeypatch):
    """_record_cli_cost must be a no-op when book= is provided (caller records)."""
    import importlib
    import brain.cost_guard as _cg
    _cg = importlib.reload(_cg)
    monkeypatch.setattr(_cg, "_DIR", tmp_path / "cost")
    monkeypatch.setattr(_cg, "_ROOT", tmp_path)

    import brain.cli_bridge as _cb
    result = {"cost_usd": 1.5, "usage": {"input_tokens": 500, "output_tokens": 100}}

    # Pass book= -> _record_cli_cost should return immediately, writing nothing.
    _cb._record_cli_cost(result, role="deep", model="claude-opus-4-8", book="autonomous")

    day = __import__("datetime").date.today().isoformat()
    assert not _cg._path(day).exists(), (
        "_record_cli_cost wrote cost data even though book= was supplied; double-count NOT fixed"
    )


def test_record_cli_cost_records_when_book_none(tmp_path, monkeypatch):
    """_record_cli_cost must record exactly once when book=None (no caller-level record)."""
    import importlib
    import brain.cost_guard as _cg
    _cg = importlib.reload(_cg)
    monkeypatch.setattr(_cg, "_DIR", tmp_path / "cost")
    monkeypatch.setattr(_cg, "_ROOT", tmp_path)

    import brain.cli_bridge as _cb
    result = {"cost_usd": 0.8, "usage": {"input_tokens": 300, "output_tokens": 80}}

    # No book= -> should record under the flagship book (role=deep maps to flagship).
    _cb._record_cli_cost(result, role="deep", model="claude-opus-4-8", book=None)

    day = __import__("datetime").date.today().isoformat()
    import json
    raw = json.loads(_cg._path(day).read_text())
    assert "flagship" in raw, "Expected cli_bridge to record under 'flagship' for role='deep'"
    assert raw["flagship"]["usd"] == pytest.approx(0.8)
    assert raw["flagship"]["input_tokens"] == 300


def test_record_cli_cost_utility_roles_still_record(tmp_path, monkeypatch):
    """Utility roles (scout/analyst, no bot-level record) must still record exactly once."""
    import importlib
    import brain.cost_guard as _cg
    _cg = importlib.reload(_cg)
    monkeypatch.setattr(_cg, "_DIR", tmp_path / "cost")
    monkeypatch.setattr(_cg, "_ROOT", tmp_path)

    import brain.cli_bridge as _cb
    result = {"cost_usd": 0.05, "usage": {"input_tokens": 100, "output_tokens": 20}}

    # scout role, no book= -> records under 'system'
    _cb._record_cli_cost(result, role="scout", model="claude-haiku-4-5")

    day = __import__("datetime").date.today().isoformat()
    import json
    raw = json.loads(_cg._path(day).read_text())
    assert "system" in raw, "Expected cli_bridge to record under 'system' for role='scout'"
    assert raw["system"]["usd"] == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# 9. _subscription_env token pool selector
# ---------------------------------------------------------------------------

def test_subscription_env_injects_pool_slot_when_bare_absent(monkeypatch):
    """When CLAUDE_CODE_OAUTH_TOKEN is absent, prefer slot 3..7 over 1..2."""
    import brain.cli_bridge as _cb
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    for i in range(1, 8):
        monkeypatch.delenv(f"CLAUDE_CODE_OAUTH_TOKEN_{i}", raising=False)
    # set slots 2 and 5 (5 should win as it is in the preferred 3..7 range)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_2", "tok_slot2")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_5", "tok_slot5")
    e = _cb._subscription_env()
    assert e.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok_slot5", (
        "Expected slot 5 (preferred range 3..7) to win over slot 2"
    )


def test_subscription_env_bare_token_wins_over_pool(monkeypatch):
    """When CLAUDE_CODE_OAUTH_TOKEN is already set, the pool selector must not override it."""
    import brain.cli_bridge as _cb
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok_bare")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_3", "tok_slot3")
    e = _cb._subscription_env()
    assert e.get("CLAUDE_CODE_OAUTH_TOKEN") == "tok_bare", (
        "Pool selector must not overwrite an explicit CLAUDE_CODE_OAUTH_TOKEN"
    )


def test_subscription_env_no_slots_no_bare(monkeypatch):
    """When no token env vars are set, CLAUDE_CODE_OAUTH_TOKEN must not appear (keychain path)."""
    import brain.cli_bridge as _cb
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    for i in range(1, 8):
        monkeypatch.delenv(f"CLAUDE_CODE_OAUTH_TOKEN_{i}", raising=False)
    e = _cb._subscription_env()
    # The key may be absent OR empty; either is fine — keychain login handles it.
    assert not e.get("CLAUDE_CODE_OAUTH_TOKEN"), (
        "CLAUDE_CODE_OAUTH_TOKEN must be absent/empty when no slots are set"
    )


def test_subscription_env_fallback_to_slot_1_2(monkeypatch):
    """When only slots 1..2 are set (no 3..7), the selector falls back to them."""
    import brain.cli_bridge as _cb
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    for i in range(1, 8):
        monkeypatch.delenv(f"CLAUDE_CODE_OAUTH_TOKEN_{i}", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_1", "tok_slot1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_2", "tok_slot2")
    e = _cb._subscription_env()
    # 3..7 are all unset; should fall back to slot 3 (empty) → 4 (empty) → ... → 1
    assert e.get("CLAUDE_CODE_OAUTH_TOKEN") in ("tok_slot1", "tok_slot2"), (
        "Fallback to stale slots 1..2 should activate when 3..7 are all empty"
    )


# ---------------------------------------------------------------------------
# 10. FIX 2 — pm_conviction flagship_pm record mirrors bot-brain pattern
# ---------------------------------------------------------------------------

def test_pm_conviction_record_full_fields(tmp_path, monkeypatch):
    """pm_conviction.build_book must record seat/model/tokens, not just usd.

    We mock cli_bridge.reason + autonomous_mcp to avoid any real LLM call and
    inspect the cost_guard ledger entry directly.
    """
    import importlib
    import brain.cost_guard as _cg
    _cg = importlib.reload(_cg)
    monkeypatch.setattr(_cg, "_DIR", tmp_path / "cost")
    monkeypatch.setattr(_cg, "_ROOT", tmp_path)

    # A fake _res as cli_bridge.reason would return for an armed pm turn
    fake_res = {
        "ok": True,
        "text": "pm analysis",
        "model": "claude-opus-4-8",
        "cost_usd": 0.75,
        "usage": {
            "input_tokens": 600,
            "output_tokens": 150,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 10,
        },
    }

    import brain.pm_conviction as _pmc

    # Patch _run_coro to return our fake result synchronously
    monkeypatch.setattr(_pmc, "_run_coro", lambda coro: fake_res)

    # autonomous_mcp is imported locally as 'brain.flagship_desk_mcp'; patch via sys.modules
    import sys
    import types as _types
    fake_amcp = _types.SimpleNamespace(
        build_servers=lambda: {},
        allowed_tools=lambda: [],
        clear_submission=lambda pid: None,
        read_submission=lambda pid: None,
    )
    monkeypatch.setitem(sys.modules, "brain.flagship_desk_mcp", fake_amcp)

    # Patch cli_bridge.reason to return a trivially awaitable (the monkeypatched
    # _run_coro will intercept it anyway)
    async def _fake_reason(*a, **kw):
        return fake_res
    import brain.cli_bridge as _cb
    monkeypatch.setattr(_cb, "reason", _fake_reason)

    # Call build_book (sized=[], the PM path returns stub unless sub is provided;
    # we only need it to reach the record() call which happens before read_submission).
    _pmc.build_book([], [], regime={}, asof="2026-07-16", strategist=None, gate_info={})

    import json
    raw_path = _cg._path("2026-07-16")
    assert raw_path.exists(), "cost_guard ledger was not written by build_book"
    raw = json.loads(raw_path.read_text())
    bk = raw.get("flagship") or {}
    assert isinstance(bk, dict), "flagship entry must be a new-schema dict"
    assert bk.get("usd", 0) == pytest.approx(0.75)
    assert bk.get("input_tokens", 0) == 600
    assert bk.get("output_tokens", 0) == 150
    assert bk.get("cache_read_tokens", 0) == 30
    assert bk.get("cache_creation_tokens", 0) == 10
    assert "flagship_pm" in (bk.get("seats") or {}), (
        "seat='flagship_pm' must be recorded in the ledger"
    )


# ---------------------------------------------------------------------------
# 11. FIX 3 — client._record_api_cost uses _ROLE_BOOK, not hardcoded 'flagship'
# ---------------------------------------------------------------------------

def test_client_api_analyst_role_records_to_system_book(tmp_path, monkeypatch):
    """API backend with role='analyst' must record under the 'system' book (not 'flagship').

    This verifies FIX 3: _record_api_cost now imports _ROLE_BOOK from cli_bridge
    instead of using a hardcoded book='flagship'.
    """
    monkeypatch.setenv("BOT_LLM_BACKEND", "api")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

    import importlib
    import brain.cost_guard as _cg
    _cg = importlib.reload(_cg)
    monkeypatch.setattr(_cg, "_DIR", tmp_path / "cost")
    monkeypatch.setattr(_cg, "_ROOT", tmp_path)

    from types import SimpleNamespace
    from unittest.mock import MagicMock, patch

    fake_usage = SimpleNamespace(
        input_tokens=200, output_tokens=50,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    fake_content = [SimpleNamespace(type="text", text="analysis done")]
    fake_resp = SimpleNamespace(
        stop_reason="end_turn",
        content=fake_content,
        usage=fake_usage,
        model="claude-haiku-4-5",
    )
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_resp

    import brain.client as _client
    with patch("anthropic.Anthropic", return_value=fake_client):
        text, err = _client.call_model("sys", "user", role="analyst")

    assert err is None
    assert text == "analysis done"

    import json
    day = __import__("datetime").date.today().isoformat()
    raw_path = _cg._path(day)
    assert raw_path.exists(), "cost_guard ledger not written"
    raw = json.loads(raw_path.read_text())
    assert "system" in raw, (
        f"role='analyst' must record to 'system' book via _ROLE_BOOK, got keys: {list(raw.keys())}"
    )
    assert "flagship" not in raw, (
        "role='analyst' must NOT record to 'flagship' (hardcoded default was removed)"
    )
