"""brain/decision_provenance.py — the E2 replayable decision-provenance ledger.
Offline; tmp-path JSONL; always-on observability, fail-soft everywhere."""
from __future__ import annotations

import bot  # noqa: F401


# --------------------------------------------------------------------------- #
# flags_hash — deterministic fingerprint of the KNOWN_FLAGS env configuration
# --------------------------------------------------------------------------- #

def test_flags_hash_is_deterministic_and_short(monkeypatch):
    from brain import decision_provenance as dp
    # a fixed env → the same 12-hex digest every call
    monkeypatch.setenv("MASTERMIND_COMMITTEE", "1")
    h1 = dp.flags_hash()
    h2 = dp.flags_hash()
    assert h1 == h2
    assert isinstance(h1, str) and len(h1) == 12
    assert all(c in "0123456789abcdef" for c in h1)


def test_flags_hash_changes_when_a_flag_env_changes(monkeypatch):
    from brain import decision_provenance as dp
    monkeypatch.delenv("MASTERMIND_COMMITTEE", raising=False)
    h_off = dp.flags_hash()
    monkeypatch.setenv("MASTERMIND_COMMITTEE", "1")
    h_on = dp.flags_hash()
    assert h_off != h_on, "flipping a KNOWN flag must change the fingerprint"
    # a DIFFERENT value is also distinguishable
    monkeypatch.setenv("MASTERMIND_COMMITTEE", "0")
    h_zero = dp.flags_hash()
    assert h_zero not in (h_off, h_on)


def test_flags_hash_ignores_non_known_flags(monkeypatch):
    from brain import decision_provenance as dp
    monkeypatch.delenv("MASTERMIND_COMMITTEE", raising=False)
    base = dp.flags_hash()
    # a MASTERMIND_* var NOT in KNOWN_FLAGS must not move the fingerprint
    monkeypatch.setenv("MASTERMIND_TOTALLY_UNKNOWN_XYZ", "1")
    assert dp.flags_hash() == base


# --------------------------------------------------------------------------- #
# row — the replayable provenance record builder
# --------------------------------------------------------------------------- #

def test_row_builder_assembles_nullable_fields_and_stamps_hash():
    from brain import decision_provenance as dp
    r = dp.row(
        "avgo",
        sources={"intake": ["momentum_board"]},
        stage_verdicts={"forge_confirmed": True, "action": "add"},
        seats=["committee"],
        sector_phase="Expansion",
        nw={"bottom_state": "CONFIRMED"},
        final={"action": "add", "weight": 0.06},
    )
    assert r["ticker"] == "AVGO"                       # normalized upper
    assert r["sources"] == {"intake": ["momentum_board"]}
    assert r["stage_verdicts"]["action"] == "add"
    assert r["seats"] == ["committee"]
    assert r["sector_phase_at_entry"] == "Expansion"
    assert r["nw"] == {"bottom_state": "CONFIRMED"}
    assert r["final"] == {"action": "add", "weight": 0.06}
    assert isinstance(r["flags_hash"], str) and len(r["flags_hash"]) == 12
    assert "recorded_at" in r


def test_row_builder_all_optional_defaults_to_none():
    from brain import decision_provenance as dp
    r = dp.row("nem")                                  # a name with none of the stages
    assert r["ticker"] == "NEM"
    for k in ("sources", "stage_verdicts", "seats", "sector_phase_at_entry", "nw", "final"):
        assert r[k] is None
    assert len(r["flags_hash"]) == 12


# --------------------------------------------------------------------------- #
# write → read round-trip
# --------------------------------------------------------------------------- #

def test_write_read_round_trip(tmp_path, monkeypatch):
    from brain import decision_provenance as dp
    monkeypatch.setattr(dp, "_DIR", tmp_path / "decision_provenance", raising=False)
    rows = [dp.row("AVGO", final={"action": "add", "weight": 0.06}),
            dp.row("NEM", final={"action": "hold", "weight": 0.0})]
    dp.write("2026-07-11", rows)
    back = dp.read("2026-07-11")
    assert len(back) == 2
    assert {r["ticker"] for r in back} == {"AVGO", "NEM"}
    assert back[0]["final"]["action"] == "add"
    # a DIFFERENT asof is a separate file (no cross-day bleed)
    assert dp.read("2026-07-12") == []


