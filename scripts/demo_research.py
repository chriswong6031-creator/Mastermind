"""Live armed-research demo.

Runs one armed Claude session: it reads the dashboard (MCP tools), searches the web/news,
reasons through 2nd/3rd-order effects, and writes proposals back — then we gate those
proposals into the falsifiable ledger.

Requires a SUBSCRIPTION credential reachable by the spawned `claude`:
    local : a normal `claude` login (keychain)
    server: export CLAUDE_CODE_OAUTH_TOKEN="$(claude setup-token)"
Without it, the bridge returns 'Not logged in' and degrades gracefully.

Run:  python -m scripts.demo_research
"""
from __future__ import annotations

import json
from pathlib import Path

import bot  # noqa: F401

from brain import cli_bridge, research_desk

_PROPOSALS = Path(__file__).resolve().parent.parent / "data" / "brain" / "proposals.jsonl"


def main() -> int:
    print(f"claude CLI: {cli_bridge.cli_path()} | armed-research available: {cli_bridge.available()}")
    out = research_desk.run_daily_research()
    print("\n=== ARMED RESEARCH SESSION ===")
    print("ok:", out.get("ok"), "| model:", out.get("model"), "| tools used:", out.get("tools_used"))
    if out.get("text"):
        print("\nClaude's conclusion:\n", out["text"][:1200])
    if out.get("error"):
        print("\n(armed session not run — needs a subscription credential):", out["error"])
        if not _PROPOSALS.exists():
            print("  injecting a sample proposal so the GATING half is still demonstrable...")
            import asyncio
            from brain import bot_mcp
            asyncio.run(bot_mcp.propose_thesis.handler(
                {"subject": "VST", "lean": "add", "conviction": "medium", "horizon_d": 60,
                 "thesis": "AI data-center power demand -> independent power producers (3rd-order)",
                 "evidence": ["grid bottleneck", "(unverified) hyperscaler PPAs"], "prob_correct": 0.62}))

    # whatever Claude proposed -> gate into the falsifiable ledger (engine derives the falsifier)
    ing = research_desk.ingest_proposals(blocked={"SMCI"})   # example: engine-blocked name would be clamped
    print("\n=== GATED INTO THE LEDGER ===")
    print(json.dumps(ing, indent=2, default=str)[:1500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
