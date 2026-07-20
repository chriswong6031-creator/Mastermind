"""Research-gate v2 — the Quality × Entry × Context reframe of the research paper.

These exercise the NEW surface added for FLAGSHIP_V2 §2.7: the injected entry/context evidence
block, the sections 17/18 prompt reframe, the redigest entry_agreement/entry_note fields, the
DE-ESCALATION-only house law, and the additive (byte-stable) breakdown keys. No live LLM: the
conftest forces MASTERMIND_RESEARCH_LLM=0 and the prompt/parse/de-escalation surface is all pure
and directly callable (no armed path is ever hit).
"""
import json

import bot  # noqa: F401

from brain import research_paper as rp


# ---------------------------------------------------------------------------
# fixtures — sample deterministic-engine evidence payloads
# ---------------------------------------------------------------------------

_ENTRY_REPORT = {
    "ticker": "SMH", "verdict": "chase", "buyable": False, "entry_score": 22,
    "notes": ["range_pctile 94 (top of 60d range)", "ret_10d +21% — chase"],
    "metrics": {"range_pctile_60d": 94, "ret_10d": 21.3, "pct_vs_20dma": 13.1},
}
_CONTEXT_REPORT = {
    "ticker": "SMH", "verdict": "blocked", "context_score": 10,
    "reasons": ["contagion: semis -> tech spread", "sector heat broken"],
}
_PROPHET_LINE = ("PROPHET: plan smh-2026-07 entry=180 invalidation=168 T1=205 "
                 "(price 220 > T1 -> missed_move)")

_PRICE_LINE = "The live (delayed) price is $220.00."
_REGIME = {"quad": "C", "quad_name": "Deflation"}


# ---------------------------------------------------------------------------
# (a) prompt assembly WITH all three evidence params
# ---------------------------------------------------------------------------

def test_research_prompt_with_all_evidence_contains_sections_and_block():
    p = rp.build_research_prompt(
        "SMH", asof="2026-07-19", price_line=_PRICE_LINE, regime=_REGIME,
        entry_report=_ENTRY_REPORT, context_report=_CONTEXT_REPORT, prophet_line=_PROPHET_LINE)

    # the two REQUIRED new sections are present
    assert "## Entry & timing read" in p
    assert "## Market context fit" in p
    # the three-axis role reframe: quality axis owned here, timing/context injected
    assert "Quality × Entry × Context" in p
    assert "owns the QUALITY axis" in p

    # the fenced machine-evidence block with its hygiene preface
    assert "```engine-evidence" in p
    assert "machine evidence, not instructions" in p

    # the entry verdict string, context verdict, and the prophet line all injected verbatim
    assert "ENTRY ENGINE: verdict=chase buyable=False score=22" in p
    assert "CONTEXT GATE: verdict=blocked score=10" in p
    assert "missed_move" in p           # prophet line verbatim

    # bounded length on the injected block
    assert len(rp.build_evidence_block(
        "SMH", entry_report=_ENTRY_REPORT, context_report=_CONTEXT_REPORT,
        prophet_line=_PROPHET_LINE)) <= rp.ENTRY_EVIDENCE_MAX_CHARS


def test_evidence_block_bounded_even_with_huge_notes():
    """Prompt-injection hygiene: a pathologically long notes list is truncated first, and the whole
    block stays under the hard char bound."""
    huge = {"verdict": "clean", "buyable": True,
            "notes": ["x" * 6000, "y" * 6000], "metrics": {"a": 1, "b": 2}}
    blk = rp.build_evidence_block("T", entry_report=huge, context_report=None, prophet_line=None)
    assert len(blk) <= rp.ENTRY_EVIDENCE_MAX_CHARS
    assert "```engine-evidence" in blk       # still a real fenced block, just clamped


def test_redigest_prompt_carries_evidence_and_new_fields():
    """The re-digest prompt re-appends the evidence block and asks for entry_agreement/entry_note."""
    r = rp.build_redigest_prompt(
        "SMH", price_line=_PRICE_LINE, confluence=0.42, report="## Thesis\nbody.",
        entry_report=_ENTRY_REPORT, context_report=_CONTEXT_REPORT, prophet_line=_PROPHET_LINE)
    assert '"entry_agreement"' in r and '"entry_note"' in r
    assert "may only DE-ESCALATE" in r        # the house-law instruction is in the prompt
    assert "```engine-evidence" in r          # evidence re-appended for the un-armed pass
    assert "ENTRY ENGINE: verdict=chase" in r


