"""Push a SINGLE ticker to the Mastermind brain for review.

The single-name sibling of scripts/demo_research.py: instead of the broad daily
scan, it arms one Claude session focused on ONE name. Claude reads the live
decision matrix (every lens), the divergences, alt-data + news flow, searches the
web for recent catalysts, then writes a falsifiable thesis + a research note back
to the review queue. We then GATE that proposal into the same Brier-scored ledger
the deterministic brain uses — the engine derives the falsifier and the risk
officer clamps a bullish lean on an engine-blocked name to a watch.

Paper-only. Nothing executes. Claude proposes the hypothesis; the engine owns
sizing and the falsifier.

Run:  python -m scripts.review_ticker CSCO
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import bot  # noqa: F401  (bootstraps vendor/macro onto sys.path)

from brain import cli_bridge, research_desk
from portfolio import lenses

_PROPOSALS = Path(__file__).resolve().parent.parent / "data" / "brain" / "proposals.jsonl"

REVIEW_PROMPT = """You are the Mastermind research desk. Review ONE name for the paper book: {ticker}.
Today is {asof}; the macro regime is {quad} ({quad_name}).

APPROACH FROM ALL SIDES. Before any verdict you MUST:
  1. call get_decision_matrix("{ticker}") and address EVERY lens with status in
     {{validated, context, partial}} — valuation, quality, growth, solvency, asymmetry,
     risk (drawdown cone + extension), conviction, narrative/leadership, rate sensitivity,
     macro risk, Fed path, institutional flows (13F + ETF), options, cross-asset — saying
     whether each agrees or disagrees with your read.
  2. call get_divergences("{ticker}") and rule on each: where lenses DISAGREE is the EDGE
     (cheap + flows-in + policy-tailwind before price) or the TRAP (hot + expensive + smart
     money leaving = distribution).
  3. call get_altdata("{ticker}") and get_news("{ticker}") for political-flow + tape context
     (CONTEXT-ONLY — informs narrative, never sizes).
  4. use WebSearch / WebFetch for the most recent earnings, guidance, events, and narrative.

AUTHORITY: validated lenses (drawdown cone, extension veto) hold authority. A hard veto
(parabolic / Altman distress / cycle-blocked / extension) caps size at 0 NO MATTER how bullish
the rest — say so plainly and do not argue past it. Confluence sets size; divergence names the edge.

DISCIPLINE (house doctrine): CONFIRMATION over prediction — a story with no price/breadth/flow
is a value trap, not an edge. Own leaders without CHASING: an extended leader is "time the entry",
not "buy now". Tag inferred inputs as (unverified). Be blunt; no moralizing; honesty, not alpha.

THEN, for {ticker} specifically:
  - call propose_thesis("{ticker}", lean, conviction, horizon_d, thesis, evidence, prob_correct)
    with your honest lean (add / overweight / avoid / underweight / watch). If the engine vetoes
    the name, your lean must respect that (watch / avoid, not add).
  - call save_research_note to summarize the review.

REQUIRED: the save_research_note body MUST be 250-500 words with ALL of these exact markdown
headings so the web app can parse it:

## Thesis
1-3 sentences: the core claim on {ticker}, why NOW, the expected outcome if correct.

## Mechanism
The causal chain — what is operationally/financially happening that makes this true.

## 2nd- and 3rd-order effects
At least 3 specific points: supply-chain, competitors, credit/refi, regulatory ripples.

## Affected tickers
- Primary: {ticker} + close peers.
- Secondary: 1-2 steps removed (suppliers, customers, substitutes).
- Short candidates: names hurt if the thesis is right.

## What to watch
At least 3 concrete, dated/threshold catalysts — earnings, policy, data, price/volume triggers.

## Risk & invalidation
The CONCRETE evidence that proves this wrong (e.g. "guidance cut >10% next quarter"). Vague
invalidators are not acceptable.

## Lens summary
A compact table of the lenses you checked (at minimum validated + context):
| Lens | Direction | Key value | Note |

Nothing you do executes a trade — the engine gates sizing and the falsifier. Propose, don't size."""


def _engine_view(ticker: str) -> dict:
    """The deterministic decision-matrix read (no LLM). This is the engine's review and is
    the source of the risk-officer block that clamps a bullish lean to a watch."""
    m = lenses.full(ticker, "name")
    s = m.get("synthesis", {})
    blocked = bool(s.get("vetoes")) or s.get("size_authority") == "blocked"
    return {"matrix": m, "synthesis": s, "blocked": blocked}


def main(argv: list[str]) -> int:
    ticker = (argv[1] if len(argv) > 1 else "CSCO").upper()
    asof = date.today().isoformat()

    ev = _engine_view(ticker)
    s = ev["synthesis"]
    print(f"=== ENGINE DECISION MATRIX — {ticker} ===")
    print(f"size_authority: {s.get('size_authority')} | confluence: {s.get('confluence')} | "
          f"n_scored: {s.get('n_scored')}")
    print(f"vetoes: {s.get('vetoes')}")
    print(f"divergences: {[d.get('kind') if isinstance(d, dict) else d for d in (s.get('divergences') or [])]}")
    if ev["blocked"]:
        print(f">>> {ticker} is ENGINE-BLOCKED — a bullish Claude lean will be clamped to a watch.")

    print(f"\nclaude CLI: {cli_bridge.cli_path()} | armed-research available: {cli_bridge.available()}")
    prompt = (REVIEW_PROMPT
              .replace("{ticker}", ticker)
              .replace("{asof}", asof)
              .replace("{quad}", str((ev['matrix'].get('synthesis') or {}).get('quad') or _regime_quad()))
              .replace("{quad_name}", _regime_quad_name()))

    out = {"ok": False, "error": "armed session not run"}
    if cli_bridge.available():
        out = cli_bridge.research_sync(prompt, role="deep")
        print("\n=== ARMED REVIEW SESSION ===")
        print("ok:", out.get("ok"), "| model:", out.get("model"), "| tools used:", out.get("tools_used"))
        if out.get("text"):
            print("\nClaude's conclusion:\n", out["text"][:2000])
        if out.get("error"):
            print("\n(armed session error — needs a subscription credential):", out["error"])
    else:
        print("\n(armed research unavailable — SDK/CLI/credential missing; engine review above still stands)")

    # Gate whatever Claude proposed into the falsifiable ledger. The engine-blocked
    # name is passed as `blocked` so a bullish lean is clamped to a watch (de-escalate only).
    ing = research_desk.ingest_proposals(asof, blocked=({ticker} if ev["blocked"] else set()))
    print("\n=== GATED INTO THE LEDGER ===")
    print(json.dumps(ing, indent=2, default=str)[:1800])
    return 0


def _regime_quad() -> str:
    try:
        d = json.loads((Path(cli_bridge._ROOT) / "vendor" / "macro" / "data" / "regime" / "latest.json").read_text())
        return d.get("quad", "?")
    except Exception:
        return "?"


def _regime_quad_name() -> str:
    try:
        d = json.loads((Path(cli_bridge._ROOT) / "vendor" / "macro" / "data" / "regime" / "latest.json").read_text())
        return d.get("quad_name", "")
    except Exception:
        return ""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