def test_write_appends_across_calls(tmp_path, monkeypatch):
    from brain import decision_provenance as dp
    monkeypatch.setattr(dp, "_DIR", tmp_path / "dp", raising=False)
    dp.write("2026-07-11", [dp.row("AVGO")])
    dp.write("2026-07-11", [dp.row("NEM")])            # append, don't clobber
    back = dp.read("2026-07-11")
    assert {r["ticker"] for r in back} == {"AVGO", "NEM"}


# --------------------------------------------------------------------------- #
# fail-soft — a write failure or bad input NEVER raises
# --------------------------------------------------------------------------- #

def test_write_empty_rows_is_a_noop(tmp_path, monkeypatch):
    from brain import decision_provenance as dp
    monkeypatch.setattr(dp, "_DIR", tmp_path / "dp", raising=False)
    dp.write("2026-07-11", [])                         # must not create a file or raise
    assert dp.read("2026-07-11") == []


def test_write_swallows_a_bad_dir(monkeypatch, tmp_path):
    from brain import decision_provenance as dp
    # point _DIR at a path whose PARENT is a FILE → mkdir(parents=True) must fail internally,
    # and write() must swallow it (never raise).
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a dir")
    monkeypatch.setattr(dp, "_DIR", blocker / "sub" / "decision_provenance", raising=False)
    # must NOT raise
    dp.write("2026-07-11", [dp.row("AVGO")])
    # and the reader over the same broken path degrades to []
    assert dp.read("2026-07-11") == []


def test_write_skips_unserializable_row_keeps_the_rest(tmp_path, monkeypatch):
    from brain import decision_provenance as dp
    monkeypatch.setattr(dp, "_DIR", tmp_path / "dp", raising=False)
    good = dp.row("AVGO", final={"action": "add"})
    # a self-referential dict is a genuine serialization failure (ValueError: Circular reference);
    # write() must SKIP it and still persist the good row.
    bad = {"ticker": "BAD", "loop": None}
    bad["loop"] = bad                                  # self-referential → json.dumps raises
    dp.write("2026-07-11", [good, bad])
    back = dp.read("2026-07-11")
    tickers = {r["ticker"] for r in back}
    assert "AVGO" in tickers                           # the good row survived
    assert "BAD" not in tickers                        # the un-serializable row was skipped


def test_read_missing_and_malformed_degrade(tmp_path, monkeypatch):
    from brain import decision_provenance as dp
    d = tmp_path / "dp"
    monkeypatch.setattr(dp, "_DIR", d, raising=False)
    assert dp.read("2026-07-11") == []                 # absent → []
    # a file with a corrupt line + a good line → only the good row survives
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-07-11.jsonl").write_text(
        '{"ticker": "AVGO"}\nnot json at all\n{"ticker": "NEM"}\n')
    back = dp.read("2026-07-11")
    assert {r["ticker"] for r in back} == {"AVGO", "NEM"}


# =========================================================================== #
# phase2 wiring — the two EXTRACTABLE pure helpers (no full build invoked)
# =========================================================================== #

def test_e3_learning_fields_from_provided_reads():
    """E3: _decision_time_learning_fields derives sector_phase + divergence + NW context from the
    injected per-build reads. The market-plane fields are deterministic (they come from the injected
    market_plane arg); the per-name NW fields depend on the vendored artifact so are asserted as
    typed/nullable (present name → a value; absent → None) — the point is the extractor never raises
    and always returns the full field set."""
    from bot import phase2
    # a cycles map keyed by sector ETF (regime_frame.cycles() shape); AAPL → XLK
    cycles = {"XLK": {"phase": "Expansion", "entry_favored": True}}
    syn = {"divergences": [{"pattern": "high_confluence_buy"}]}
    out = phase2._decision_time_learning_fields(
        "AAPL", cycles=cycles, market_plane={"contradiction_count": 3,
                                             "verdict": {"label_en": "risk-off"}}, synthesis=syn)
    # deterministic — divergence from the injected synthesis, NW verdict/contradiction from the
    # injected market_plane (NOT the live artifact).
    assert out["divergence_from_sector"] == "high_confluence_buy"
    assert out["nw_verdict"] == "risk-off"
    assert out["nw_contradiction_count"] == 3
    # sector_phase resolves iff the name maps to a sector ETF present in `cycles`. In a worktree
    # WITHOUT the vendored stockdata, _default_sector_etf → None → phase None; with it, → 'Expansion'.
    assert out["sector_phase_at_entry"] in ("Expansion", None)
    # per-name NW candidate fields depend on the vendored mastermind_context.json — a value when AAPL
    # is present, None when absent. Assert typed/nullable, never a specific env-dependent value.
    assert out["nw_bottom_state"] is None or isinstance(out["nw_bottom_state"], str)
    assert out["nw_conflicts"] is None or isinstance(out["nw_conflicts"], int)
    assert out["safe_haven_diverger"] is None or isinstance(out["safe_haven_diverger"], bool)


