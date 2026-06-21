"""The Research Desk — a full holistic research paper that gates EVERY buy.

This is the discipline the user asked for: before a name the engine wants to buy can
actually be purchased, the Brain writes a complete, human-readable research report on the
company (thesis, pros/cons, valuation, fundamentals, revenue streams, confirmed + pending
catalysts, a *recalculated* forward-earnings view, and other factors), then **re-digests**
that report to confirm the company is still a viable investment AT THIS PRICE. The re-digest
emits a research_score (0-100). That score is combined with the engine "buy score" (the
confluence from the multi-sided decision matrix); only a combined score over the confirm bar
lets the purchase go through — and the combined score also scales the position size, within
the engine's existing per-name cap. Paper-only. The engine still owns the hard vetoes and
the falsifier; a research paper can confirm, veto, or size a buy, but it can never rescue a
hard-vetoed name (parabolic / Altman distress / cycle-blocked).

Two production modes:
  - "llm"    : an ARMED Claude session writes the report (web search + the bot's read tools),
               then a second un-armed pass re-digests it into the verdict JSON. This is the
               default for a NEW buy. Reuses brain.cli_bridge (same path research_desk uses).
  - "engine" : a deterministic report + score composed straight from the decision matrix.
               No network, no tokens — the honest fallback for tests / CI / offline, and for
               carried names that already have a paper.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import bot  # noqa: F401  -> vendor/macro onto sys.path

from brain import cli_bridge


def llm_enabled() -> bool:
    """Whether the ARMED (Claude) report path may run. On by default in production; tests and
    CI set MASTERMIND_RESEARCH_LLM=0 to force the deterministic path (no tokens, no network)."""
    if os.environ.get("MASTERMIND_RESEARCH_LLM", "1").strip().lower() in ("0", "false", "no", ""):
        return False
    return cli_bridge.available()

_ROOT = Path(__file__).resolve().parent.parent
_PAPERS = _ROOT / "data" / "research" / "papers"
_INDEX = _PAPERS / "index.jsonl"
_NOTES = _ROOT / "data" / "research" / "notes"

SCHEMA = "research_paper.v1"
CONFIRM_THRESHOLD = 60            # combined "Conviction Index" needed to confirm a NEW buy
# hysteresis: a name we ALREADY hold stays confirmed down to a lower bar, so a carried position
# isn't churned out of the book on a marginal combined-score wobble around the 60 line. A genuine
# exit (viability='avoid' or, upstream, a hard veto) still drops it immediately.
_HELD_HYSTERESIS = 8
VIABILITIES = ("compelling", "fair", "rich", "avoid")

# the holistic report's required sections, in render order: (key, display heading)
SECTION_ORDER: list[tuple[str, str]] = [
    ("thesis", "Thesis"),
    ("pros", "Pros"),
    ("cons", "Cons"),
    ("valuation", "Valuation"),
    ("fundamentals", "Fundamentals"),
    ("revenue_streams", "Revenue streams"),
    ("competitive_landscape", "Competitive landscape & moat"),
    ("confirmed_catalysts", "Confirmed catalysts"),
    ("pending_catalysts", "Pending catalysts"),
    ("potential_catalysts", "Potential catalysts"),
    ("forward_earnings", "Forward earnings (recalculated)"),
    ("scenarios", "Bull / base / bear scenarios"),
    ("second_third_order", "Second- and third-order effects"),
    ("variant_perception", "Variant perception — what the market is missing"),
    ("what_to_watch", "What to watch"),
    ("other_factors", "Other factors"),
    ("redigest", "Re-digest — viability at this price"),
]
_LIST_SECTIONS = {"pros", "cons", "confirmed_catalysts", "pending_catalysts",
                  "potential_catalysts", "second_third_order", "what_to_watch"}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def engine_score(confluence: float) -> int:
    """Map the engine's confluence (the 'buy score' from models/signals) onto 0-100.

    confluence 0.0 -> 50, 0.3 -> 65, 0.5 -> 75, 1.0 -> 100. The conviction gate only
    sizes a name when confluence > 0.3, so a sized name's engine_score is >= ~65."""
    return int(round(_clamp(50 + (confluence or 0.0) * 50.0, 0, 100)))


# ---------------------------------------------------------------------------
# combined gate — research score + engine buy-score -> confirm / veto / size
# ---------------------------------------------------------------------------

