"""Tests for brain/translate.py and its integration into the web API.

All tests are OFFLINE: no real API calls are made. Monkeypatching is used to
stub the cache file path and the Haiku LLM call.
"""
from __future__ import annotations

import json
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

# ── W8 legacy-contract pin (2026-07-19): this file exercises pre-W8 build/book mechanics; the v2
# gates are covered by tests/test_flagship_v2_replay.py + tests/test_entry_context_engines.py.
import pytest as _pytest_w8


@_pytest_w8.fixture(autouse=True)
def _w8_legacy_env(monkeypatch):
    monkeypatch.setenv("MASTERMIND_ENTRY_GATE", "0")
    monkeypatch.setenv("MASTERMIND_PROPHET_FEED", "0")
    monkeypatch.setenv("MASTERMIND_ROTATION_IN", "off")
    monkeypatch.setenv("MASTERMIND_NW_DECISION", "off")
    try:
        from portfolio import prophet_feed as _pf
        _pf._reset_cache()
    except Exception:
        pass
    yield
    try:
        from portfolio import prophet_feed as _pf
        _pf._reset_cache()
    except Exception:
        pass



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_minimal_book() -> dict:
    """A tiny portfolio.v1 dict with the fields translate_book() reads."""
    return {
        "schema": "portfolio.v1",
        "disclaimer": "Paper-only. Not investment advice.",
        "positions": [
            {
                "ticker": "SMH",
                "thesis_full": {
                    "summary": "SMH is a leader.",
                    "why_now": "RS rank top decile.",
                    "sizing_rationale": "Equal-weight.",
                    "bull": ["RS rank: 99th percentile"],
                    "bear": ["Leadership rotation risk."],
                },
            }
        ],
        "rejected": [
            {
                "ticker": "ORCL",
                "reason": "Vetoed: altman_distress",
                "bear": ["Solvency: Altman zone=distress"],
            }
        ],
    }


# ---------------------------------------------------------------------------
# A: cached_zh
# ---------------------------------------------------------------------------

class TestCachedZh:
    def test_returns_none_when_absent(self, tmp_path, monkeypatch):
        """cached_zh returns None for a string not in the cache."""
        import brain.translate as tr
        monkeypatch.setattr(tr, "_CACHE_PATH", tmp_path / "translations.json")
        assert tr.cached_zh("hello world") is None

    def test_returns_value_when_present(self, tmp_path, monkeypatch):
        """cached_zh returns the cached Chinese text."""
        import brain.translate as tr
        cache_file = tmp_path / "translations.json"
        cache_file.write_text(json.dumps({"hello world": "你好世界"}), encoding="utf-8")
        monkeypatch.setattr(tr, "_CACHE_PATH", cache_file)
        assert tr.cached_zh("hello world") == "你好世界"

    def test_returns_none_for_empty_string(self, tmp_path, monkeypatch):
        import brain.translate as tr
        monkeypatch.setattr(tr, "_CACHE_PATH", tmp_path / "translations.json")
        assert tr.cached_zh("") is None
        assert tr.cached_zh("   ") is None

    def test_never_raises_on_corrupt_cache(self, tmp_path, monkeypatch):
        """cached_zh must not raise even if the cache file is corrupt JSON."""
        import brain.translate as tr
        cache_file = tmp_path / "translations.json"
        cache_file.write_text("NOT JSON!!!", encoding="utf-8")
        monkeypatch.setattr(tr, "_CACHE_PATH", cache_file)
        assert tr.cached_zh("anything") is None


# ---------------------------------------------------------------------------
# B: translate_and_cache + translate_book
# ---------------------------------------------------------------------------

_STUB_MAP = {
    "Paper-only. Not investment advice.": "仅纸面。非投资建议。",
    "SMH is a leader.": "SMH 是领先者。",
    "RS rank top decile.": "RS 排名前十分之一。",
    "Equal-weight.": "等权重。",
    "RS rank: 99th percentile": "RS 排名：第99百分位",
    "Leadership rotation risk.": "领导力轮动风险。",
    "Vetoed: altman_distress": "否决：奥特曼困境",
    "Solvency: Altman zone=distress": "偿付能力：奥特曼区=困境",
}


def _stub_call_haiku(texts: list[str]) -> dict[str, str]:
    return {t: _STUB_MAP[t] for t in texts if t in _STUB_MAP}


