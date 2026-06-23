"""Forward shadow A/B runner for the desk levers (Arm B of docs/design/desk/AB_EXPERIMENT.md).

Re-derives the desk-lever books (L1 no-ETF, L2 concentration, L3 timing-gate, L4 autonomous-clone,
and the L1+L2+L3 desk_proxy) on EVERY stored decision-inputs day, marks each book forward, grades the
matured theses vs SPY (leakage-free, via brain.outcomes.label_thesis), and writes:

    data/shadow/desk_ab/leaderboard.json   — machine-readable: per-book + per-day + paired diffs
    data/shadow/desk_ab/report.md          — human-readable summary + the pre-registered power read

It is STANDALONE: it imports the live modules read-only, touches nothing the bot writes, and lands all
output under data/shadow/desk_ab/. No network, no LLM. It runs and exits cleanly even with only a day
or two of inputs — reporting "insufficient data, building" rather than crashing.

    python scripts/run_desk_ab.py                  # all stored days
    python scripts/run_desk_ab.py --asof 2026-06-21  # one day
    python scripts/run_desk_ab.py --no-write         # print only, don't persist
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on sys.path

import bot  # noqa: F401,E402 — bootstraps vendor/macro onto sys.path
from portfolio import desk_ab  # noqa: E402
from portfolio import shadow_books as S  # noqa: E402

_DESK_AB = desk_ab._DESK_AB
_LEADERBOARD = _DESK_AB / "leaderboard.json"
_REPORT = _DESK_AB / "report.md"

# Pre-registered significance floor (AB_EXPERIMENT.md §7 / predictions._MIN_DATES): until this many
# independent resolved-day clusters exist, the verdict is "INSUFFICIENT POWER — keep running".
_MIN_DATES = 8


def _input_days(asof: str | None) -> list:
    """Sorted list of YYYY-MM-DD strings for which a decision-inputs file exists."""
    if asof:
        a = str(asof)[:10]
        return [a] if (S._INPUTS / f"{a}.json").exists() else []
    try:
        return sorted(p.stem for p in S._INPUTS.glob("*.json"))
    except Exception:  # noqa: BLE001
        return []


def _run_all(days: list) -> dict:
    """Run every day in chronological order (books accrue state across days) and return the LAST
    day's full result dict, annotated with the day count + a power read. Best-effort per day."""
    last = {"as_of": None, "books": {}, "leaderboard": []}
    n_ok = 0
    for d in days:
        try:
            last = desk_ab.run(d)
            n_ok += 1
        except Exception as e:  # noqa: BLE001 — a bad day must not sink the run
            print(f"  [warn] day {d} failed: {e}")
    last["n_days"] = n_ok
    return last


def _power_read(result: dict) -> dict:
    """How much forward signal has actually resolved, and whether the pre-registered bar is reachable
    yet. Counts the max resolved-thesis count across books as a crude independent-cluster proxy."""
    books = result.get("books") or {}
    max_resolved = max((b.get("n_resolved") or 0 for b in books.values()), default=0)
    n_days = result.get("n_days") or 0
    enough = max_resolved >= _MIN_DATES
    return {
        "n_days_run": n_days,
        "max_resolved_theses": max_resolved,
        "min_dates_floor": _MIN_DATES,
        "verdict": ("SCORING — power floor reached; read the decision rule"
                    if enough else
                    "INSUFFICIENT POWER — building; keep running (no build decision)"),
    }


def _paired_diffs(result: dict) -> list:
    """Per-book active-return-vs-prod, for the leaderboard's at-a-glance read. The FORMAL paired HAC
    test lives in the decision rule (needs the resolved per-name panel); this is the descriptive
    delta only — clearly labeled NOT significance."""
    books = result.get("books") or {}
    prod = books.get("prod") or {}
    base = prod.get("vs_spy_pct")
    out = []
    for bid, b in books.items():
        v = b.get("vs_spy_pct")
        out.append({
            "id": bid, "label": b.get("label"),
            "vs_spy_pct": v,
            "vs_prod_pct": (round(v - base, 2) if (v is not None and base is not None) else None),
            "return_pct": b.get("return_pct"),
            "n_positions": b.get("n_positions"),
            "n_resolved": b.get("n_resolved"),
            "avg_rel_return": b.get("avg_rel_return"),
            "hit_rate": b.get("hit_rate"),
        })
    return out


