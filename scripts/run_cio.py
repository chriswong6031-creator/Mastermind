"""On-demand CIO / Meta-PM weekly review.

Reads the Flagship desk's accountability state — per-role calibration multipliers, each seat's graded
KPIs, book NAV-vs-benchmark, and the shadow leaderboard — and writes a "what is working / who is
miscalibrated" note plus non-binding tuning recommendations to data/brain/cio/<isoweek>.{json,md}.

It RECOMMENDS ONLY: it never trades, never flips a flag, and never changes any seat's behavior. The
numbers + recommendations are computed deterministically; Opus only writes the prose note. Safe to run
in the weekly scheduler or by hand.

    python scripts/run_cio.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root → import bot/brain

import bot  # noqa: F401,E402 — vendor/macro on sys.path before importing brain deps
from brain import cio  # noqa: E402


def run(asof: date | None = None) -> dict:
    """Build + persist the weekly review. Returns the write() result dict. Never raises."""
    try:
        res = cio.write(asof)
    except Exception as e:  # noqa: BLE001 — the runner is best-effort; never crash the scheduler
        return {"ok": False, "error": str(e)}
    return res


def main() -> None:
    res = run()
    if not res.get("ok"):
        print("CIO review UNAVAILABLE:", res.get("error") or "(write failed)")
        return
    print("CIO review written ->", res.get("md_path"))
    print(f"  week={res.get('week')} narrated={res.get('narrated')}")
    print(f"  json={res.get('json_path')}")


if __name__ == "__main__":
    main()