def score_breakdown(confluence: float, paper: dict, held: bool = False) -> dict:
    """Combine the engine buy-score and the paper's research_score into the decision.

    Returns {engine_score, research_score, combined, confirmed, size_mult, viability,
             recommend, reason}. `combined` is the "Conviction Index" shown on the page.

    Rules (doctrine-faithful):
      - combined = round(0.5*engine_score + 0.5*research_score)
      - BLOCK if combined < the confirm bar, or viability == 'avoid', or (LLM mode and
        the paper does not recommend). A blocked buy is held (size 0) — never executed.
      - `held` lowers the confirm bar by _HELD_HYSTERESIS so a CARRIED position isn't churned
        out on a marginal wobble (a new buy still needs the full CONFIRM_THRESHOLD).
      - When confirmed, size_mult scales the engine weight: 60->0.5 (starter), 80->1.0,
        >=92->1.3 (boost). The caller re-caps at the per-name cap, so a boost never breaches
        the cap; a hard-vetoed name never reaches this gate at all.
    """
    es = engine_score(confluence)
    rs = int(round(_clamp(paper.get("research_score", 50), 0, 100)))
    combined = int(round(0.5 * es + 0.5 * rs))
    viability = paper.get("viability", "fair")
    mode = paper.get("mode", "engine")
    recommend = bool(paper.get("recommend", True))
    bar = CONFIRM_THRESHOLD - (_HELD_HYSTERESIS if held else 0)

    reason = ""
    confirmed = True
    if combined < bar:
        confirmed, reason = False, f"combined conviction {combined} < {bar}"
    elif viability == "avoid":
        confirmed, reason = False, "research viability = avoid"
    elif mode == "llm" and not recommend:
        confirmed, reason = False, "research desk does not recommend at this price"

    size_mult = round(_clamp(0.5 + (combined - CONFIRM_THRESHOLD) / 40.0, 0.5, 1.3), 3) if confirmed else 0.0
    return {"engine_score": es, "research_score": rs, "combined": combined,
            "confirmed": confirmed, "size_mult": size_mult, "viability": viability,
            "recommend": recommend, "reason": reason or "confirmed"}


def _attach_gate(paper: dict, confluence: float) -> dict:
    """Stamp the combined-gate result onto the paper so a standalone (reviewed-but-not-traded)
    paper is self-describing — the Research page can show the Conviction Index even when the
    name isn't in the live book. `confluence` is the engine read at review time."""
    g = score_breakdown(confluence, paper)
    paper["confluence"] = round(confluence, 3)
    paper["engine_score"] = g["engine_score"]
    paper["combined"] = g["combined"]
    paper["confirmed"] = g["confirmed"]
    paper["gate_reason"] = g["reason"]
    return paper


# ---------------------------------------------------------------------------
# deterministic (engine-only) report + score
# ---------------------------------------------------------------------------

def _dir_of(rows: list[dict], lens: str) -> str | None:
    for r in rows:
        if r.get("lens") == lens:
            return r.get("direction")
    return None


def _val_of(rows: list[dict], lens: str) -> dict:
    for r in rows:
        if r.get("lens") == lens:
            return r.get("value") or {}
    return {}


def _note_of(rows: list[dict], lens: str) -> str:
    for r in rows:
        if r.get("lens") == lens:
            return r.get("note") or ""
    return ""


def _deterministic_score(rows: list[dict], confluence: float) -> int:
    """A research score grounded in the fundamental/risk lenses (not just confluence).

    Anchored on confluence, then nudged by valuation, growth, quality, asymmetry, risk and
    13F flow so the paper's score reflects the COMPANY, not only the signal stack."""
    score = 50 + (confluence or 0.0) * 40.0          # confluence anchor
    deltas = {
        ("valuation", "bull"): 6, ("valuation", "bear"): -9,
        ("growth", "bull"): 5, ("growth", "bear"): -6,
        ("quality", "bull"): 3, ("quality", "bear"): -10,
        ("asymmetry", "bull"): 5, ("asymmetry", "bear"): -5,
        ("risk_drawdown", "bear"): -5,
        ("extension", "bear"): -4,
        ("flows_13f", "bull"): 3, ("flows_13f", "bear"): -3,
        ("solvency", "bear"): -15,
        # leadership / price / theme — the Brain mirrors the engine's confirmation discipline so a
        # cheap name in a lagging sector or a downtrend can't read 'compelling' (the bank-cohort fix)
        ("sector_rs", "bull"): 6, ("sector_rs", "bear"): -16,
        ("trend", "bull"): 4, ("trend", "bear"): -12,
        ("narrative", "bull"): 4, ("narrative", "bear"): -8,
    }
    for (lens, want), d in deltas.items():
        if _dir_of(rows, lens) == want:
            score += d
    return int(round(_clamp(score, 0, 100)))