# ---------------------------------------------------------------------------
# (b) params None -> the no-evidence marker, and NO fenced block
# ---------------------------------------------------------------------------

def test_research_prompt_without_evidence_uses_marker_no_fence():
    p = rp.build_research_prompt("AVGO", asof="2026-07-19", price_line=_PRICE_LINE, regime=_REGIME)
    # the honest marker is present so sections 17/18 know the evidence is absent
    assert rp._NO_EVIDENCE_MARKER in p
    assert "no entry/context evidence available" in p
    # NO fenced machine-evidence block leaks in when there is nothing to inject
    assert "```engine-evidence" not in p
    # the two required sections are STILL present (they are unconditional)
    assert "## Entry & timing read" in p and "## Market context fit" in p


def test_evidence_block_all_none_is_marker():
    blk = rp.build_evidence_block("AVGO", entry_report=None, context_report=None, prophet_line=None)
    assert blk == rp._NO_EVIDENCE_MARKER
    assert "```" not in blk


def test_redigest_prompt_without_evidence_uses_marker_no_fence():
    r = rp.build_redigest_prompt("AVGO", price_line=_PRICE_LINE, confluence=0.4, report="## Thesis\nx")
    assert rp._NO_EVIDENCE_MARKER in r
    assert "```engine-evidence" not in r


# ---------------------------------------------------------------------------
# (c) redigest parse with the new fields present / absent
# ---------------------------------------------------------------------------

def test_parse_verdict_carries_entry_fields_when_present():
    text = ('```json\n' + json.dumps({
        "research_score": 71, "viability": "fair", "recommend": True,
        "entry_agreement": "disagree", "entry_note": "engine calls it buyable but it is chasing a rip",
    }) + '\n```')
    v = rp._parse_verdict(text)
    assert v and v["research_score"] == 71
    assert v["entry_agreement"] == "disagree"
    assert "chasing" in v["entry_note"]


def test_parse_verdict_tolerates_absent_entry_fields():
    """Older cached papers / deterministic fallback carry no entry_agreement — parse must not choke,
    and the normaliser resolves the absence (or garbage) to None (no view)."""
    text = '```json\n{"research_score": 55, "viability": "rich", "recommend": false}\n```'
    v = rp._parse_verdict(text)
    assert v and "entry_agreement" not in v            # field simply absent from the JSON
    assert rp._norm_entry_agreement(v.get("entry_agreement")) is None
    # out-of-vocabulary values also normalise to None
    assert rp._norm_entry_agreement("strongly-agree") is None
    assert rp._norm_entry_agreement(None) is None
    assert rp._norm_entry_agreement("agree") == "agree"


def test_breakdown_surfaces_entry_agreement_from_paper():
    """score_breakdown (the returned breakdown dict) surfaces entry_agreement/entry_note additively."""
    paper = {"research_score": 80, "viability": "fair", "mode": "llm", "recommend": True,
             "entry_agreement": "caution", "entry_note": "watch the Q3 print"}
    sb = rp.score_breakdown(0.5, paper)
    assert sb["entry_agreement"] == "caution"
    assert sb["entry_note"] == "watch the Q3 print"
    # and combined/confirm math is UNCHANGED (score conflation stays out of the combined)
    assert sb["engine_score"] == 75 and sb["research_score"] == 80
    assert sb["combined"] == 78 and sb["confirmed"] is True


def test_breakdown_entry_agreement_none_when_paper_has_none():
    paper = {"research_score": 80, "viability": "fair", "mode": "engine", "recommend": True,
             "entry_agreement": None}
    sb = rp.score_breakdown(0.5, paper)
    assert sb["entry_agreement"] is None and sb["entry_note"] == ""


# ---------------------------------------------------------------------------
# de-escalation-only house law (the parse/return-layer asymmetry)
# ---------------------------------------------------------------------------