class TestTranslateAndCache:
    def test_warms_cache_for_known_strings(self, tmp_path, monkeypatch):
        """translate_and_cache writes new translations into the cache file."""
        import brain.translate as tr
        monkeypatch.setattr(tr, "_CACHE_PATH", tmp_path / "translations.json")
        monkeypatch.setattr(tr, "_call_haiku", _stub_call_haiku)

        result = tr.translate_and_cache(["SMH is a leader.", "Equal-weight."])
        assert result["SMH is a leader."] == "SMH 是领先者。"
        assert result["Equal-weight."] == "等权重。"
        # verify persisted to disk
        saved = json.loads((tmp_path / "translations.json").read_text())
        assert saved["SMH is a leader."] == "SMH 是领先者。"

    def test_skips_already_cached(self, tmp_path, monkeypatch):
        """Strings already in the cache do not trigger a Haiku call."""
        import brain.translate as tr
        cache_file = tmp_path / "translations.json"
        cache_file.write_text(json.dumps({"hello": "你好"}), encoding="utf-8")
        monkeypatch.setattr(tr, "_CACHE_PATH", cache_file)

        call_count = {"n": 0}

        def _no_call(texts):
            call_count["n"] += len(texts)
            return {}

        monkeypatch.setattr(tr, "_call_haiku", _no_call)
        result = tr.translate_and_cache(["hello"])
        assert result["hello"] == "你好"
        assert call_count["n"] == 0

    def test_falls_back_to_en_on_llm_failure(self, tmp_path, monkeypatch):
        """If the LLM returns nothing for a string, the result contains the en text."""
        import brain.translate as tr
        monkeypatch.setattr(tr, "_CACHE_PATH", tmp_path / "translations.json")
        monkeypatch.setattr(tr, "_call_haiku", lambda texts: {})  # empty response

        result = tr.translate_and_cache(["some unknown text"])
        assert result["some unknown text"] == "some unknown text"

    def test_translate_book_warms_expected_keys(self, tmp_path, monkeypatch):
        """translate_book() collects all translatable strings from a book dict."""
        import brain.translate as tr
        monkeypatch.setattr(tr, "_CACHE_PATH", tmp_path / "translations.json")
        monkeypatch.setattr(tr, "_call_haiku", _stub_call_haiku)

        book = _make_minimal_book()
        tr.translate_book(book)

        cache = json.loads((tmp_path / "translations.json").read_text())
        assert cache["Paper-only. Not investment advice."] == "仅纸面。非投资建议。"
        assert cache["SMH is a leader."] == "SMH 是领先者。"
        assert cache["RS rank top decile."] == "RS 排名前十分之一。"
        assert cache["Equal-weight."] == "等权重。"
        assert cache["RS rank: 99th percentile"] == "RS 排名：第99百分位"
        assert cache["Leadership rotation risk."] == "领导力轮动风险。"
        assert cache["Vetoed: altman_distress"] == "否决：奥特曼困境"
        assert cache["Solvency: Altman zone=distress"] == "偿付能力：奥特曼区=困境"

    def test_translate_book_handles_empty_book(self, tmp_path, monkeypatch):
        """translate_book() does not crash on an empty dict."""
        import brain.translate as tr
        monkeypatch.setattr(tr, "_CACHE_PATH", tmp_path / "translations.json")
        called = {"n": 0}
        monkeypatch.setattr(tr, "_call_haiku", lambda t: {})
        tr.translate_book({})  # must not raise


# ---------------------------------------------------------------------------
# C: /api/portfolio includes _zh when cache is pre-seeded
# ---------------------------------------------------------------------------

