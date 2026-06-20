"""The Research Desk gate — the holistic paper that must confirm every buy.

These exercise the DETERMINISTIC path (the conftest forces MASTERMIND_RESEARCH_LLM=0 and
redirects the papers/notes dirs to a tmp dir), the verdict parser, the combined gate math,
and the phase2 integration. No Claude auth / network needed.
"""
import json

import bot  # noqa: F401

from brain import research_paper as rp
from portfolio import lenses


# ---------------------------------------------------------------------------
# deterministic report
# ---------------------------------------------------------------------------

def test_deterministic_paper_has_all_sections():
    full = lenses.full("AVGO", "name")
    syn = full["synthesis"]
    paper = rp.generate("AVGO", asof="2026-06-20", confluence=syn["confluence"],
                        rows=full["rows"], vetoes=syn["vetoes"], price=411.0, armed=False)
    assert paper["mode"] == "engine"
    assert paper["ticker"] == "AVGO"
    # every required section is present and non-empty
    for key, _disp in rp.SECTION_ORDER:
        assert key in paper["sections"], f"missing section {key}"
        assert paper["sections"][key], f"empty section {key}"
    assert 0 <= paper["research_score"] <= 100
    assert paper["viability"] in rp.VIABILITIES
    assert paper["report_md"] and "## Thesis" in paper["report_md"]
    assert paper["price_at_review"] == 411.0


def test_deterministic_score_responds_to_fundamentals():
    """A cheaper, growing name should not score below a pricey, decelerating one given equal
    confluence — the score must reflect the fundamental lenses, not only confluence."""
    cheap = [{"lens": "valuation", "value": {"value_z": 1.0}, "status": "context", "direction": "bull"},
             {"lens": "growth", "value": {"rev_cagr": 25}, "status": "partial", "direction": "bull"}]
    rich = [{"lens": "valuation", "value": {"value_z": -1.0}, "status": "context", "direction": "bear"},
            {"lens": "growth", "value": {"rev_cagr": -5}, "status": "partial", "direction": "bear"}]
    s_cheap = rp._deterministic_score(cheap, 0.4)
    s_rich = rp._deterministic_score(rich, 0.4)
    assert s_cheap > s_rich


# ---------------------------------------------------------------------------
# verdict parser
# ---------------------------------------------------------------------------

def test_parse_verdict_fenced():
    text = 'Here is my verdict.\n```json\n{"research_score": 78, "viability": "fair", "recommend": true}\n```\nDone.'
    v = rp._parse_verdict(text)
    assert v and v["research_score"] == 78 and v["viability"] == "fair"


def test_parse_verdict_bare_braces():
    text = 'prose {"research_score": 40, "viability": "avoid", "recommend": false} more prose'
    v = rp._parse_verdict(text)
    assert v and v["research_score"] == 40 and v["recommend"] is False


def test_parse_verdict_none_when_absent():
    assert rp._parse_verdict("no json here at all") is None
    assert rp._parse_verdict("") is None


_SAMPLE_REPORT = """## Thesis
AVGO is the picks-and-shovels winner of custom AI silicon, compounding at a premium.

## Pros
- Custom-ASIC moat with hyperscaler design wins
- VMware adds durable, high-margin recurring software

## Cons
- Rich valuation leaves little room for a miss
- Customer concentration in a few hyperscalers

## Valuation
~35x forward earnings, a premium to the SOX but justified by mix shift to software.

## Fundamentals
60%+ gross margins, strong FCF conversion, manageable post-VMware leverage.

## Revenue streams
Semiconductor solutions (networking, custom ASICs, wireless) plus infrastructure software.

## Confirmed catalysts
- VMware integration ahead of plan
- Raised AI revenue guide last quarter

## Pending catalysts
- Next-gen custom accelerator ramp in 2H

## Potential catalysts
- (30%) A large software acquisition re-rates the multiple higher
- (20%) An export-control shock hits the China mix — downside

## Forward earnings (recalculated)
I model next-year EPS ~$6.50 on ~20% AI growth, modestly above consensus.

## Bull / base / bear scenarios
Bull (30%) $600; base (50%) $480; bear (20%) $340 — the swing factor is the FY27 AI guide.

## Second- and third-order effects
- TSMC and HBM suppliers benefit from the ASIC ramp
- Merchant-GPU competitors lose share at the margin
- Power and cooling vendors are the two-steps-removed winners

## Variant perception — what the market is missing
Consensus over-anchored on the guide that *didn't* rise; the printed ramp is the edge, not the trap.

## What to watch
- Q3 AI revenue print vs the $16B guide
- A break of the $380 put wall would change the entry

## Other factors
Options positioning is constructive; management execution is best-in-class.
"""


