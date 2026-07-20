"""P2 funnel — the Neural-Web WHOLE-UNIVERSE candidacy scan in the conviction sleeve.

CARDINAL RULE under test: with MASTERMIND_NW_DECISION at its default (off), the scan returns [] and
candidates() is BYTE-IDENTICAL to today. The scan is additive, deduped-by-ticker, capped at
NW_UNIVERSE_SCAN_CAP, honours _MANUAL_EXCLUDE, and is fully fail-soft (never raises into a build).

All NW state is monkeypatched onto brain.neural_web_context so the tests are fully offline and do not
depend on the live vendor artifact. nw_universe_scan() imports the leaf lazily as ``nwc`` and calls
``nwc.nw_decision_mode`` / ``nwc.context`` / ``nwc.decision_signals`` / ``nwc._mode_ge`` — so patching
those module attributes drives the scan deterministically. We keep the REAL _mode_ge so the ladder
threshold (candidacy) is evaluated honestly against the patched mode string.
"""
import bot  # noqa: F401

from brain import neural_web_context as nwc
from portfolio import conviction


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _ctx(*tickers: str) -> dict:
    """A minimal NW context artifact carrying a candidate_context keyed by ticker."""
    return {"candidate_context": {t: {"_stub": True} for t in tickers}}


def _signals_for(qualifying: set[str]):
    """A decision_signals stand-in: a name in `qualifying` gets a non-None candidacy dict
    (fdr-cleared + qualifying bottom_state), everything else gets candidacy None (inert/uncleared)."""
    def _fn(ticker: str) -> dict:
        sym = str(ticker).upper()
        if sym in qualifying:
            return {"candidacy": {"state": "BOTTOMING", "score": 0.5, "lean": +1},
                    "entry_shrink": None, "clean_in_conflicted": False,
                    "inert": False, "mode": "candidacy"}
        return {"candidacy": None, "entry_shrink": None, "clean_in_conflicted": False,
                "inert": True, "mode": "candidacy"}
    return _fn


def _arm_candidacy(monkeypatch, *, ctx: dict, qualifying: set[str]):
    """Drive the NW leaf into candidacy mode with a given context + qualifying set."""
    monkeypatch.setattr(nwc, "nw_decision_mode", lambda: "candidacy")
    monkeypatch.setattr(nwc, "context", lambda: ctx)
    monkeypatch.setattr(nwc, "decision_signals", _signals_for(qualifying))


# --------------------------------------------------------------------------- #
# 1. OFF (default) → byte-identical no-op
# --------------------------------------------------------------------------- #
def test_scan_empty_when_flag_unset(monkeypatch):
    """W8 (2026-07-19): the DEFAULT is now 'shrink' (candidacy armed, operator-ordered). The
    explicit-off contract remains: MASTERMIND_NW_DECISION=off → scan is []."""
    monkeypatch.delenv("MASTERMIND_NW_DECISION", raising=False)
    assert nwc.nw_decision_mode() == "shrink"          # W8 default
    monkeypatch.setenv("MASTERMIND_NW_DECISION", "off")
    assert nwc.nw_decision_mode() == "off"
    assert conviction.nw_universe_scan() == []


def test_scan_empty_below_candidacy_even_with_ctx(monkeypatch):
    """shadow mode is BELOW candidacy on the ladder → scan is [] even though a rich NW ctx exists and
    names would qualify. Only candidacy+ arms the passthrough."""
    monkeypatch.setattr(nwc, "nw_decision_mode", lambda: "shadow")
    monkeypatch.setattr(nwc, "context", lambda: _ctx("AAA", "BBB"))
    monkeypatch.setattr(nwc, "decision_signals", _signals_for({"AAA", "BBB"}))
    assert conviction.nw_universe_scan() == []


# --------------------------------------------------------------------------- #
# 2. candidacy mode → qualifying tickers, capped + deduped
# --------------------------------------------------------------------------- #
def test_scan_returns_qualifying_tickers(monkeypatch):
    """At candidacy mode, every fdr-cleared + qualifying-bottom_state name is returned; a name whose
    decision_signals()['candidacy'] is None (uncleared / non-qualifying state) is EXCLUDED."""
    _arm_candidacy(monkeypatch, ctx=_ctx("AAA", "BBB", "CCC"), qualifying={"AAA", "CCC"})
    out = conviction.nw_universe_scan()
    assert set(out) == {"AAA", "CCC"}          # BBB (candidacy None) is filtered out
    assert "BBB" not in out


def test_scan_uppercases_and_dedups(monkeypatch):
    """Tickers are normalised to upper-case; the internal `seen` guard means no ticker appears twice."""
    _arm_candidacy(monkeypatch, ctx={"candidate_context": {"aaa": {}, "bbb": {}}},
                   qualifying={"AAA", "BBB"})
    out = conviction.nw_universe_scan()
    assert set(out) == {"AAA", "BBB"}
    assert len(out) == len(set(out))           # no duplicates


def test_scan_caps_at_module_constant(monkeypatch):
    """More qualifying names than NW_UNIVERSE_SCAN_CAP → the scan is truncated to the cap."""
    n = conviction.NW_UNIVERSE_SCAN_CAP + 15
    names = [f"T{i:03d}" for i in range(n)]
    _arm_candidacy(monkeypatch, ctx=_ctx(*names), qualifying=set(names))
    out = conviction.nw_universe_scan()
    assert len(out) == conviction.NW_UNIVERSE_SCAN_CAP
    assert conviction.NW_UNIVERSE_SCAN_CAP == 25   # documented unverified prior