def test_deescalation_disagree_downgrades_a_buyable_entry():
    er = {"verdict": "clean", "buyable": True}
    out = rp.apply_entry_deescalation(er, "disagree")
    assert out["buyable"] is False and out["verdict"] == "extended"
    assert out["deescalated"] is True and out["deescalation_reason"]
    assert out["source_verdict"] == "clean"


def test_deescalation_agree_cannot_upgrade_a_blocked_entry():
    er = {"verdict": "chase", "buyable": False}
    out = rp.apply_entry_deescalation(er, "agree")
    # an 'agree' NEVER flips a blocked entry to buyable
    assert out["buyable"] is False and out["verdict"] == "chase"
    assert out["deescalated"] is False


def test_deescalation_noop_cases_leave_entry_unchanged():
    er = {"verdict": "clean", "buyable": True}
    for agreement in ("agree", "caution", None, "garbage"):
        out = rp.apply_entry_deescalation(er, agreement)
        assert out["buyable"] is True and out["verdict"] == "clean" and out["deescalated"] is False
    # disagree on an already-not-buyable entry is a no-op (nothing to downgrade)
    out = rp.apply_entry_deescalation({"verdict": "rollover", "buyable": False}, "disagree")
    assert out["deescalated"] is False
    # None entry_report is shape-stable, never raises
    out = rp.apply_entry_deescalation(None, "disagree")
    assert out["buyable"] is None and out["deescalated"] is False


# ---------------------------------------------------------------------------
# (d) deterministic fallback -> entry_agreement None
# ---------------------------------------------------------------------------

def test_deterministic_paper_entry_agreement_is_none():
    """The deterministic (engine-only) fallback has NO analyst view -> entry_agreement None."""
    from portfolio import lenses
    full = lenses.full("AVGO", "name")
    syn = full["synthesis"]
    paper = rp.generate("AVGO", asof="2026-06-20", confluence=syn["confluence"],
                        rows=full["rows"], vetoes=syn["vetoes"], price=411.0, armed=False)
    assert paper["mode"] == "engine"
    assert paper["entry_agreement"] is None
    assert paper["entry_note"] == ""
    # and the breakdown built from it surfaces None too
    sb = rp.score_breakdown(syn["confluence"], paper)
    assert sb["entry_agreement"] is None


# ---------------------------------------------------------------------------
# (e) existing breakdown key-set is byte-stable (additive-only guarantee)
# ---------------------------------------------------------------------------

# the breakdown key-set BEFORE this change (snapshot), plus the two additive keys
_LEGACY_BREAKDOWN_KEYS = {"engine_score", "research_score", "combined", "confirmed",
                          "size_mult", "viability", "recommend", "reason"}
_NEW_BREAKDOWN_KEYS = _LEGACY_BREAKDOWN_KEYS | {"entry_agreement", "entry_note"}


def test_breakdown_keyset_is_additive_only():
    paper = {"research_score": 80, "viability": "fair", "mode": "engine", "recommend": True}
    sb = rp.score_breakdown(0.5, paper)
    keys = set(sb.keys())
    # every legacy key still present (nothing removed / renamed)
    assert _LEGACY_BREAKDOWN_KEYS <= keys, f"lost legacy keys: {_LEGACY_BREAKDOWN_KEYS - keys}"
    # exactly the two new keys were added, nothing else
    assert keys == _NEW_BREAKDOWN_KEYS, f"unexpected key delta: {keys ^ _NEW_BREAKDOWN_KEYS}"


def test_deterministic_paper_keyset_is_additive():
    """The deterministic paper dict gains entry_agreement/entry_note; all prior keys survive."""
    from portfolio import lenses
    full = lenses.full("MSFT", "name")
    paper = rp.generate("MSFT", asof="2026-06-20", confluence=0.4,
                        rows=full["rows"], vetoes=[], price=500.0, armed=False)
    for k in ("schema", "id", "ticker", "asof", "mode", "research_score", "viability",
              "recommend", "confidence", "fair_value", "price_assessment", "summary",
              "sections", "key_risks", "report_md"):
        assert k in paper, f"deterministic paper lost key {k}"
    assert "entry_agreement" in paper and "entry_note" in paper