def _viability(score: int, rows: list[dict]) -> str:
    val_dir = _dir_of(rows, "valuation")
    ext_dir = _dir_of(rows, "extension")
    sector_lag = _dir_of(rows, "sector_rs") == "bear"
    downtrend = _dir_of(rows, "trend") == "bear"
    # a fundamentally-fine name fighting a lagging sector or in a downtrend is never 'compelling'
    if score < 45 or downtrend:
        return "avoid" if (score < 45) else "rich"
    if (val_dir == "bear" or ext_dir == "bear" or sector_lag) and score < 70:
        return "rich"
    if score >= 72 and not sector_lag:
        return "compelling"
    return "fair"


def _compact_value(value: dict) -> str:
    """A short 'k=v' digest of the 1-2 most informative numeric/string fields in a lens value."""
    if not isinstance(value, dict):
        return ""
    parts: list[str] = []
    for k, v in value.items():
        if v is None or isinstance(v, (list, dict)) or k in ("basis", "scored"):
            continue
        if isinstance(v, float):
            v = round(v, 2)
        parts.append(f"{k}={v}")
        if len(parts) >= 2:
            break
    return ", ".join(parts)


def _bullets_from_rows(rows: list[dict], direction: str) -> list[str]:
    out: list[str] = []
    for r in rows:
        if r.get("direction") != direction:
            continue
        label = r.get("lens", "").replace("_", " ")
        note = r.get("note") or ""
        detail = note or _compact_value(r.get("value") or {})
        out.append(f"{label} — {detail}" if detail else label)
    return out[:8]


