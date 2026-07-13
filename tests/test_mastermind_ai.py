"""tests/test_mastermind_ai.py — W-AI loop coordinator (brain/mastermind_ai.py) offline battery.

No LLM, no vendor data. Every test isolates the module's data layout (_DIR/_SETTINGS/_LOOP_LOG/
_REVIEWS/_DIRECTIVES) AND its call-time root (_ROOT) to tmp_path, stubs the doctrine block, and
stubs brain.neural_web_context / brain.journal / brain.nw_reflection so nothing live is read or
written.

Coverage:
  * settings(): doctrine defaults; update_settings rejects unknown keys + out-of-range values
    + booleans offered to int keys, applies valid (bounded) ones.
  * add_directive(): intake scrub refuses env names / $ amounts / key-shaped tokens / over-length;
    accepts clean text; open_directives_for_publish flips queued→published for SHIPPED rows only,
    truncating oldest-first at the cap; reconcile_ack flips published→acknowledged off the
    macro ack lobe.
  * run_cycle(): skipped when MASTERMIND_AI_LOOP=0; with the flag on and review_every_n_loops=2
    two cycles → two loop-log rows, the second carrying a review appended to reviews.jsonl.
  * _build_review() LLM seam: reason_sync stubbed hermetically; a deny-pattern hit in the text
    drops the whole llm_assessment; clean text lands; allowed_tools=[] + log_run=False on the call.
  * status()/improvements()/loop_log() read-surface shapes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import brain.neural_web_context as nwc
from brain import journal, mastermind_ai as mai, nw_reflection as nwr


# ───────────────────────────── isolation ─────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_ai(tmp_path, monkeypatch):
    d = tmp_path / "data" / "mastermind_ai"
    monkeypatch.setattr(mai, "_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(mai, "_DIR", d, raising=True)
    monkeypatch.setattr(mai, "_SETTINGS", d / "settings.json", raising=True)
    monkeypatch.setattr(mai, "_LOOP_LOG", d / "loop_log.jsonl", raising=True)
    monkeypatch.setattr(mai, "_REVIEWS", d / "reviews.jsonl", raising=True)
    monkeypatch.setattr(mai, "_DIRECTIVES", d / "directives.jsonl", raising=True)
    monkeypatch.setattr(mai, "_doctrine_block", lambda: {}, raising=True)
    # reflection engine writes under tmp too (run_cycle step 2 + _build_review history read)
    out = tmp_path / "data" / "nw_reflection"
    monkeypatch.setattr(nwr, "_ROOT", tmp_path, raising=True)
    monkeypatch.setattr(nwr, "_OUT_DIR", out, raising=True)
    monkeypatch.setattr(nwr, "_LATEST", out / "latest.json", raising=True)
    monkeypatch.setattr(nwr, "_HISTORY", out / "history.jsonl", raising=True)
    # journal store isolated; drafting/pinning stubbed to fast no-ops (degrade-safe by design,
    # but they write under data/journal/ when exercised for real)
    monkeypatch.setattr(journal, "_JOURNAL", tmp_path / "data" / "journal", raising=False)
    monkeypatch.setattr(journal, "draft_all", lambda asof=None: {}, raising=True)
    monkeypatch.setattr(journal, "recompute_pins", lambda seat, asof=None: [], raising=True)
    # no live NW artifact, no live agenda
    monkeypatch.setattr(nwc, "context", lambda: {}, raising=True)
    monkeypatch.setattr(nwc, "market_plane", lambda: {"stale": True, "asof": None}, raising=True)
    monkeypatch.setattr(mai, "_agenda_top", lambda n=3: [], raising=True)
    # flags: default env clean; individual tests set them
    monkeypatch.delenv("MASTERMIND_AI_LOOP", raising=False)
    monkeypatch.delenv("MASTERMIND_AI_REVIEW_LLM", raising=False)


# ───────────────────────────── settings ─────────────────────────────

def test_settings_defaults():
    s = mai.settings()
    assert s == {
        "loop_enabled": True,
        "review_every_n_loops": 5,
        "nudges_max": 10,
        "attribution_min_n": 12,
        "llm_review": False,
        "directives_max_open": 10,
    }


def test_update_settings_rejects_unknown_keys():
    res = mai.update_settings({"evil_new_knob": 1, "nudges_max": 5})
    assert res["ok"] is True
    assert "evil_new_knob" in res["rejected"]
    assert res["settings"]["nudges_max"] == 5
    assert "evil_new_knob" not in res["settings"]


def test_update_settings_rejects_out_of_range_values():
    res = mai.update_settings({
        "review_every_n_loops": 1,     # below 2
        "nudges_max": 11,              # above 10
        "attribution_min_n": 5,        # below 6
        "directives_max_open": 0,      # below 1
        "loop_enabled": {"not": "coercible"},
    })
    assert sorted(res["rejected"]) == sorted([
        "review_every_n_loops", "nudges_max", "attribution_min_n",
        "directives_max_open", "loop_enabled"])
    assert res["settings"] == mai.settings()  # nothing applied → still defaults
    assert res["settings"]["review_every_n_loops"] == 5


def test_update_settings_rejects_bool_for_int_key():
    """int(True)==1 would silently pass the review_every_n_loops bounds — bools offered to
    int keys are rejected outright."""
    res = mai.update_settings({"review_every_n_loops": True})
    assert res["ok"] is True
    assert res["rejected"] == ["review_every_n_loops"]
    assert res["settings"]["review_every_n_loops"] == 5   # default untouched


def test_update_settings_applies_valid_values_and_persists():
    res = mai.update_settings({"review_every_n_loops": 2, "llm_review": True,
                               "loop_enabled": "yes", "attribution_min_n": 20})
    assert res["ok"] is True and res["rejected"] == []
    s = mai.settings()
    assert s["review_every_n_loops"] == 2
    assert s["llm_review"] is True
    assert s["loop_enabled"] is True
    assert s["attribution_min_n"] == 20
    # persisted to the isolated settings.json, not just in-memory
    on_disk = json.loads(mai._SETTINGS.read_text())
    assert on_disk["review_every_n_loops"] == 2


# ───────────────────────────── directives ─────────────────────────────

def test_add_directive_refuses_env_name():
    res = mai.add_directive("please arm MASTERMIND_FOO on the live book")
    assert res["ok"] is False
    assert "deny pattern" in res["error"]
    assert mai.directives() == []


def test_add_directive_refuses_dollar_amount():
    res = mai.add_directive("cap the book at $1,000 notional")
    assert res["ok"] is False
    assert "deny pattern" in res["error"]


def test_add_directive_refuses_key_shaped_token():
    token = "Ab3" * 15  # 45-char base64-ish token
    res = mai.add_directive(f"use key {token} for the feed")
    assert res["ok"] is False
    assert "deny pattern" in res["error"]


def test_add_directive_refuses_over_length():
    res = mai.add_directive("x" * 281)
    assert res["ok"] is False
    assert "too long" in res["error"]


def test_add_directive_refuses_empty():
    assert mai.add_directive("   ")["ok"] is False


def test_add_directive_accepts_clean_text_and_queues():
    res = mai.add_directive("prefer confirmed bottom states over watch-only candidates")
    assert res["ok"] is True
    row = res["directive"]
    assert row["status"] == "queued"
    assert len(row["id"]) == 16
    rows = mai.directives()
    assert len(rows) == 1 and rows[0]["status"] == "queued"


def test_add_directive_respects_open_cap():
    assert mai.update_settings({"directives_max_open": 1})["ok"]
    assert mai.add_directive("first clean directive")["ok"] is True
    res = mai.add_directive("second clean directive")
    assert res["ok"] is False
    assert "too many open directives" in res["error"]


def test_open_directives_for_publish_flips_queued_to_published():
    added = mai.add_directive("watch the small-cap breadth divergence")
    rid = added["directive"]["id"]
    out = mai.open_directives_for_publish()
    assert [d["id"] for d in out] == [rid]
    assert set(out[0]) == {"id", "created", "text"}
    # the queue now records the row as published (latest-status view)
    assert mai.directives()[0]["status"] == "published"
    # publishing again still ships it (published rows stay on the wire until acked)
    assert [d["id"] for d in mai.open_directives_for_publish()] == [rid]


def test_open_directives_for_publish_truncates_fifo_and_publishes_only_shipped():
    """Truncate-first FIFO at the cap: with 3 queued rows and cap=1 exactly the OLDEST ships,
    and only the shipped row flips queued→published — the truncated two stay queued."""
    ids = []
    for text in ("first clean directive in the queue",
                 "second clean directive in the queue",
                 "third clean directive in the queue"):
        res = mai.add_directive(text)
        assert res["ok"] is True
        ids.append(res["directive"]["id"])
    assert mai.update_settings({"directives_max_open": 1})["ok"]

    out = mai.open_directives_for_publish()
    assert [d["id"] for d in out] == [ids[0]]          # oldest-first, capped at 1

    status_by_id = {d["id"]: d["status"] for d in mai.directives()}
    assert status_by_id[ids[0]] == "published"         # shipped → published
    assert status_by_id[ids[1]] == "queued"            # truncated → NEVER 'published'
    assert status_by_id[ids[2]] == "queued"

    # republishing keeps shipping the same oldest row (still on the wire until acked)
    assert [d["id"] for d in mai.open_directives_for_publish()] == [ids[0]]
    status_by_id = {d["id"]: d["status"] for d in mai.directives()}
    assert status_by_id[ids[1]] == "queued" and status_by_id[ids[2]] == "queued"


def test_reconcile_ack_flips_published_to_acknowledged(monkeypatch):
    added = mai.add_directive("track the tga drawdown into quarter end")
    rid = added["directive"]["id"]
    mai.open_directives_for_publish()          # queued → published
    monkeypatch.setattr(nwc, "context", lambda: {
        "lobes": {"mastermind_ai": {"ack": {
            "directive_ids_seen": [rid], "nudge_codes_seen": ["graph_conflicts_absent"]}}},
    })
    res = mai.reconcile_ack()
    assert res["state"] == "ok"
    assert res["directives_acknowledged"] == 1
    assert res["nudge_codes_seen_n"] == 1
    assert mai.directives()[0]["status"] == "acknowledged"
    # acknowledged rows leave the publish wire
    assert mai.open_directives_for_publish() == []


def test_reconcile_ack_absent_lobe_is_honest_noop():
    res = mai.reconcile_ack()
    assert res == {"state": "absent", "directives_acknowledged": 0, "nudge_codes_seen_n": 0}


# ───────────────────────────── run_cycle ─────────────────────────────

def test_run_cycle_skipped_when_env_flag_off(monkeypatch):
    monkeypatch.setenv("MASTERMIND_AI_LOOP", "0")
    res = mai.run_cycle("2026-07-13")
    assert res == {"ok": False, "skipped": "loop disabled", "asof": "2026-07-13"}
    assert mai.loop_log() == []


def test_run_cycle_skipped_when_runtime_setting_off(monkeypatch):
    monkeypatch.setenv("MASTERMIND_AI_LOOP", "1")
    mai.update_settings({"loop_enabled": False})
    res = mai.run_cycle("2026-07-13")
    assert res.get("skipped") == "loop disabled"


def test_run_cycle_two_loops_and_review_cadence(monkeypatch):
    monkeypatch.setenv("MASTERMIND_AI_LOOP", "1")
    assert mai.update_settings({"review_every_n_loops": 2})["ok"]

    row1 = mai.run_cycle("2026-07-13", trigger="test")
    assert row1["ok"] is True
    assert row1["loop_n"] == 1
    assert row1["trigger"] == "test"
    assert "review" not in row1
    assert "reflection" in row1["steps"]          # nw_reflection.persist ran (into tmp)
    assert "summary" in row1 and "loop 1" in row1["summary"]

    row2 = mai.run_cycle("2026-07-14", trigger="test")
    assert row2["loop_n"] == 2
    assert "review" in row2
    assert row2["review"]["loop_n"] == 2

    log = mai.loop_log()
    assert [r["loop_n"] for r in log] == [1, 2]

    reviews = mai.reviews()
    assert len(reviews) == 1
    rev = reviews[0]
    assert set(rev) >= {"ts", "loop_n", "window_loops", "completed", "assessment", "agenda_top"}
    assert rev["loop_n"] == 2 and rev["window_loops"] == 2
    assert isinstance(rev["assessment"], list) and rev["assessment"]
    # deterministic review: no LLM prose without the double gate
    assert "llm_assessment" not in rev


def test_run_cycle_writes_reflection_artifacts_into_isolated_tree(monkeypatch):
    monkeypatch.setenv("MASTERMIND_AI_LOOP", "1")
    mai.run_cycle("2026-07-13")
    assert nwr._LATEST.exists()
    assert json.loads(nwr._LATEST.read_text())["asof"] == "2026-07-13"
    assert mai._LOOP_LOG.exists()


def test_run_cycle_never_raises_when_reflection_breaks(monkeypatch):
    monkeypatch.setenv("MASTERMIND_AI_LOOP", "1")
    monkeypatch.setattr(nwr, "persist",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    row = mai.run_cycle("2026-07-13")
    assert row["ok"] is True
    assert row["steps"].get("reflection_error") == "RuntimeError"
    assert row["nudges_open"] == 0


# ───────────────────────────── the LLM review seam ─────────────────────────────

def _stub_cli_bridge(monkeypatch, result: dict) -> list[dict]:
    """Install a stub brain.cli_bridge (the real one pulls the bot package + SDK) whose
    reason_sync records its kwargs and returns `result`. Returns the call log."""
    import sys
    import types
    import brain as brain_pkg
    calls: list[dict] = []
    mod = types.ModuleType("brain.cli_bridge")

    def reason_sync(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return result

    mod.reason_sync = reason_sync
    monkeypatch.setitem(sys.modules, "brain.cli_bridge", mod)
    monkeypatch.setattr(brain_pkg, "cli_bridge", mod, raising=False)
    return calls


def _arm_llm_review(monkeypatch):
    """Both halves of the double gate: env capability flag AND runtime setting."""
    monkeypatch.setenv("MASTERMIND_AI_REVIEW_LLM", "1")
    assert mai.update_settings({"llm_review": True})["ok"]


def test_build_review_deny_sweep_drops_leaky_llm_assessment(monkeypatch):
    """reviews.jsonl rsyncs to the PUBLIC mirror — a single _DIRECTIVE_DENY hit in the
    model text rejects the whole assessment."""
    _arm_llm_review(monkeypatch)
    calls = _stub_cli_bridge(monkeypatch, {"ok": True, "text": "MASTERMIND_FOO leaked"})
    review = mai._build_review(2, mai.settings())
    assert "llm_assessment" not in review
    assert review["assessment"]                        # deterministic assessment still present
    # the seam is locked down: no tools, no run log
    assert len(calls) == 1
    assert calls[0]["allowed_tools"] == []
    assert calls[0]["log_run"] is False


def test_build_review_clean_llm_text_lands(monkeypatch):
    _arm_llm_review(monkeypatch)
    _stub_cli_bridge(monkeypatch, {"ok": True, "text": "steady progress; no alpha claims"})
    review = mai._build_review(2, mai.settings())
    assert review["llm_assessment"] == "steady progress; no alpha claims"


def test_build_review_llm_stays_dark_without_env_flag(monkeypatch):
    """Runtime setting alone must not open the seam — reason_sync is never called."""
    monkeypatch.delenv("MASTERMIND_AI_REVIEW_LLM", raising=False)
    assert mai.update_settings({"llm_review": True})["ok"]
    calls = _stub_cli_bridge(monkeypatch, {"ok": True, "text": "should never be asked"})
    review = mai._build_review(2, mai.settings())
    assert "llm_assessment" not in review
    assert calls == []


# ───────────────────────────── read surfaces ─────────────────────────────

def test_loop_log_shape_and_limit(monkeypatch):
    monkeypatch.setenv("MASTERMIND_AI_LOOP", "1")
    for i in range(3):
        mai.run_cycle(f"2026-07-1{i+1}")
    log = mai.loop_log(limit=2)
    assert len(log) == 2
    assert [r["loop_n"] for r in log] == [2, 3]
    for r in log:
        assert set(r) >= {"ts", "asof", "trigger", "loop_n", "steps", "nudges_open",
                          "summary", "ok"}


def test_status_shape(monkeypatch):
    monkeypatch.setenv("MASTERMIND_AI_LOOP", "1")
    mai.update_settings({"review_every_n_loops": 2})
    mai.run_cycle("2026-07-13")
    mai.run_cycle("2026-07-14")
    st = mai.status()
    assert st["schema"] == "mastermind_ai_status.v1"
    assert set(st) >= {"generated_at", "flags", "settings", "loop_n", "last_loops",
                       "last_review", "reflection", "journal", "directives"}
    assert st["flags"] == {"loop": True, "llm_review": False}
    assert st["loop_n"] == 2
    assert st["last_review"] and st["last_review"]["loop_n"] == 2
    refl = st["reflection"]
    assert set(refl) >= {"asof", "nudges", "contract_drift_n", "coverage",
                         "context_quality", "attribution"}
    assert refl["asof"] == "2026-07-14"
    jc = st["journal"]
    assert set(jc) == {"drafts", "pending", "lessons", "pins_active", "pins_unpinned"}


def test_status_cold_start_shape():
    st = mai.status()
    assert st["schema"] == "mastermind_ai_status.v1"
    assert st["loop_n"] == 0
    assert st["last_loops"] == []
    assert st["last_review"] is None
    assert st["reflection"]["nudges"] == []
    assert st["directives"] == []


def test_improvements_shape(monkeypatch):
    monkeypatch.delenv("MASTERMIND_SELF_TUNE", raising=False)
    out = mai.improvements()
    assert set(out) >= {"pins", "self_tune", "agenda_top", "lessons_by_taxonomy"}
    assert isinstance(out["pins"], list)
    assert isinstance(out["lessons_by_taxonomy"], dict)
    st = out["self_tune"]
    assert set(st) == {"families_n", "locked", "armed"}
    assert st["armed"] is False        # MASTERMIND_SELF_TUNE not set in tests


def test_flag_helpers_defaults(monkeypatch):
    monkeypatch.delenv("MASTERMIND_AI_LOOP", raising=False)
    monkeypatch.delenv("MASTERMIND_AI_REVIEW_LLM", raising=False)
    assert mai.loop_flag_on() is True            # loop defaults ON
    assert mai.llm_review_flag_on() is False     # LLM review defaults OFF
    monkeypatch.setenv("MASTERMIND_AI_LOOP", "0")
    assert mai.loop_flag_on() is False
