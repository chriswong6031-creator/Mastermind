import asyncio


class _SharedKeyPool:
    def __init__(self, cooling=()):
        self.cooling = set(cooling)

    def is_cooling(self, key_id):
        return key_id in self.cooling


class _SharedAuth:
    @staticmethod
    def _oauth_pool_candidates(_lane):
        return [
            ("claude_code_oauth_3", "CLAUDE_CODE_OAUTH_TOKEN_3"),
            ("claude_code_oauth_5", "CLAUDE_CODE_OAUTH_TOKEN_5"),
        ]

    @staticmethod
    def _is_auth_error(exc):
        return "401" in str(exc)

    @staticmethod
    def _is_rate_limit_error(exc):
        return "usage limit" in str(exc).lower() or "429" in str(exc)


def test_provider_rungs_are_codex_first_and_respect_shared_cooling(monkeypatch):
    from brain import codex_bridge, provider_waterfall as pw

    monkeypatch.setattr(codex_bridge, "available", lambda: True)
    monkeypatch.setattr(pw, "_shared_modules",
                        lambda: (_SharedAuth, _SharedKeyPool({"claude_code_oauth_3"})))
    monkeypatch.setattr(pw, "_local_cooling", lambda: {})

    assert [(r["provider"], r["key_id"]) for r in pw.provider_rungs("pm")] == [
        ("codex", "codex_account"),
        ("oauth", "claude_code_oauth_5"),
        ("oauth", "claude_code_oauth_3"),
    ]


def test_successful_codex_stops_before_oauth(monkeypatch):
    from brain import cli_bridge, provider_waterfall as pw

    monkeypatch.setattr(pw, "provider_rungs", lambda _role: [
        {"provider": "codex", "key_id": "codex_account",
         "env_name": None, "cooling": False},
        {"provider": "oauth", "key_id": "claude_code_oauth_3",
         "env_name": "CLAUDE_CODE_OAUTH_TOKEN_3", "cooling": False},
    ])
    monkeypatch.setattr(pw, "_note_codex", lambda *_args, **_kwargs: None)
    calls = []

    async def fake_reason(_prompt, **kwargs):
        calls.append(kwargs["_backend_override"])
        return {"ok": True, "text": "codex", "backend": "codex"}

    monkeypatch.setattr(cli_bridge, "_reason", fake_reason)
    out = asyncio.run(pw.reason("test", role="pm"))

    assert out["text"] == "codex"
    assert out["provider"] == "codex"
    assert calls == ["codex"]


def test_codex_quota_failure_falls_back_to_selected_oauth_slot(monkeypatch):
    from brain import cli_bridge, provider_waterfall as pw

    monkeypatch.setattr(
        pw,
        "_shared_modules",
        lambda: (_SharedAuth, _SharedKeyPool()),
    )
    monkeypatch.setattr(pw, "provider_rungs", lambda _role: [
        {"provider": "codex", "key_id": "codex_account",
         "env_name": None, "cooling": False},
        {"provider": "oauth", "key_id": "claude_code_oauth_5",
         "env_name": "CLAUDE_CODE_OAUTH_TOKEN_5", "cooling": False},
    ])
    monkeypatch.setattr(pw, "_note_codex", lambda *_args, **_kwargs: None)
    calls = []

    async def fake_reason(_prompt, **kwargs):
        calls.append(kwargs)
        if kwargs["_backend_override"] == "codex":
            return {"ok": False, "text": None, "backend": "codex",
                    "error": "429 usage limit reached"}
        return {"ok": True, "text": "claude", "backend": "sdk",
                "key_id": "claude_code_oauth_5"}

    monkeypatch.setattr(cli_bridge, "_reason", fake_reason)
    out = asyncio.run(pw.reason("test", role="pm"))

    assert out["text"] == "claude"
    assert out["provider"] == "oauth"
    assert calls[0]["_backend_override"] == "codex"
    assert calls[1]["_oauth_candidates"][0]["key_id"] == "claude_code_oauth_5"
    assert [a["provider"] for a in out["provider_attempts"]] == ["codex", "oauth"]


def test_non_provider_codex_failure_is_not_replayed(monkeypatch):
    from brain import cli_bridge, provider_waterfall as pw

    monkeypatch.setattr(pw, "provider_rungs", lambda _role: [
        {"provider": "codex", "key_id": "codex_account",
         "env_name": None, "cooling": False},
        {"provider": "oauth", "key_id": "claude_code_oauth_3",
         "env_name": "CLAUDE_CODE_OAUTH_TOKEN_3", "cooling": False},
    ])
    monkeypatch.setattr(pw, "_note_codex", lambda *_args, **_kwargs: None)
    calls = []

    async def fake_reason(_prompt, **kwargs):
        calls.append(kwargs["_backend_override"])
        return {"ok": False, "text": None, "backend": "codex",
                "error": "typed tool returned invalid state"}

    monkeypatch.setattr(cli_bridge, "_reason", fake_reason)
    out = asyncio.run(pw.reason("test", role="pm"))

    assert out["ok"] is False
    assert calls == ["codex"]