def _deterministic(ticker: str, *, asof: str, rows: list[dict], confluence: float,
                   price: float | None, vetoes: list[str]) -> dict:
    score = _deterministic_score(rows, confluence)
    viability = _viability(score, rows)
    pros = _bullets_from_rows(rows, "bull")
    cons = _bullets_from_rows(rows, "bear")

    val = _val_of(rows, "valuation")
    growth = _val_of(rows, "growth")
    qual = _val_of(rows, "quality")
    solv = _val_of(rows, "solvency")
    asym = _val_of(rows, "asymmetry")
    ext = _val_of(rows, "extension")
    conv = _val_of(rows, "conviction")

    def _f(x, suffix=""):
        if x is None:
            return "n/a"
        if isinstance(x, float):
            x = round(x, 2)
        return f"{x}{suffix}"

    valuation = (
        f"value_z {_f(val.get('value_z'))}; trailing P/E percentile {_f(val.get('cheap_pctile'))}; "
        f"forward P/E {_f(val.get('forward_pe'))} ({val.get('basis', 'n/a')} basis). "
        + ("Cheap vs its own history." if _dir_of(rows, 'valuation') == 'bull'
           else "Richly valued vs its own history — price discipline matters here."
           if _dir_of(rows, 'valuation') == 'bear' else "Valuation roughly mid-range.")
    )
    fundamentals = (
        f"Quality z {_f(qual.get('quality_z'))} (accounting flag: {qual.get('accounting') or 'none'}); "
        f"Altman zone {_f(solv.get('altman_zone'))}, Piotroski {_f(solv.get('piotroski'))}; "
        f"revenue CAGR {_f(growth.get('rev_cagr'), '%')}, EPS CAGR {_f(growth.get('eps_cagr'), '%')}. "
        f"Engine conviction band: {conv.get('band') or 'n/a'} (score {_f(conv.get('score'))})."
    )
    revenue_streams = (
        "Revenue-segment detail is not in the engine feed — the armed research report fills "
        "the per-segment / geographic mix. The growth figures above are the multi-year "
        "top-line and EPS CAGR the engine does carry."
    )
    rc = growth.get("rev_cagr")
    ec = growth.get("eps_cagr")
    fwd = val.get("forward_pe")
    fe_bits = []
    if ec is not None:
        fe_bits.append(f"trailing EPS CAGR of {ec}% implies forward EPS growth in that vicinity absent a regime change")
    if fwd is not None and ec is not None:
        peg = round(fwd / ec, 2) if ec else None
        if peg is not None:
            fe_bits.append(f"a forward P/E of {fwd}x against ~{ec}% growth is a PEG of ~{peg}")
    forward_earnings = (
        "Recalculated forward view (engine inputs only): " + "; ".join(fe_bits) + "."
        if fe_bits else
        "Forward-earnings recalculation needs the armed report — the engine feed lacks "
        "consensus estimates for this name."
    )
    other = (
        f"Asymmetry (upside/downside) {_f(asym.get('upside_downside'))}; "
        f"extension grade {ext.get('grade') or 'n/a'}"
        + (", parabolic" if ext.get('parabolic') else "")
        + f" ({_f(ext.get('pct_vs_200dma'), '%')} vs 200dma). "
        + (f"Hard vetoes present: {', '.join(vetoes)}." if vetoes else "No hard engine vetoes.")
    )

    # ---- deeper, grounded-where-possible sections (the armed report supersedes these) ----
    narrative = _val_of(rows, "narrative")
    options = _val_of(rows, "options")
    fed = _val_of(rows, "fed_path")
    risk = _val_of(rows, "risk_drawdown")
    basket = narrative.get("basket")
    theme_str = f"the '{basket}' theme basket" if basket else "no mapped engine theme"

    competitive_landscape = (
        f"Peer set and moat durability need the armed report. The engine places {ticker} in "
        f"{theme_str}; within-theme relative-strength leadership is the proxy it tracks for "
        f"competitive position. Engine conviction band: {conv.get('band') or 'n/a'}."
    )

    potential_catalysts = []
    if fwd is not None:
        potential_catalysts.append(
            f"Multiple re-rating: at {fwd}x forward P/E, a shift in the multiple (up or down) is "
            f"itself a re-rating lever independent of earnings.")
    cuts = fed.get("implied_cuts_12m")
    if cuts:
        direction = "tailwind" if cuts > 0 else "headwind"
        potential_catalysts.append(
            f"Fed path: {cuts} implied cuts/12m is a potential multiple {direction} for the cohort.")
    potential_catalysts.append(
        "Speculative re-rating paths (M&A, new product cycles, regulatory shifts) need the armed "
        "report — the engine feed does not carry them.")

    mfe = asym.get("mfe_med")
    dda = asym.get("dd_avg")
    ddt = risk.get("dd_tail")
    scenarios = (
        f"Base case: the multi-sided read holds and {ticker} tracks {theme_str} (engine confluence "
        f"{confluence:+.2f}). "
        f"Bull case: roughly the median favorable excursion of {_f(mfe, '%')} if leadership and flow "
        f"persist. "
        f"Bear case: roughly the average drawdown of {_f(dda, '%')} (tail {_f(ddt, '%')}) if the "
        f"thesis breaks. Calibrated probabilities and price targets need the armed report."
    )

    second_third_order = [
        f"First-order: {ticker} directly.",
        f"Second-order: leadership rotation within {theme_str} — the engine's proxy for the cohort "
        f"the move radiates through.",
        "Third-order: supply-chain winners/losers, competitive set, and credit/refi ripples are NOT "
        "mapped by the engine — the armed report traces them.",
    ]

    try:
        from portfolio import lenses as _lens
        _divs = _lens._divergences(rows)
    except Exception:
        _divs = []
    if _divs:
        variant_perception = ("The engine flags divergence(s): "
                              + "; ".join(d.get("read", d.get("pattern", "")) for d in _divs)
                              + ". Whether this is the edge or the trap is the question the armed report rules on.")
    else:
        variant_perception = (
            f"No engine divergence flagged — the multi-sided lenses are broadly aligned at confluence "
            f"{confluence:+.2f}, so there is no obvious mispricing to exploit from the engine read alone. "
            f"A genuine variant view needs the armed report.")

    what_to_watch = [
        "Relative performance vs SPY over the next ~21 sessions — the engine falsifier fires if it "
        "lags SPY by >=5%.",
    ]
    pv2 = ext.get("pct_vs_200dma")
    if pv2 is not None:
        what_to_watch.append(f"Price vs 200-day MA (currently {_f(pv2, '%')}) — a move toward the "
                             f"200dma changes the entry math.")
    if options.get("call_wall") or options.get("put_wall"):
        what_to_watch.append(f"Dealer walls — call wall {_f(options.get('call_wall'))}, put wall "
                            f"{_f(options.get('put_wall'))}.")
    what_to_watch.append("Dated catalysts (earnings, guidance, policy, data prints) come from the armed report.")

    px_str = f"${price:.2f}" if price else "the current price"
    if viability == "compelling":
        price_assessment = f"At {px_str} the multi-sided read is favorable and the entry is reasonable."
    elif viability == "rich":
        price_assessment = f"At {px_str} the name looks richly valued / extended — wait for a better entry."
    elif viability == "avoid":
        price_assessment = f"At {px_str} the balance of evidence does not support a new position."
    else:
        price_assessment = f"At {px_str} the read is balanced — a measured, confluence-weighted entry only."

    thesis = (
        f"{ticker}: {len(pros)} of the scored lenses are constructive vs {len(cons)} cautionary "
        f"(engine confluence {confluence:+.2f}). {price_assessment}"
    )
    redigest = (
        f"Re-reading the above, the company scores {score}/100 on the research desk and reads "
        f"'{viability}' at {px_str}. "
        + ("The fundamentals and risk picture support a position, sized to the combined conviction."
           if viability in ("compelling", "fair")
           else "The price/quality balance argues for patience over a new position right now.")
        + " (engine-only paper — no web/filings; the armed report supersedes this when available.)"
    )

    sections = {
        "thesis": thesis, "pros": pros, "cons": cons, "valuation": valuation,
        "fundamentals": fundamentals, "revenue_streams": revenue_streams,
        "competitive_landscape": competitive_landscape,
        "confirmed_catalysts": ["Engine feed carries no explicit catalyst calendar — see the armed report."],
        "pending_catalysts": ["Pending catalysts require the armed report (earnings dates, policy, filings)."],
        "potential_catalysts": potential_catalysts,
        "forward_earnings": forward_earnings,
        "scenarios": scenarios,
        "second_third_order": second_third_order,
        "variant_perception": variant_perception,
        "what_to_watch": what_to_watch,
        "other_factors": other, "redigest": redigest,
    }
    recommend = viability in ("compelling", "fair")
    summary = (f"{ticker} — research desk {score}/100, '{viability}'. " + price_assessment)
    paper = {
        "schema": SCHEMA, "id": f"{asof}-{ticker.upper()}", "ticker": ticker.upper(),
        "asof": asof, "generated_at": _now(), "mode": "engine", "model": None,
        "price_at_review": price, "research_score": score, "viability": viability,
        "recommend": recommend, "confidence": "low", "fair_value": None,
        "price_assessment": price_assessment, "summary": summary,
        "sections": sections, "key_risks": cons[:4],
    }
    paper["report_md"] = render_markdown(paper)
    return _attach_gate(paper, confluence)