def test_split_sections_parses_real_markdown():
    """The one integration point not covered by the deterministic path: parsing a real
    Claude-style markdown report into the section dict (incl. the deep-reasoning sections)."""
    secs = rp._split_sections(_SAMPLE_REPORT)
    # list sections become lists; prose sections become strings
    assert isinstance(secs["pros"], list) and len(secs["pros"]) == 2
    assert isinstance(secs["cons"], list) and "Rich valuation" in secs["cons"][0]
    assert isinstance(secs["confirmed_catalysts"], list) and len(secs["confirmed_catalysts"]) == 2
    assert "picks-and-shovels" in secs["thesis"]
    assert "forward_earnings" in secs and "$6.50" in secs["forward_earnings"]
    assert "Revenue" not in secs["revenue_streams"]   # heading stripped, body kept
    assert "infrastructure software" in secs["revenue_streams"]
    # the new deep-reasoning sections parse too
    assert isinstance(secs["potential_catalysts"], list) and len(secs["potential_catalysts"]) == 2
    assert isinstance(secs["second_third_order"], list) and len(secs["second_third_order"]) == 3
    assert isinstance(secs["what_to_watch"], list) and len(secs["what_to_watch"]) == 2
    assert "swing factor" in secs["scenarios"]
    assert "edge, not the trap" in secs["variant_perception"]


# ---------------------------------------------------------------------------
# combined gate (engine buy-score + research score)
# ---------------------------------------------------------------------------

def test_engine_score_mapping():
    assert rp.engine_score(0.0) == 50
    assert rp.engine_score(0.3) == 65
    assert rp.engine_score(0.5) == 75
    assert rp.engine_score(1.0) == 100


def test_score_breakdown_confirms_and_sizes():
    paper = {"research_score": 80, "viability": "fair", "mode": "engine", "recommend": True}
    sb = rp.score_breakdown(0.5, paper)            # engine 75, research 80 -> combined 78 (rounds to 78)
    assert sb["engine_score"] == 75 and sb["research_score"] == 80
    assert sb["combined"] == 78 and sb["confirmed"] is True
    assert 0.5 <= sb["size_mult"] <= 1.3


def test_score_breakdown_blocks_low_combined():
    paper = {"research_score": 30, "viability": "rich", "mode": "engine", "recommend": True}
    sb = rp.score_breakdown(0.31, paper)           # engine ~66, research 30 -> combined 48 < 60
    assert sb["confirmed"] is False and sb["size_mult"] == 0.0
    assert "combined" in sb["reason"]


def test_score_breakdown_blocks_avoid_even_if_combined_high():
    paper = {"research_score": 95, "viability": "avoid", "mode": "engine", "recommend": True}
    sb = rp.score_breakdown(0.9, paper)
    assert sb["confirmed"] is False and "avoid" in sb["reason"]


def test_score_breakdown_blocks_llm_no_recommend():
    paper = {"research_score": 90, "viability": "fair", "mode": "llm", "recommend": False}
    sb = rp.score_breakdown(0.9, paper)
    assert sb["confirmed"] is False and "recommend" in sb["reason"]


def test_size_mult_bounds_and_monotonic():
    lo = rp.score_breakdown(0.31, {"research_score": 60, "viability": "fair", "mode": "engine", "recommend": True})
    hi = rp.score_breakdown(1.0, {"research_score": 100, "viability": "compelling", "mode": "engine", "recommend": True})
    assert lo["size_mult"] >= 0.5 and hi["size_mult"] <= 1.3
    assert hi["size_mult"] >= lo["size_mult"]


# ---------------------------------------------------------------------------
# persistence (isolated to tmp dir by conftest)
# ---------------------------------------------------------------------------

def test_save_load_latest_roundtrip():
    paper = rp.generate("MSFT", asof="2026-06-20", confluence=0.4,
                        rows=lenses.full("MSFT", "name")["rows"], vetoes=[], price=500.0, armed=False)
    rp.save_paper(paper)
    papers = rp.load_papers()
    assert any(p["id"] == paper["id"] for p in papers)
    latest = rp.latest_for("MSFT")
    assert latest and latest["ticker"] == "MSFT"


def test_write_feed_note_is_parseable():
    from app.web import _parse_note
    paper = rp.generate("AAPL", asof="2026-06-20", confluence=0.4,
                        rows=lenses.full("AAPL", "name")["rows"], vetoes=[], price=240.0, armed=False)
    path = rp.write_feed_note(paper)
    note = _parse_note(path)
    assert note and "AAPL" in note["tickers"] and note["title"]
    assert note["date"]            # ISO timestamp present so the feed can sort it


def test_llm_disabled_under_pytest():
    # conftest sets MASTERMIND_RESEARCH_LLM=0 for the whole session
    assert rp.llm_enabled() is False


# ---------------------------------------------------------------------------
# phase2 integration — the gate stands between a name and the book
# ---------------------------------------------------------------------------

def test_phase2_attaches_research_to_conviction_and_holds():
    from pathlib import Path
    from bot import phase2
    db = Path(__file__).resolve().parent.parent / "data" / "bot.db"
    if db.exists():
        db.unlink()
    out = phase2.run(force=True)
    assert out["ran"]
    assert "research_held" in out                  # the discipline surface exists
    conv = [p for p in out["book"] if p["sleeve"] == "conviction"]
    for p in conv:
        rb = p.get("research")
        assert rb, f"{p['ticker']} confirmed without a research paper"
        assert rb["confirmed"] is True
        assert rb["combined"] >= rp.CONFIRM_THRESHOLD
        assert rb["viability"] in rp.VIABILITIES
        assert p["weight"] <= 0.08 + 1e-9          # research scaling never breaches the name cap
    # every research-held name failed the gate for a stated reason
    for h in out["research_held"]:
        assert h["confirmed"] is False and h["reason"]
    if db.exists():
        db.unlink()