def test_e3_learning_fields_all_none_on_empty_inputs():
    from bot import phase2
    out = phase2._decision_time_learning_fields(
        "ZZZZ", cycles={}, market_plane={}, synthesis={})
    for k in ("sector_phase_at_entry", "divergence_from_sector", "nw_bottom_state",
              "nw_conflicts", "nw_verdict", "nw_contradiction_count", "safe_haven_diverger"):
        assert out[k] is None


def test_e3_learning_fields_never_raises_on_garbage():
    from bot import phase2
    # deliberately hostile inputs must degrade to None fields, never raise
    out = phase2._decision_time_learning_fields(
        None, cycles="not a dict", market_plane=123, synthesis=["nope"])
    assert isinstance(out, dict)
    assert out["sector_phase_at_entry"] is None


def test_provenance_rows_assembles_one_row_per_candidate():
    """E2: _provenance_rows builds one replayable row per book/held/rejected candidate from the
    already-computed decision state, reading (not mutating) the shadow-book inputs."""
    from bot import phase2
    book = [{"ticker": "AVGO", "verdict": "add", "weight": 0.06, "sleeve": "conviction"}]
    gate_info = {"AVGO": {
        "full": {"synthesis": {"vetoes": [], "confluence": 0.7}},
        "breakdown": {"confirmed": True, "engine_score": 60, "research_score": 65,
                      "combined": 62},
    }}
    shadow_inputs = [{"ticker": "AVGO", "is_new": True, "retained": False,
                      "forge_confirmed": True,
                      "committee": {"action": "confirm"}, "sentinel": {"stance": "bear"},
                      "nw_context": {"bottom_state": "CONFIRMED"}}]
    research_held = [{"ticker": "NEM", "reason": "timing withhold: extended"}]
    rejected = [{"ticker": "XYZ", "reason": "veto:distress", "confluence": -0.2}]
    rows = phase2._provenance_rows(
        book=book, gate_info=gate_info, shadow_inputs=shadow_inputs,
        research_held=research_held, rejected=rejected, cycles={})
    by_t = {r["ticker"]: r for r in rows}
    assert set(by_t) == {"AVGO", "NEM", "XYZ"}
    # AVGO — confirmed book name carries the full stage chain + NW block + final
    avgo = by_t["AVGO"]
    assert avgo["final"] == {"action": "add", "weight": 0.06}
    assert avgo["stage_verdicts"]["forge_confirmed"] is True
    assert avgo["stage_verdicts"]["gate"] is True
    assert avgo["stage_verdicts"]["committee"] == "confirm"
    assert avgo["stage_verdicts"]["combined"] == 62
    assert set(avgo["seats"]) == {"committee", "sentinel"}
    assert avgo["nw"] == {"bottom_state": "CONFIRMED"}
    assert avgo["sources"]["confluence"] == 0.7
    assert len(avgo["flags_hash"]) == 12
    # NEM — held with a timing reason maps timing→withheld
    nem = by_t["NEM"]
    assert nem["final"]["action"] == "held" and nem["final"]["weight"] == 0.0
    assert nem["stage_verdicts"]["timing"] == "withheld"
    # XYZ — rejected
    xyz = by_t["XYZ"]
    assert xyz["final"]["action"] == "rejected"


def test_provenance_rows_does_not_mutate_shadow_inputs():
    """The shadow-book record must stay byte-identical — _provenance_rows only READS it."""
    import copy
    from bot import phase2
    shadow_inputs = [{"ticker": "AVGO", "is_new": True, "forge_confirmed": True,
                      "nw_context": {"bottom_state": "CONFIRMED"},
                      "committee": {"action": "confirm"}}]
    snapshot = copy.deepcopy(shadow_inputs)
    phase2._provenance_rows(
        book=[{"ticker": "AVGO", "verdict": "add", "weight": 0.06}],
        gate_info={}, shadow_inputs=shadow_inputs, research_held=[], rejected=[], cycles={})
    assert shadow_inputs == snapshot, "shadow-book inputs must be byte-identical (read-only)"


def test_provenance_rows_empty_build_is_empty():
    from bot import phase2
    assert phase2._provenance_rows(
        book=[], gate_info={}, shadow_inputs=[], research_held=[], rejected=[]) == []