# ---------------------------------------------------------------------------
# armed (LLM) report + re-digest
# ---------------------------------------------------------------------------

RESEARCH_PROMPT = """You are the Research Desk of the Mastermind — an autonomous, paper-only,
narrative investing bot. The engine's signals already want to BUY {ticker}; your job is to
write the FULL holistic research report a responsible analyst would write BEFORE that buy is
allowed to execute. Today is {asof}; the macro regime is {quad} ({quad_name}). {price_line}

Use your tools first: get_decision_matrix("{ticker}") (address every lens), get_divergences,
get_altdata + get_news (context only), get_quote for the live price. THEN use WebSearch /
WebFetch aggressively for the most recent earnings, guidance, filings, analyst notes,
competitive and macro context.

This is a DEEP-REASONING assignment, not a summary. Go and figure things out: trace causal
chains, follow the money through the supply chain, stress-test the consensus, and reason
several steps ahead. Where you infer rather than observe, tag it (unverified) — but do not
hold back on the inference; that is the value you add over the engine.

Write the report in markdown using EXACTLY these headings (the app parses them):

## Thesis
2-4 sentences: the core investment claim on {ticker}, why now, and the expected outcome.

## Pros
A bulleted list — the strongest reasons to own it (each a concrete, specific point).

## Cons
A bulleted list — the strongest reasons NOT to, including what the bulls are ignoring.

## Valuation
Multiples (P/E, EV/EBITDA, P/S as relevant), vs history and vs peers, and a fair-value view.

## Fundamentals
Balance sheet, margins, returns on capital, cash generation, debt — the financial health.

## Revenue streams
The actual business: segments, geographic mix, customer concentration, what drives the top line.

## Competitive landscape & moat
Who competes, where {ticker} wins or is vulnerable, and how durable the advantage is. Name the
binding constraint / scarce input the franchise controls (or doesn't).

## Confirmed catalysts
A bulleted list of catalysts that have ALREADY happened or are scheduled/known (with dates).

## Pending catalysts
A bulleted list of known-but-unresolved catalysts and their rough timing/probability.

## Potential catalysts
A bulleted list of SPECULATIVE, not-yet-on-the-radar catalysts you reason could re-rate the
stock up OR down (M&A, product cycles, regulatory shifts, multiple re-rating) — with a rough
probability and the directional impact for each.

## Forward earnings (recalculated)
YOUR re-derived forward estimate — next-year revenue & EPS, the assumptions behind it, and how
it compares to consensus. Show the reasoning, not just a number.

## Bull / base / bear scenarios
Three scenarios with a rough probability and an implied price/return for each, and the single
variable that decides which one plays out.

## Second- and third-order effects
Reason past the obvious. At least 4 specific knock-on points: supply-chain winners/losers,
competitors helped or hurt, customers, credit/refinancing, labor and regulatory ripples — and
who is the non-obvious beneficiary or victim two steps removed.

## Variant perception — what the market is missing
The EDGE: where your analysis differs from consensus and why the disagreement exists (and is it
the edge or the trap). If you have no genuine variant view, say so plainly.

## What to watch
At least 4 concrete, dated/threshold monitoring triggers — earnings dates, data prints, price/
volume levels, guidance bars — that would confirm or break the thesis.

## Other factors
Anything else material: management & capital allocation, sentiment/positioning, governance, ESG/
regulatory, options structure.

Discipline: CONFIRMATION over prediction; a story with no price/flow is a value trap. Tag any
inferred input as (unverified). Be blunt; honesty, not alpha. This is paper-only and nothing
you write executes a trade — the engine gates the size and the falsifier. Output ONLY the
markdown report as your final message."""