def test_scan_respects_manual_exclude(monkeypatch):
    """A name on the operational _MANUAL_EXCLUDE hold-out is dropped from the scan even if it would
    otherwise qualify (the do-not-auto-re-add guard applies to the NW source too)."""
    excl = next(iter(conviction._MANUAL_EXCLUDE))   # a real held-out ticker (NVDA/AVGO)
    _arm_candidacy(monkeypatch, ctx=_ctx(excl, "GOODX"), qualifying={excl, "GOODX"})
    out = conviction.nw_universe_scan()
    assert excl not in out
    assert "GOODX" in out


# --------------------------------------------------------------------------- #
# 3. candidates() unchanged when the scan is empty (byte-identical)
# --------------------------------------------------------------------------- #
def test_candidates_byte_identical_when_scan_empty(monkeypatch):
    monkeypatch.setenv("MASTERMIND_NW_DECISION", "off")          # W8: default flipped; pin legacy
    monkeypatch.setenv("MASTERMIND_PROPHET_FEED", "0")            # W8: prophet source off for byte-identity
    from portfolio import prophet_feed as _pf; _pf._reset_cache()
    """The union guard: when nw_universe_scan() == [] the candidates() output is IDENTICAL to what it
    would be with the scan removed entirely. Force the scan empty (default-off surrogate) and compare
    against the real candidates() computed with the same underlying sources."""
    # Neutralise ALL live candidates() sources (seed, universe, ledger AND the intake queue) so the
    # comparison isolates the union behaviour deterministically. candidates() also unions
    # `set(intake.tickers(...))`; leaving it live would leak the real queue into the comparison.
    monkeypatch.setattr(conviction, "regime_seed", lambda: ["SEEDX"])
    monkeypatch.setattr(conviction, "universe", lambda: ["UNIX"])
    monkeypatch.setattr("brain.ledger.all_theses",
                        lambda: [{"subject": "PROPX", "status": "open"}])
    monkeypatch.setattr("brain.intake.tickers", lambda *a, **k: [])

    # WITH an empty scan (forced):
    monkeypatch.setattr(conviction, "nw_universe_scan", lambda: [])
    with_empty = conviction.candidates()

    # The union of the same sources computed WITHOUT the nw term at all (the pre-P2 shape):
    expected = sorted(({"SEEDX"} | {"UNIX"} | {"PROPX"}) - conviction._MANUAL_EXCLUDE)
    assert with_empty == expected


def test_candidates_unions_scan_when_armed(monkeypatch):
    """When the scan returns names, they are ADDITIVELY unioned into candidates(), deduped by ticker
    (an NW name that already exists in another source is not duplicated)."""
    monkeypatch.setattr(conviction, "regime_seed", lambda: ["SEEDX"])
    monkeypatch.setattr(conviction, "universe", lambda: ["DUPX"])   # also emitted by the scan below
    monkeypatch.setattr("brain.ledger.all_theses", lambda: [])
    monkeypatch.setattr("brain.intake.tickers", lambda *a, **k: [])
    monkeypatch.setattr(conviction, "nw_universe_scan", lambda: ["NWX", "DUPX"])

    cands = conviction.candidates()
    assert "NWX" in cands                       # additive NW name enters the pool
    assert "SEEDX" in cands and "DUPX" in cands
    assert cands.count("DUPX") == 1             # deduped — present once despite two sources
    assert cands == sorted(set(cands))          # sorted + unique (the candidates() contract)


# --------------------------------------------------------------------------- #
# 4. fail-soft: an exception in the NW leaf never breaks the scan or candidates()
# --------------------------------------------------------------------------- #
def _boom(*_a, **_k):
    raise RuntimeError("neural web artifact exploded")


def test_scan_failsoft_on_context_raise(monkeypatch):
    """context() raising at candidacy mode → the scan swallows it and returns [] (never propagates)."""
    monkeypatch.setattr(nwc, "nw_decision_mode", lambda: "candidacy")
    monkeypatch.setattr(nwc, "context", _boom)
    assert conviction.nw_universe_scan() == []


def test_scan_failsoft_on_decision_signals_raise(monkeypatch):
    """A per-row decision_signals() raising must not sink the whole scan — the bad row is skipped and
    the remaining qualifying names still come through."""
    monkeypatch.setattr(nwc, "nw_decision_mode", lambda: "candidacy")
    monkeypatch.setattr(nwc, "context", lambda: _ctx("BADX", "GOODX"))

    def _sig(ticker):
        if str(ticker).upper() == "BADX":
            raise RuntimeError("row blew up")
        return {"candidacy": {"state": "BOTTOMING", "score": 0.5, "lean": +1}}
    monkeypatch.setattr(nwc, "decision_signals", _sig)

    out = conviction.nw_universe_scan()
    assert out == ["GOODX"]                      # bad row skipped, good row retained


def test_candidates_still_works_when_scan_would_raise(monkeypatch):
    """Even if the NW leaf is entirely broken, candidates() must still assemble the OTHER sources —
    nw_universe_scan() is fail-soft, so the union simply gets [] and the build proceeds."""
    monkeypatch.setattr(conviction, "regime_seed", lambda: ["SEEDX"])
    monkeypatch.setattr(conviction, "universe", lambda: [])
    monkeypatch.setattr("brain.ledger.all_theses", lambda: [])
    monkeypatch.setattr("brain.intake.tickers", lambda *a, **k: [])
    # Drive the real nw_universe_scan() through a broken leaf (context raises) — it must degrade to [].
    monkeypatch.setattr(nwc, "nw_decision_mode", lambda: "candidacy")
    monkeypatch.setattr(nwc, "context", _boom)

    cands = conviction.candidates()             # must NOT raise
    assert "SEEDX" in cands