class TestWebPortfolioZh:
    def _seed_cache(self, tmp_path: Path, entries: dict[str, str]) -> Path:
        p = tmp_path / "translations.json"
        p.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
        return p

    def test_portfolio_includes_thesis_zh_when_cached(self, tmp_path, monkeypatch):
        """_zh fields appear in the portfolio response when the cache has entries."""
        import brain.translate as tr
        cache_file = self._seed_cache(tmp_path, {
            "Paper-only. Not investment advice.": "仅纸面。非投资建议。",
            "SMH is a leader.": "SMH 是领先者。",
            "RS rank top decile.": "RS 排名前十分之一。",
            "Equal-weight.": "等权重。",
            "RS rank: 99th percentile": "RS 排名：第99百分位",
            "Leadership rotation risk.": "领导力轮动风险。",
            "Vetoed: altman_distress": "否决：奥特曼困境",
            "Solvency: Altman zone=distress": "偿付能力：奥特曼区=困境",
        })
        monkeypatch.setattr(tr, "_CACHE_PATH", cache_file)

        # Also patch the app's _cached_zh wrapper so it uses our tmp cache
        import app.web as web_mod
        monkeypatch.setattr(web_mod, "_cached_zh", tr.cached_zh)

        # Build a minimal latest.json in a temp dir
        portfolio_dir = tmp_path / "portfolio"
        portfolio_dir.mkdir()
        book = _make_minimal_book()
        (portfolio_dir / "latest.json").write_text(json.dumps(book), encoding="utf-8")

        # Patch _data() to point at tmp_path/data layout
        data_root = tmp_path
        monkeypatch.setattr(web_mod, "_data", lambda: data_root)
        # Re-route portfolio dir
        orig_data_fn = web_mod._data

        def _patched_data():
            return tmp_path

        monkeypatch.setattr(web_mod, "_data", _patched_data)
        # /api/portfolio resolves the book via _portfolio_dir() -> registry.data_dir() (an ABSOLUTE
        # checkout-root path), NOT _data(), so patching _data alone leaks the LIVE production book.
        # Redirect _portfolio_dir to the tmp book so the endpoint serves this test's fixture.
        monkeypatch.setattr(web_mod, "_portfolio_dir", lambda portfolio=None: tmp_path / "portfolio")
        # Rewrite latest.json at the right path
        (tmp_path / "portfolio").mkdir(exist_ok=True)
        (tmp_path / "portfolio" / "latest.json").write_text(json.dumps(book), encoding="utf-8")

        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app, raise_server_exceptions=True)
        r = client.get("/api/portfolio")
        assert r.status_code == 200
        data = r.json()

        # disclaimer_zh
        assert data.get("disclaimer_zh") == "仅纸面。非投资建议。"

        # position _zh
        pos = data["positions"][0]
        tf_zh = pos["thesis_full"].get("_zh", {})
        assert tf_zh.get("summary") == "SMH 是领先者。"
        assert tf_zh.get("why_now") == "RS 排名前十分之一。"
        assert tf_zh.get("sizing_rationale") == "等权重。"
        assert "RS 排名：第99百分位" in (tf_zh.get("bull") or [])
        assert "领导力轮动风险。" in (tf_zh.get("bear") or [])

        # rejected _zh
        rej = data["rejected"][0]
        rej_zh = rej.get("_zh", {})
        assert rej_zh.get("reason") == "否决：奥特曼困境"
        assert "偿付能力：奥特曼区=困境" in (rej_zh.get("bear") or [])

    def test_portfolio_unchanged_when_cache_empty(self, tmp_path, monkeypatch):
        """If nothing is cached, the portfolio payload has no _zh fields."""
        import brain.translate as tr
        empty_cache = tmp_path / "translations.json"
        empty_cache.write_text("{}", encoding="utf-8")     # a real empty cache on disk
        monkeypatch.setattr(tr, "_CACHE_PATH", empty_cache)
        # Reset the module-level mtime memo — otherwise _load_cache returns the stale in-memory
        # cache warmed by an earlier test / live call (it falls back to _cache_mem when the patched
        # path is unchanged/missing), leaking real zh entries into this "empty cache" assertion.
        monkeypatch.setattr(tr, "_cache_mem", None)
        monkeypatch.setattr(tr, "_cache_mtime", -1.0)

        import app.web as web_mod
        monkeypatch.setattr(web_mod, "_cached_zh", tr.cached_zh)

        (tmp_path / "portfolio").mkdir(exist_ok=True)
        book = _make_minimal_book()
        (tmp_path / "portfolio" / "latest.json").write_text(json.dumps(book), encoding="utf-8")
        monkeypatch.setattr(web_mod, "_data", lambda: tmp_path)
        # Serve the tmp book (not the live production portfolio — see the cached-test note above).
        monkeypatch.setattr(web_mod, "_portfolio_dir", lambda portfolio=None: tmp_path / "portfolio")

        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app, raise_server_exceptions=True)
        r = client.get("/api/portfolio")
        assert r.status_code == 200
        data = r.json()

        assert "disclaimer_zh" not in data
        for pos in data.get("positions", []):
            assert "_zh" not in (pos.get("thesis_full") or {})
        for rej in data.get("rejected", []):
            assert "_zh" not in rej
