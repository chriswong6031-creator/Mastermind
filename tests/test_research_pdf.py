"""Guards for the on-the-spot research-paper PDF engine (app/research_pdf).

The engine renders WITHOUT AI, deterministically, so the real risk is robustness across
wildly varying content: long reports, empty sections, XML-special chars, ragged markdown
tables, malformed emphasis, missing fields. These tests prove it always emits a valid PDF
and never raises on content — exactly the failure mode flagged for this feature.
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

import pytest

from app import research_pdf as R

_PAPERS = glob.glob(str(Path(__file__).resolve().parent.parent / "data" / "research" / "papers" / "*.json"))


def _valid_pdf(b: bytes) -> bool:
    return isinstance(b, (bytes, bytearray)) and b[:4] == b"%PDF" and b"%%EOF" in b[-2048:]


def test_tags_balanced():
    assert R._tags_balanced("<b>x</b>")
    assert R._tags_balanced("<b>a <i>b</i> c</b>")
    assert not R._tags_balanced("<b>a <i>b</b></i>")   # mis-nested
    assert not R._tags_balanced("<b>unclosed")
    assert R._tags_balanced("plain text, no tags")


def test_escaping_neutralises_xml():
    # a stray '<', '&', '>' must never reach ReportLab unescaped
    out = R._esc("P<E & margins > 50% <NVDA>")
    assert "<" not in out and ">" not in out
    assert "&amp;" in out and "&lt;" in out and "&gt;" in out


def test_malformed_emphasis_falls_back_to_plain():
    # overlapping markers would mis-nest; _p must fall back, not crash
    para = R._p("**bold *italic** weird* nesting", R._styles()["body"])
    assert para is not None


@pytest.mark.skipif(not _PAPERS, reason="no saved research papers present")
def test_all_saved_papers_render():
    for f in _PAPERS:
        paper = json.load(open(f))
        b = R.build(paper, {})
        assert _valid_pdf(b), f"invalid PDF for {os.path.basename(f)}"


@pytest.mark.parametrize("paper", [
    {},                                                              # totally empty
    {"ticker": "X"},                                                # ticker only
    {"ticker": "LONG", "report_md": "## H\n\n" + ("word " * 5000)},  # multi-page flow
    {"ticker": "XML", "report_md": "a < b & c > d, P<E, M&A, 3<5x",
     "key_risks": ["risk with < and & and > chars"], "summary": "1 < 2 & 3 > 0"},
    {"ticker": "TBL", "report_md": "| Lens | Dir | Val |\n|---|---|---|\n| Trend | up | 1.2 |\n| Carry | mixed |\n| x |"},
    {"ticker": "BAD", "report_md": "**bold *italic** stray * and ** and `code"},
    {"ticker": "LINK", "report_md": "see [SEC](https://sec.gov/x?a=1&b=2) and [bad](notaurl)"},
    {"ticker": "BUL", "report_md": "- a\n- b\n* c\n1. d\n2. e\n\n---\n\nplain"},
    {"ticker": "FV", "fair_value": 245, "price_at_review": 210.69, "engine_score": 55,
     "research_score": 52, "combined": 54, "viability": "rich", "confirmed": False},
    # malformed-shape guards (these used to crash build() — see adversarial review)
    {"ticker": "ZP", "fair_value": 100, "price_at_review": "0"},      # string-zero price → no ZeroDivisionError
    {"ticker": "ZP2", "fair_value": 100, "price_at_review": "0.0"},
    {"ticker": "MD", "report_md": {"k": "v"}},                        # non-string report_md
    {"ticker": "MD2", "report_md": 42},
    {"ticker": "KR", "key_risks": 42},                               # non-iterable key_risks
    {"ticker": "KR2", "key_risks": {"a": 1}},
    {"ticker": "VB", "viability": 123},                              # non-string viability
])
def test_edge_cases_never_crash(paper):
    b = R.build(paper, {})
    assert _valid_pdf(b)


def test_meta_fields_render():
    paper = {"ticker": "NVDA", "fair_value": 245, "price_at_review": 210.69,
             "combined": 54, "engine_score": 55, "research_score": 52, "viability": "rich",
             "report_md": "## Thesis\n\nGreat company.", "key_risks": ["concentration"]}
    meta = {"name": "Nvidia Corp", "sector": "Technology", "price": 211.0, "market_cap": 5.2e12,
            "fwd_pe": 34.0, "div_yield": 0.0003, "analyst_target": 260, "rating": "Buy",
            "rev_growth": 0.85, "gross_margin": 0.75, "net_margin": 0.55, "roe": 1.1,
            "description": "Designs GPUs.", "next_earnings": "Aug 20, 2026"}
    assert _valid_pdf(R.build(paper, meta))
    # None-laden meta must also be safe
    assert _valid_pdf(R.build(paper, {k: None for k in meta}))


def test_endpoint_returns_pdf():
    import bot  # noqa: F401 — bootstraps vendor/macro
    from fastapi import FastAPI
    from starlette.testclient import TestClient
    from app import web
    from brain import research_paper

    # conftest isolates the papers dir to a tmp path, so seed one paper for the endpoint to find
    paper = {"id": "test-NVDA", "ticker": "NVDA", "asof": "2026-06-18",
             "generated_at": "2026-06-18T00:00:00Z", "fair_value": 245, "engine_score": 55,
             "research_score": 52, "combined": 54, "viability": "rich", "confirmed": False,
             "report_md": "## Thesis\n\nGreat company with **observed** demand.",
             "summary": "Strong franchise.", "key_risks": ["concentration"],
             "mode": "llm", "schema": "research_paper.v1"}
    research_paper.save_paper(paper)
    pid = "test-NVDA"
    app = FastAPI(); app.include_router(web.router)
    c = TestClient(app)
    r = c.get("/research_paper.pdf", params={"id": pid})
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/pdf")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert r.content[:4] == b"%PDF"
    # unknown id -> clean 404, not a 500
    assert c.get("/research_paper.pdf", params={"id": "nope-xyz"}).status_code == 404
