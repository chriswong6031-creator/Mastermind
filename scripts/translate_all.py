"""Batch cache-warmer for EN->Simplified-Chinese translations.

Usage:
    cd /Users/chriswong/Documents/Cluade/Mastermind
    ANTHROPIC_API_KEY=<key> python3 scripts/translate_all.py

Loads latest.json + all research notes, calls translate_book() + translate_notes()
to warm the translation cache at data/brain/translations.json.
Requires ANTHROPIC_API_KEY in the environment (or already set in .env).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make sure the project root is on sys.path so imports work
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Load .env if present (optional convenience — do not crash if dotenv missing)
try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass


def main() -> None:
    # Translation routes through the SUBSCRIPTION cli bridge (CLAUDE_CODE_OAUTH_TOKEN),
    # not a metered ANTHROPIC_API_KEY — verify the bridge is reachable instead.
    from brain import cli_bridge
    if not cli_bridge.available():
        print("ERROR: Claude CLI/SDK not available — translation needs the subscription bridge "
              "(CLAUDE_CODE_OAUTH_TOKEN). Check your .env / `claude` install.")
        sys.exit(1)

    from brain.translate import (
        translate_book, translate_notes, translate_papers, _CACHE_PATH, _load_cache,
    )

    # -----------------------------------------------------------------------
    # 1. Load portfolio book
    # -----------------------------------------------------------------------
    portfolio_path = _ROOT / "data" / "portfolio" / "latest.json"
    book: dict = {}
    if portfolio_path.exists():
        try:
            book = json.loads(portfolio_path.read_text(encoding="utf-8"))
            print(f"Loaded portfolio: {len(book.get('positions', []))} positions, "
                  f"{len(book.get('rejected', []))} rejected")
        except Exception as exc:
            print(f"WARN: could not load latest.json: {exc}")
    else:
        print("WARN: data/portfolio/latest.json not found — skipping portfolio translation")

    # -----------------------------------------------------------------------
    # 2. Snapshot pre-run cache size
    # -----------------------------------------------------------------------
    cache_before = len(_load_cache())

    # -----------------------------------------------------------------------
    # 3. Translate portfolio
    # -----------------------------------------------------------------------
    if book:
        print("Translating portfolio (thesis, bear cases, disclaimer)...")
        translate_book(book)

    # -----------------------------------------------------------------------
    # 4. Translate research notes
    # -----------------------------------------------------------------------
    notes_dir = _ROOT / "data" / "research" / "notes"
    n_notes = len(list(notes_dir.glob("*.md"))) if notes_dir.exists() else 0
    if n_notes:
        print(f"Translating {n_notes} research note(s) (title + body)...")
        translate_notes(notes_dir)
    else:
        print("No research notes found — skipping")

    # -----------------------------------------------------------------------
    # 4b. Translate research papers (summary + full report markdown)
    # -----------------------------------------------------------------------
    papers_dir = _ROOT / "data" / "research" / "papers"
    n_papers = len(list(papers_dir.glob("*.json"))) if papers_dir.exists() else 0
    if n_papers:
        print(f"Translating {n_papers} research paper(s) (summary + report)...")
        translate_papers(papers_dir)
    else:
        print("No research papers found — skipping")

    # -----------------------------------------------------------------------
    # 5. Summary
    # -----------------------------------------------------------------------
    cache_after = len(_load_cache())
    new_entries = cache_after - cache_before
    print(f"\nDone. Cache: {cache_before} -> {cache_after} entries (+{new_entries} new).")
    print(f"Cache file: {_CACHE_PATH}")


if __name__ == "__main__":
    main()
