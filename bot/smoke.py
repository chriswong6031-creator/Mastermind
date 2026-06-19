"""Phase 0 smoke test — the smallest proof that the whole premise holds.

It proves the bot can:
  1. import the macro dashboard's engine as a library (via the pinned submodule),
  2. read the canonical published regime (data/regime/latest.json), AND
  3. recompute that regime live from the parquet store (build_features -> classify)
     and get the SAME answer — i.e. "live == backtest by construction".

Run:  python -m bot.smoke
Exit code 0 iff the live recompute matches the published regime.
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

import bot  # noqa: F401  -> triggers the vendor/macro sys.path bootstrap

warnings.filterwarnings("ignore")  # silence pandas fragmentation noise


def main() -> int:
    from lib import config
    from engine import inputs, regime

    root = Path(config.ROOT)
    data_dir = Path(config.data_dir())
    print(f"engine ROOT       : {root}")
    print(f"data dir          : {data_dir}")

    # 1) the canonical published regime (what the static dashboard shows)
    latest = json.loads((data_dir / "regime" / "latest.json").read_text())
    pub_quad = latest["quad"]
    print(
        f"published regime  : {pub_quad} {latest.get('quad_name', '')}  "
        f"g={latest['growth_score']}  i={latest['inflation_score']}  "
        f"liq={latest['liquidity_overlay']}  asof={latest['date']}"
    )

    # 2) recompute it live from the parquet store
    feats = inputs.build_features()
    cls = regime.classify(feats)
    live = cls.iloc[-1]
    live_quad = str(live["quad"])
    live_asof = str(cls.index[-1].date())
    print(
        f"live recompute    : {live_quad}  "
        f"g={round(float(live['growth_score']), 3)}  "
        f"i={round(float(live['inflation_score']), 3)}  asof={live_asof}"
    )

    # 3) the assertion that the whole architecture rests on
    ok = live_quad == pub_quad
    print("-" * 56)
    print(f"live == published : {'PASS' if ok else 'FAIL'}  ({live_quad} vs {pub_quad})")
    if ok:
        print("Phase 0 wiring proven: engine imports + live regime reproduces.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