REDIGEST_PROMPT = """You are the Research Desk re-digesting your OWN research report on {ticker}
to decide whether to confirm the investment AT THIS PRICE ({price_line}). Engine confluence
(the models/signals buy-score) is {confluence:+.2f}.

Re-read the report below, then return ONLY a single fenced ```json block — no prose outside it —
with exactly these fields:
{{
  "research_score": <integer 0-100, your conviction that this is a sound investment AT THIS PRICE>,
  "viability": "compelling" | "fair" | "rich" | "avoid",
  "recommend": true | false,
  "confidence": "low" | "medium" | "high",
  "fair_value": <number or null, your fair-value estimate per share>,
  "price_assessment": "<one sentence: is the current price attractive, fair, or rich, and why>",
  "key_risks": ["<the 2-4 risks most likely to break the thesis>"],
  "summary": "<one to two sentence verdict>"
}}

Scoring guide: 'compelling' (75+) = sound business at an attractive/fair price; 'fair' (60-74)
= ownable, measured entry; 'rich' (45-59) = good company but priced for perfection, wait;
'avoid' (<45) = do not initiate here. Be honest — a great company at a bad price is not a buy.

=== RESEARCH REPORT ===
{report}
=== END REPORT ==="""


def _parse_verdict(text: str) -> dict | None:
    """Extract the re-digest verdict JSON. Tolerant: prefers a ```json fence, then any {...}."""
    if not text:
        return None
    candidates: list[str] = []
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        candidates.append(m.group(1))
    # fall back to the largest brace-balanced span
    if not candidates:
        start = text.find("{")
        end = text.rfind("}")
        if 0 <= start < end:
            candidates.append(text[start:end + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict) and "research_score" in obj:
                return obj
        except Exception:
            continue
    return None


def _strip_leading_narration(md: str) -> str:
    """Drop any leading agent narration that leaked in before the report's first markdown
    heading (e.g. "I have what I need. Writing the report.").

    The armed report is prompted to begin at "## Thesis", but the agent's chatter sometimes
    bleeds into the saved text and then renders verbatim in the dashboard's thesis modal (in
    both English and the Chinese translation). Conservative: only removes whole lines BEFORE
    the first ATX heading (#/##/###...), and only when such a heading exists — if the text has
    no heading at all, it is returned untouched so real content is never dropped."""
    if not md:
        return md
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^\s{0,3}#{1,6}\s+\S", line):
            return "\n".join(lines[i:]).strip()
    return md


def _split_sections(md: str) -> dict[str, Any]:
    """Parse a markdown report (## headings) into the section dict, matching SECTION_ORDER
    headings case-insensitively. List sections become lists of bullet strings."""
    heading_map = {}
    for key, disp in SECTION_ORDER:
        heading_map[disp.lower()] = key
    # resilient aliases — match common heading variants Claude may emit (checked AFTER the
    # exact SECTION_ORDER headings, so exact wins; ordered specific-before-generic)
    heading_map.update({
        "forward earnings": "forward_earnings",
        "revenue": "revenue_streams",
        "competitive": "competitive_landscape",
        "moat": "competitive_landscape",
        "confirmed catalyst": "confirmed_catalysts",
        "pending catalyst": "pending_catalysts",
        "potential catalyst": "potential_catalysts",
        "future catalyst": "potential_catalysts",
        "scenario": "scenarios",
        "second": "second_third_order",
        "2nd": "second_third_order",
        "third-order": "second_third_order",
        "knock-on": "second_third_order",
        "ripple": "second_third_order",
        "variant": "variant_perception",
        "what the market": "variant_perception",
        "what to watch": "what_to_watch",
        "monitoring": "what_to_watch",
        "catalysts": "confirmed_catalysts",
    })
    sections: dict[str, Any] = {}
    cur_key = None
    buf: list[str] = []

    def _flush():
        if cur_key is None:
            return
        body = "\n".join(buf).strip()
        if cur_key in _LIST_SECTIONS:
            items = [re.sub(r"^[-*\d.)\s]+", "", ln).strip()
                     for ln in body.splitlines() if ln.strip()]
            sections[cur_key] = [i for i in items if i]
        else:
            sections[cur_key] = body

    for line in md.splitlines():
        m = re.match(r"^#{1,3}\s+(.*)$", line.strip())
        if m:
            _flush()
            head = re.sub(r"[*_`]", "", m.group(1)).strip().lower()
            cur_key = None
            for h, k in heading_map.items():
                if head.startswith(h):
                    cur_key = k
                    break
            buf = []
        else:
            buf.append(line)
    _flush()
    return sections


def generate(ticker: str, *, asof: str, confluence: float, rows: list[dict],
             vetoes: list[str], price: float | None, regime: dict | None = None,
             armed: bool | None = None) -> dict:
    """Produce the holistic research paper for one name.

    armed=None -> auto (use Claude if available). armed=False -> force the deterministic path.
    Always returns a valid research_paper.v1 dict (falls back gracefully on any LLM failure).
    """
    ticker = ticker.upper()
    use_llm = llm_enabled() if armed is None else bool(armed)
    if not use_llm:
        return _deterministic(ticker, asof=asof, rows=rows, confluence=confluence,
                              price=price, vetoes=vetoes)

    regime = regime or {}
    price_line = f"The live (delayed) price is ${price:.2f}." if price else "Pull the live price with get_quote."
    prompt = (RESEARCH_PROMPT
              .replace("{ticker}", ticker).replace("{asof}", asof)
              .replace("{quad}", str(regime.get("quad", "?")))
              .replace("{quad_name}", str(regime.get("quad_name", "")))
              .replace("{price_line}", price_line))
    try:
        res = cli_bridge.research_sync(prompt, role="deep")
    except Exception as exc:                       # event-loop / SDK failure -> deterministic
        fb = _deterministic(ticker, asof=asof, rows=rows, confluence=confluence, price=price, vetoes=vetoes)
        fb["fallback_error"] = repr(exc)[:200]
        return fb
    report = (res or {}).get("text") or ""
    if not res or not res.get("ok") or len(report) < 200:
        fb = _deterministic(ticker, asof=asof, rows=rows, confluence=confluence, price=price, vetoes=vetoes)
        fb["fallback_error"] = (res or {}).get("error") or "armed report empty"
        return fb

    # strip any leaked agent narration before the first heading so report_md (and the parsed
    # sections / summary below) begin at the real report content, not the agent's chatter.
    report = _strip_leading_narration(report)

    # ---- re-digest pass (un-armed, no tools): the report -> the verdict JSON ----
    redigest_prompt = (REDIGEST_PROMPT
                       .replace("{ticker}", ticker)
                       .replace("{price_line}", price_line)
                       .replace("{confluence:+.2f}", f"{confluence:+.2f}")
                       .replace("{report}", report[:12000]))
    verdict = None
    try:
        rv = cli_bridge.reason_sync(redigest_prompt, role="deep", allowed_tools=[],
                                    max_turns=1, log_run=False)
        verdict = _parse_verdict((rv or {}).get("text") or "")
    except Exception:
        verdict = None
    if verdict is None:
        # last resort: re-digest within the report text, else deterministic verdict
        verdict = _parse_verdict(report) or {}

    sections = _split_sections(report)
    score = int(round(_clamp(verdict.get("research_score",
                _deterministic_score(rows, confluence)), 0, 100)))
    viability = verdict.get("viability")
    if viability not in VIABILITIES:
        viability = _viability(score, rows)
    summary = verdict.get("summary") or (sections.get("thesis") or "")[:240]
    sections["redigest"] = verdict.get("price_assessment") or sections.get("redigest") or summary

    paper = {
        "schema": SCHEMA, "id": f"{asof}-{ticker}", "ticker": ticker, "asof": asof,
        "generated_at": _now(), "mode": "llm", "model": (res or {}).get("model"),
        "price_at_review": price, "research_score": score, "viability": viability,
        "recommend": bool(verdict.get("recommend", viability in ("compelling", "fair"))),
        "confidence": verdict.get("confidence") or "medium",
        "fair_value": verdict.get("fair_value"),
        "price_assessment": verdict.get("price_assessment") or "",
        "summary": summary, "sections": sections,
        "key_risks": verdict.get("key_risks") or [],
        "tools_used": (res or {}).get("tools_used"),
        "cost_usd": (res or {}).get("cost_usd"),
    }
    # keep the model's own markdown as the canonical report body; append the verdict footer
    paper["report_md"] = report.strip() + "\n\n" + _verdict_footer(paper)
    return _attach_gate(paper, confluence)


def _verdict_footer(paper: dict) -> str:
    fv = paper.get("fair_value")
    return (
        f"## Re-digest — viability at this price\n\n"
        f"**Research score:** {paper['research_score']}/100 &nbsp;·&nbsp; "
        f"**Viability:** {paper['viability']} &nbsp;·&nbsp; "
        f"**Recommend:** {'yes' if paper['recommend'] else 'no'} &nbsp;·&nbsp; "
        f"**Confidence:** {paper.get('confidence')}"
        + (f" &nbsp;·&nbsp; **Fair value:** ${fv}" if fv else "") + "\n\n"
        + (paper.get("price_assessment") or "")
    )


# ---------------------------------------------------------------------------
# render + persist
# ---------------------------------------------------------------------------

def render_markdown(paper: dict) -> str:
    """Render the full holistic report as markdown (for the page modal + the feed note)."""
    s = paper.get("sections") or {}
    out: list[str] = [f"# {paper['ticker']} — Research Report",
                      f"*{paper['asof']} · research score {paper['research_score']}/100 · "
                      f"{paper['viability']} · {paper.get('mode')} mode*", ""]
    for key, disp in SECTION_ORDER:
        v = s.get(key)
        if not v:
            continue
        out.append(f"## {disp}")
        if isinstance(v, list):
            out += [f"- {item}" for item in v]
        else:
            out.append(str(v))
        out.append("")
    return "\n".join(out).strip()


def save_paper(paper: dict) -> Path:
    """Persist a paper to data/research/papers/<asof>_<TICKER>.json and append to the index."""
    _PAPERS.mkdir(parents=True, exist_ok=True)
    path = _PAPERS / f"{paper['asof']}_{paper['ticker']}.json"
    path.write_text(json.dumps(paper, indent=2, default=str, ensure_ascii=False))
    with _INDEX.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": paper["id"], "ticker": paper["ticker"], "asof": paper["asof"],
                             "generated_at": paper["generated_at"], "file": path.name}, default=str) + "\n")
    return path


