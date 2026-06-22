"""Regression tests: the Autonomous Brain can no longer peek at another book's state.

The autonomous book's holdings looked uncannily like Flagship's. The forensic verdict was
COMMON CAUSE (both read the same in-house buy board), but two latent peek PATHS were open:

  1. ``read_signal`` allowlisted ``_ROOT/data`` — which contains the portfolio book dirs
     (``data/portfolio/*``, ``data/portfolios/*``) — so a Brain could pull any book's
     positions/ledger/research by path.
  2. raw ``Read``/``Grep``/``Glob`` were in the autonomous allow-list with the session cwd at
     the repo root, reaching any file with no allowlist at all.

These tests prove BOTH are closed: read_signal denies portfolio-book paths (and audits the
attempt) while still serving real signal JSON, and the autonomous tool surface no longer
carries raw filesystem tools or the flagship book reader.
"""
import asyncio
import json
from pathlib import Path

import bot  # noqa: F401

from brain import autonomous_mcp, bot_mcp

_ROOT = Path(__file__).resolve().parent.parent


def _text(result: dict) -> str:
    return result["content"][0]["text"]


def _read(path: str) -> str:
    return _text(asyncio.run(bot_mcp.read_signal.handler({"path": path})))


# ── read_signal: portfolio book state is now firewalled ──────────────────────────────────

def test_read_signal_denies_flagship_book():
    out = _read("data/portfolio/latest.json")
    assert "DENIED" in out and "portfolio book" in out.lower()


def test_read_signal_denies_flagship_ledger_and_subdirs():
    # the legacy flagship dir AND its nested books (e.g. self_directed lives under it)
    for rel in ("data/portfolio/positions_ledger.json",
                "data/portfolio/self_directed/account.json"):
        assert "DENIED" in _read(rel), rel


def test_read_signal_denies_every_other_portfolio_book():
    # any book under data/portfolios/<id>/ — autonomous reading flagship/self_directed, etc.
    for pid in ("autonomous", "self_directed", "heavyweight"):
        out = _read(f"data/portfolios/{pid}/latest.json")
        assert "DENIED" in out and "portfolio book" in out.lower(), pid


def test_read_signal_still_serves_real_signals():
    # a genuine published-signal path must NOT be caught by the portfolio-book denylist
    # (it either returns data or an honest "not found" — never the book-state denial).
    out = _read("vendor/macro/site/factordata/us_standouts.json")
    assert "portfolio book" not in out.lower()


def test_read_signal_external_path_still_denied():
    # regression: the original outside-the-roots guard is intact
    assert "DENIED" in _read("/etc/passwd")


def test_denied_book_read_is_audited(tmp_path, monkeypatch):
    audit = tmp_path / "read_signal_denied.jsonl"
    monkeypatch.setattr(bot_mcp, "_LEAK_AUDIT", audit)
    assert "DENIED" in _read("data/portfolio/latest.json")
    rows = [json.loads(l) for l in audit.read_text().splitlines() if l.strip()]
    assert rows and rows[-1]["denied_path"].endswith("data/portfolio/latest.json")


# ── autonomous allow-list: no raw filesystem, no peer-book reader ─────────────────────────

def test_autonomous_allowlist_drops_raw_fs_tools():
    allowed = set(autonomous_mcp.allowed_tools())
    assert not (allowed & {"Read", "Grep", "Glob"}), \
        "raw filesystem tools must not be in the autonomous allow-list (peek path)"


def test_autonomous_allowlist_has_no_flagship_book_reader():
    allowed = set(autonomous_mcp.allowed_tools())
    # get_portfolio is the FLAGSHIP book reader — it must stay excluded from the Brain's surface
    assert f"mcp__{bot_mcp.SERVER_NAME}__get_portfolio" not in allowed
    # and the server itself must not register it either
    bot_tools = {t.name for t in autonomous_mcp._READ_TOOLS}
    assert "get_portfolio" not in bot_tools


def test_autonomous_allowlist_is_only_typed_read_desk_web():
    # every allowed tool is a typed mcp__bot__/mcp__desk__ tool or a web tool — nothing raw.
    for t in autonomous_mcp.allowed_tools():
        assert t.startswith("mcp__bot__") or t.startswith("mcp__desk__") or t in set(bot_mcp.WEB_TOOLS), t
