"""Empirical decomposition of the cycle_blocked veto.

For every baked panel, determine WHICH of the five lenses triggers raised the
cycle_blocked veto:  conviction.cycle_blocked flag / size.bucket=='avoid' /
axes.entry.blocked / size.pct<=0 / band=='avoid'  (portfolio/lenses.py:820).
Tally trigger frequency + by-sector, and dump the conviction detail for marquee
names (NVDA/AAPL/energy) so we can tell a regime/cycle gate from a per-name one.

Read-only.  Run:  python -m scripts.cycle_blocked_decomp
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SD = ROOT / "vendor" / "macro" / "site" / "stockdata"
REGIME = ROOT / "vendor" / "macro" / "data" / "regime" / "latest.json"

MARQUEE = ["NVDA", "AAPL", "MSFT", "META", "XOM", "CVX", "COP", "OXY", "JPM", "LLY"]


def g(d, path):
    cur = d
    for k in path.split("."):
        cur = cur.get(k) if isinstance(cur, dict) else None
    return cur


def triggers(d):
    """Return the set of cycle_blocked triggers that fire for this panel (mirrors lenses.py:637-643,820)."""
    sz = g(d, "conviction.size.pct")
    t = []
    if g(d, "conviction.cycle_blocked"):
        t.append("cycle_flag")
    if g(d, "conviction.size.bucket") == "avoid":
        t.append("bucket_avoid")
    if g(d, "conviction.axes.entry.blocked"):
        t.append("entry_blocked")
    if sz is not None and sz <= 0:
        t.append("size_0")
    if g(d, "conviction.band") == "avoid":
        t.append("band_avoid")
    return t


def main():
    try:
        reg = json.loads(REGIME.read_text())
        print(f"REGIME: quad={reg.get('quad')} ({reg.get('quad_name')}) "
              f"liquidity={reg.get('liquidity_overlay')} macro_risk={g(reg, 'macro_risk.score')}")
    except Exception as e:
        print(f"REGIME: (unreadable: {e!r})")

    files = sorted(SD.glob("*.json"))
    n = 0
    n_blocked = 0
    trig_count = Counter()
    by_sector = defaultdict(lambda: {"n": 0, "blocked": 0})
    sole = Counter()  # cases where exactly ONE trigger fires
    for fp in files:
        try:
            d = json.loads(fp.read_text())
        except Exception:
            continue
        if "conviction" not in d:
            continue
        n += 1
        sec = str(d.get("sector"))
        by_sector[sec]["n"] += 1
        t = triggers(d)
        if t:
            n_blocked += 1
            by_sector[sec]["blocked"] += 1
            for x in t:
                trig_count[x] += 1
            if len(t) == 1:
                sole[t[0]] += 1

    print(f"\npanels with conviction: {n} | cycle_blocked (any trigger): {n_blocked} ({100*n_blocked/n:.0f}%)")
    print(f"\nTRIGGER FREQUENCY (a name can fire several):")
    for k, v in trig_count.most_common():
        print(f"  {k:14s} {v:4d}  ({100*v/n_blocked:.0f}% of blocked)")
    print(f"\nSOLE TRIGGER (only this one fired — the decisive cause):")
    for k, v in sole.most_common():
        print(f"  {k:14s} {v:4d}")
    print(f"\nBLOCK RATE BY SECTOR:")
    for sec in sorted(by_sector, key=lambda s: -by_sector[s]["blocked"]):
        b = by_sector[sec]
        if b["n"] >= 5:
            print(f"  {sec[:24]:24s} {b['blocked']:3d}/{b['n']:3d}  ({100*b['blocked']/b['n']:.0f}%)")

    print(f"\nMARQUEE NAMES (conviction detail):")
    for tk in MARQUEE:
        try:
            d = json.loads((SD / f"{tk}.json").read_text())
        except Exception:
            print(f"  {tk}: (no panel)"); continue
        print(f"  {tk:5s} sec={str(d.get('sector'))[:14]:14s} band={str(g(d,'conviction.band')):6s} "
              f"size%={g(d,'conviction.size.pct')} bucket={g(d,'conviction.size.bucket')} "
              f"cycle_flag={g(d,'conviction.cycle_blocked')} entry_blocked={g(d,'conviction.axes.entry.blocked')} "
              f"verdict={g(d,'conviction.verdict')} -> triggers={triggers(d)}")
    print("=== DONE ===")


if __name__ == "__main__":
    main()