def load_papers() -> list[dict]:
    """All saved papers, newest first, de-duplicated on id (latest generated_at wins)."""
    if not _PAPERS.exists():
        return []
    by_id: dict[str, dict] = {}
    for p in _PAPERS.glob("*.json"):
        try:
            paper = json.loads(p.read_text())
        except Exception:
            continue
        pid = paper.get("id") or p.stem
        cur = by_id.get(pid)
        if cur is None or (paper.get("generated_at") or "") >= (cur.get("generated_at") or ""):
            by_id[pid] = paper
    return sorted(by_id.values(), key=lambda x: x.get("generated_at") or "", reverse=True)


def latest_for(ticker: str) -> dict | None:
    """The most recent saved paper for a ticker, or None."""
    t = ticker.upper()
    cands = [p for p in load_papers() if (p.get("ticker") or "").upper() == t]
    return cands[0] if cands else None


def write_feed_note(paper: dict) -> Path:
    """Mirror the paper into data/research/notes/ so the Research Feed (/api/research) shows it.

    Uses the `# Title` + `*tickers: … · ISO*` header app/web._parse_note expects."""
    _NOTES.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{paper['ticker']}-research".lower())[:48].strip("-")
    path = _NOTES / f"{int(time.time())}_{slug}.md"
    title = f"{paper['ticker']} — Research Report ({paper['viability']}, {paper['research_score']}/100)"
    body = render_markdown(paper)
    # drop the duplicate H1 from the rendered body (the note has its own title line)
    body = re.sub(r"^#\s+.*\n", "", body, count=1).lstrip()
    path.write_text(f"# {title}\n\n*tickers: {paper['ticker']} · {paper['generated_at']}*\n\n{body}\n")
    return path
