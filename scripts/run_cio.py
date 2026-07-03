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


def run(asof: date | None = None, *, with_agenda: bool = True) -> dict:
    """Build + persist the weekly CIO review, then the improvement agenda (W-L / L3). Returns the CIO
    write() result dict with an added ``agenda`` key. Never raises.

    The agenda fuses every accountability artifact — reusing the CIO review just computed (the
    calibration source) rather than re-running review(): one source of truth (P7). Guarded so an
    agenda miss never fails the CIO review, and vice-versa.

    The weekly SCHEDULER runs the agenda as its own dedicated job (``_improvement_agenda_job``, L6)
    30 min after the CIO job, so ``with_agenda`` defaults on only for the ON-DEMAND / manual runner —
    the scheduler passes ``with_agenda=False`` to avoid a double-write. Either path calls the same
    ``brain.improvement_agenda.write`` (one writer, charter P7)."""
    try:
        res = cio.write(asof)
    except Exception as e:  # noqa: BLE001 — the runner is best-effort; never crash the scheduler
        return {"ok": False, "error": str(e)}
    if not with_agenda:
        return res
    # improvement agenda — advisory-only fusion; degrade-safe, never blocks the CIO review
    try:
        from brain import improvement_agenda
        cio_rep = res.get("review") if isinstance(res, dict) else None
        # build with the injected review (avoids a second review() pass), then persist
        agenda_dict = improvement_agenda.build(asof, cio_rep=cio_rep)
        ag = improvement_agenda.write(asof)
        ag["n_items"] = agenda_dict.get("n_items", ag.get("n_items"))
        res["agenda"] = ag
    except Exception as e:  # noqa: BLE001
        res["agenda"] = {"ok": False, "error": str(e)}
    return res


def main() -> None:
    res = run()
    if not res.get("ok"):
        print("CIO review UNAVAILABLE:", res.get("error") or "(write failed)")
        return
    print("CIO review written ->", res.get("md_path"))
    print(f"  week={res.get('week')} narrated={res.get('narrated')}")
    print(f"  json={res.get('json_path')}")
    ag = res.get("agenda") or {}
    if ag.get("ok"):
        print("Improvement agenda written ->", ag.get("md_path"))
        print(f"  n_items={ag.get('n_items')} json={ag.get('json_path')}")
    else:
        print("Improvement agenda UNAVAILABLE:", ag.get("error") or "(write failed)")


if __name__ == "__main__":
    main()
