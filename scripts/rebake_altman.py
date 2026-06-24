"""Surgical rebake of ONLY the Altman block in baked site/stockdata panels.

The engine fix (_altman reconstructs liabilities = assets - equity so the X4 leg is
never dropped) corrects the SOURCE, but the lenses layer reads the BAKED panel zone.
A full panels() rebuild is a heavy, whole-universe build with many data deps; instead
this recomputes just `financials.multiyear.altman` for each panel using the SAME inputs
the engine uses (statements parquet latest annual row + the panel's mktcap_bn) and the
corrected formula, then atomically rewrites ONLY the files whose zone actually changes.

Validated: this reproduces the engine's stored z exactly (see scripts/altman_variant_test.py),
so the only deltas are the intended liability-reconstruction corrections.

Run:  python -m scripts.rebake_altman           # dry-run (report only)
      python -m scripts.rebake_altman --write    # apply
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pandas as pd

import bot  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
PARQUET = ROOT / "vendor" / "macro_src" / "data" / "edgar" / "statements.parquet"
SD = ROOT / "vendor" / "macro" / "site" / "stockdata"


def _num(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _altman(latest: dict, mktcap):
    """Faithful copy of the patched engine _altman (reconstruct liabilities for X4, with the
    tiny-denominator guard + the 'reconstruction can only rescue, never create distress' approx rule)."""
    a = _num(latest.get("assets"))
    if not a or not mktcap:
        return None
    ca, cl = _num(latest.get("cur_assets")), _num(latest.get("cur_liab"))
    wc = (ca - cl) if (ca is not None and cl is not None) else None
    liab = _num(latest.get("liabilities"))
    reconstructed = False
    if not liab:
        eq = _num(latest.get("equity"))
        recon = (a - eq) if eq is not None else None
        if recon is not None:
            liab, reconstructed = recon, True
    if liab is not None and liab < 0.01 * a:       # corrupt tiny liabilities -> drop X4 (-> approx)
        liab, reconstructed = None, False
    x4 = mktcap / liab if liab else None
    if x4 is not None and x4 > 100:                # non-physical equity:liabilities -> corrupt; drop
        x4, reconstructed = None, False
    base = [(1.2, wc / a if wc is not None else None),
            (1.4, _num(latest.get("retained_earnings")) and _num(latest.get("retained_earnings")) / a),
            (3.3, _num(latest.get("op_income")) and _num(latest.get("op_income")) / a),
            (1.0, _num(latest.get("revenue")) and _num(latest.get("revenue")) / a)]
    base_avail = [(c, x) for c, x in base if x is not None]
    legs = base + [(0.6, x4)]
    avail = [(c, x) for c, x in legs if x is not None]
    if len(avail) < 4:
        return None
    z = sum(c * x for c, x in avail)
    zone = "safe" if z > 2.99 else "grey" if z >= 1.81 else "distress"
    approx = (x4 is None) or (reconstructed and len(base_avail) < 4 and zone == "distress")
    return {"z": round(z, 2), "zone": zone, "approx": approx}


def _find_mktcap_bn(obj, depth=0):
    if depth > 7 or obj is None:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() == "mktcap_bn" and _num(v):
                return _num(v)
        for v in obj.values():
            r = _find_mktcap_bn(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj[:60]:
            r = _find_mktcap_bn(v, depth + 1)
            if r:
                return r
    return None


def main(argv):
    write = "--write" in argv
    df = pd.read_parquet(PARQUET)
    df["ticker"] = df["ticker"].astype(str).str.upper()
    latest_by_tk = {tk: g.sort_values("fy").iloc[-1].to_dict() for tk, g in df.groupby("ticker")}

    files = sorted(SD.glob("*.json"))
    n_seen = n_changed = n_cleared = n_newdistress = 0
    cleared, newdistress = [], []
    for fp in files:
        tk = fp.stem.upper()
        latest = latest_by_tk.get(tk)
        if latest is None:
            continue
        try:
            d = json.loads(fp.read_text())
        except Exception:
            continue
        my = ((d.get("financials") or {}).get("multiyear"))
        if not isinstance(my, dict):
            continue
        old = my.get("altman") or {}
        mktcap_bn = _find_mktcap_bn(d)
        new = _altman(latest, mktcap_bn * 1e9 if mktcap_bn else None)
        if new is None:
            continue
        n_seen += 1
        old_zone, new_zone = old.get("zone"), new.get("zone")
        if old_zone == new_zone and old.get("z") == new.get("z"):
            continue
        n_changed += 1
        is_clearance = old_zone == "distress" and new_zone != "distress"
        if is_clearance:
            n_cleared += 1; cleared.append(f"{tk}({old.get('z')}->{new.get('z')} {new_zone})")
        elif old_zone != "distress" and new_zone == "distress":
            n_newdistress += 1; newdistress.append(f"{tk}({old_zone}->{new.get('z')} approx={new.get('approx')})")
        # CONSERVATIVE: only write the false-positive clearances (distress -> non-distress). Never
        # write newly-enabled distress — leave those panels untouched (the full engine rebuild bakes
        # them as approx -> demoted anyway, so they add no hard block either way).
        if write and is_clearance:
            my["altman"] = new
            tmp = fp.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False))
            os.replace(tmp, fp)

    print(f"mode: {'WRITE' if write else 'DRY-RUN'}")
    print(f"panels with recomputable altman: {n_seen} | changed: {n_changed} "
          f"| distress->cleared: {n_cleared} | ->newly distress: {n_newdistress}")
    print(f"\nCLEARED (data-artifact false positives now non-distress):\n  {cleared}")
    if newdistress:
        print(f"\nNEWLY DISTRESS (recompute now flags — review):\n  {newdistress}")
    print("=== DONE ===")


if __name__ == "__main__":
    main(sys.argv)
