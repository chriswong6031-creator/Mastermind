"""Show the LIVE vs STALE-snapshot marks for the US books — proves the live-price fix and quantifies
the drawdown the stale marks were hiding. Read-only; prints a report, writes nothing, trades nothing.

    python scripts/check_live_marks.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402,F401  -> vendor/macro onto sys.path

from portfolio import paper_account  # noqa: E402
from data_layer import yahoo_feed     # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
US_BOOKS = ["flagship", "autonomous", "heavyweight", "etf"]


def _snapshot(t: str):
    try:
        p = _ROOT / "vendor" / "macro" / "site" / "stockdata" / f"{t}.json"
        return float((json.loads(p.read_text()).get("tech") or {}).get("price"))
    except Exception:
        return None


def _stored_nav(pid):
    try:
        rows = [json.loads(l) for l in (paper_account._paths(pid)["nav"]).read_text().splitlines() if l.strip()]
        return rows[-1].get("nav") if rows else None
    except Exception:
        return None


def main():
    print("yfinance importable:", end=" ")
    try:
        import yfinance  # noqa: F401
        print("yes")
    except Exception as e:
        print("NO —", repr(e), "(live leg will degrade to snapshot)")
    for pid in US_BOOKS:
        try:
            st = paper_account._load_account(pid)
        except Exception:
            continue
        pos = st.get("positions") or {}
        us = [t for t in pos if t and "." not in t]
        if not us:
            continue
        yahoo_feed.warm(us)
        cash = float(st.get("cash") or 0.0)
        print(f"\n=== {pid}  cash=${cash:,.0f}  held={len(us)} ===")
        live_mv = 0.0
        snap_mv = 0.0
        print(f"  {'ticker':8s} {'shares':>10s} {'live':>10s} {'snapshot':>10s} {'live vs snap':>13s}")
        for t in sorted(us):
            sh = float((pos[t] or {}).get("shares") or 0.0)
            live = yahoo_feed.price_local(t)
            snap = _snapshot(t)
            live_mv += sh * (live or snap or 0.0)
            snap_mv += sh * (snap or live or 0.0)
            chg = (f"{(live/snap-1)*100:+6.1f}%" if (live and snap) else "   n/a")
            print(f"  {t:8s} {sh:10.2f} {('%.2f'%live) if live else '   --':>10s} "
                  f"{('%.2f'%snap) if snap else '   --':>10s} {chg:>13s}")
        live_nav = cash + live_mv
        snap_nav = cash + snap_mv
        stored = _stored_nav(pid)
        print(f"  NAV  live=${live_nav:,.0f}  snapshot=${snap_nav:,.0f}  stored(nav_history)=${(stored or 0):,.0f}")
        if snap_nav:
            print(f"  → live vs snapshot NAV: {(live_nav/snap_nav-1)*100:+.2f}%  "
                  f"(the drawdown the stale mark was hiding)")


if __name__ == "__main__":
    main()