def _render_report(result: dict, power: dict, diffs: list) -> str:
    """A human-readable markdown summary. Honest about power: a tie or sub-floor result reads as
    'keep running', never as a build trigger."""
    lines = []
    lines.append("# Desk-lever forward A/B — report\n")
    lines.append(f"_As of {result.get('as_of')} · {result.get('n_days', 0)} day(s) of inputs replayed._\n")
    lines.append("## Power read\n")
    lines.append(f"- Days replayed: **{power['n_days_run']}**")
    lines.append(f"- Max resolved theses (independent-cluster proxy): "
                 f"**{power['max_resolved_theses']}** / floor {power['min_dates_floor']}")
    lines.append(f"- **Verdict: {power['verdict']}**\n")
    lines.append("> Decision rule (AB_EXPERIMENT.md §5) is read ONLY after BOTH arms clear their "
                 "pre-registered bars. Below is descriptive only — paired HAC significance is not "
                 "yet computed. A tie or sub-floor result is 'keep running', never a build trigger.\n")
    lines.append("## Books (descriptive — NOT significance)\n")
    lines.append("| Book | vs SPY % | vs prod % | return % | n_pos | n_resolved | avg rel | hit |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    order = {p["id"]: i for i, p in enumerate(desk_ab.POLICIES)}
    for d in sorted(diffs, key=lambda x: order.get(x["id"], 99)):
        def f(v, suf=""):
            return f"{v}{suf}" if v is not None else "—"
        lines.append(f"| {d['label']} | {f(d['vs_spy_pct'])} | {f(d['vs_prod_pct'])} | "
                     f"{f(d['return_pct'])} | {f(d['n_positions'])} | {f(d['n_resolved'])} | "
                     f"{f(d['avg_rel_return'])} | {f(d['hit_rate'])} |")
    lines.append("")
    lines.append("## Holdings divergence vs prod\n")
    for b in result.get("leaderboard") or []:
        extra = ", ".join(b.get("extra_vs_prod") or []) or "—"
        missing = ", ".join(b.get("missing_vs_prod") or []) or "—"
        lines.append(f"- **{b.get('label')}** — extra: {extra} · missing: {missing}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Forward shadow A/B for the desk levers (leakage-free).")
    ap.add_argument("--asof", default=None, help="Run a single day (YYYY-MM-DD). Default: all stored days.")
    ap.add_argument("--no-write", action="store_true", help="Print only; do not persist artifacts.")
    args = ap.parse_args()

    days = _input_days(args.asof)
    if not days:
        where = args.asof or "data/shadow/inputs/"
        print(f"No decision-inputs found ({where}). Nothing to replay — insufficient data, building.")
        return

    print(f"Replaying {len(days)} day(s): {days[0]} … {days[-1]}")
    result = _run_all(days)
    power = _power_read(result)
    diffs = _paired_diffs(result)
    result["power"] = power
    result["paired_diffs"] = diffs

    if not args.no_write:
        try:
            _DESK_AB.mkdir(parents=True, exist_ok=True)
            _LEADERBOARD.write_text(json.dumps(result, indent=2, default=str))
            _REPORT.write_text(_render_report(result, power, diffs))
            print(f"Wrote {_LEADERBOARD}")
            print(f"Wrote {_REPORT}")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] could not persist artifacts: {e}")

    # console summary
    print(f"\nPower: {power['verdict']}")
    print(f"  days={power['n_days_run']} max_resolved={power['max_resolved_theses']} "
          f"(floor {power['min_dates_floor']})")
    order = {p["id"]: i for i, p in enumerate(desk_ab.POLICIES)}
    print("\nBook                          vs SPY%  vs prod%  ret%   n  resolved")
    for d in sorted(diffs, key=lambda x: order.get(x["id"], 99)):
        def s(v):
            return f"{v:>7}" if v is not None else "      —"
        print(f"  {d['id']:<26} {s(d['vs_spy_pct'])}  {s(d['vs_prod_pct'])}  "
              f"{s(d['return_pct'])} {str(d['n_positions'] or 0):>3} {str(d['n_resolved'] or 0):>3}")


if __name__ == "__main__":
    main()
