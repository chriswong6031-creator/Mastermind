"""Decisive decomposition of altman_distress + side-by-side fix test.

Two failure modes were suspected; this isolates them per name and tests 3 fixes.

For each name we pull the 5 legs from the EDGAR parquet, then compute:
  * engine_repro  — replicate the engine EXACTLY (market X4 = mktcap/liabilities,
                    X4 dropped when the raw `liabilities` tag is missing). Must
                    match the stored panel z → validates the replication.
  * origZ_reconLiab — original Z but with liabilities RECONSTRUCTED as assets-equity
                    so the X4 leg is never silently dropped. Isolates the
                    MISSING-LIABILITIES data artifact.
  * origZ_debtX4  — original Z but X4 = mktcap / total_debt (debt_lt+debt_cur)
                    instead of total liabilities. Isolates the denominator choice.
  * Zpp           — non-manufacturer Z'' = 6.56X1+3.26X2+6.72X3+1.05*(equity/liab),
                    drops X5. zones distress<1.1 / grey / safe>2.6.

A 'flip' = was distress (stored), no longer distress under that variant.
Read-only.  Run:  python -m scripts.altman_variant_test
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PARQUET = ROOT / "vendor" / "macro_src" / "data" / "edgar" / "statements.parquet"
SD = ROOT / "vendor" / "macro" / "site" / "stockdata"

FALSE_POS = ["NEE", "SO", "DUK", "AEP", "AMZN", "T", "TMUS", "DIS", "AMT", "ORCL", "ABBV", "TMO"]
TRUE_CATCH = ["DAL", "UAL", "AAL", "CCL", "GM"]
CONTROLS = ["AAPL", "MSFT"]
TARGETS = FALSE_POS + TRUE_CATCH + CONTROLS


def _f(x):
    try:
        v = float(x)
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _find_mktcap_bn(obj, depth=0):
    if depth > 7 or obj is None:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() == "mktcap_bn" and _f(v):
                return _f(v)
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


def _panel(tk):
    try:
        d = json.loads((SD / f"{tk}.json").read_text())
        a = (((d.get("financials") or {}).get("multiyear") or {}).get("altman")) or {}
        bn = _find_mktcap_bn(d)
        return a.get("z"), a.get("zone"), (bn * 1e9 if bn else None), d.get("sector")
    except Exception:
        return None, None, None, None


def z_orig(z): return None if z is None else ("safe" if z > 2.99 else "grey" if z >= 1.81 else "distress")
def z_zz(z):   return None if z is None else ("safe" if z > 2.6 else "grey" if z >= 1.1 else "distress")


def main():
    df = pd.read_parquet(PARQUET)
    df["ticker"] = df["ticker"].astype(str).str.upper()

    hdr = (f"{'TK':5s} {'sector':12s} {'stЗ':>5} {'repro':>5} | "
           f"{'X1':>6} {'X2':>6} {'X3':>6} {'X4m':>6} {'X5':>6} liab? | "
           f"{'reconZ':>6} {'fl':>3} | {'debtZ':>6} {'fl':>3} | {'Zpp':>6} {'fl':>3}")
    print(hdr)
    out = []
    for tk in TARGETS:
        sub = df[df["ticker"] == tk]
        if sub.empty:
            print(f"{tk:5s} (no parquet rows)"); continue
        row = sub.sort_values("fy").iloc[-1].to_dict()

        a    = _f(row.get("assets"))
        ca   = _f(row.get("cur_assets"));   cl = _f(row.get("cur_liab"))
        re   = _f(row.get("retained_earnings"))
        ebit = _f(row.get("op_income"))
        rev  = _f(row.get("revenue"))
        liab0 = _f(row.get("liabilities"))               # raw tag (may be None)
        eq    = _f(row.get("equity"))
        dlt   = _f(row.get("debt_lt")) or 0.0
        dcur  = _f(row.get("debt_cur")) or 0.0
        debt  = (dlt + dcur) or None

        stored_z, stored_zone, mktcap, sector = _panel(tk)
        liab_recon = liab0 if liab0 else ((a - eq) if (a is not None and eq is not None) else None)

        wc = (ca - cl) if (ca is not None and cl is not None) else None
        X1 = wc / a if (wc is not None and a) else None
        X2 = re / a if (re is not None and a) else None
        X3 = ebit / a if (ebit is not None and a) else None
        X5 = rev / a if (rev is not None and a) else None
        X4m_orig  = mktcap / liab0 if (mktcap and liab0) else None        # engine: drops if liab0 missing
        X4m_recon = mktcap / liab_recon if (mktcap and liab_recon) else None
        X4m_debt  = mktcap / debt if (mktcap and debt) else None
        X4bk      = eq / liab_recon if (eq is not None and liab_recon) else None

        def osum(x4):
            legs = [(1.2, X1), (1.4, X2), (3.3, X3), (0.6, x4), (1.0, X5)]
            av = [(c, x) for c, x in legs if x is not None]
            return sum(c * x for c, x in av) if len(av) >= 4 else None

        repro   = osum(X4m_orig)      # should equal stored_z
        reconZ  = osum(X4m_recon)     # liabilities fixed
        debtZ   = osum(X4m_debt)      # debt denominator
        zlegs = [(6.56, X1), (3.26, X2), (6.72, X3), (1.05, X4bk)]
        zav = [(c, x) for c, x in zlegs if x is not None]
        Zpp = sum(c * x for c, x in zav) if len(zav) >= 3 else None

        was_d = (stored_zone == "distress")
        def flip(zone): return "YES" if (was_d and zone is not None and zone != "distress") else ""
        rz, dz, zz = z_orig(reconZ), z_orig(debtZ), z_zz(Zpp)

        def g(x): return f"{x:6.2f}" if isinstance(x, (int, float)) else "   nan"
        print(f"{tk:5s} {str(sector)[:12]:12s} {g(stored_z)} {g(repro)} | "
              f"{g(X1)} {g(X2)} {g(X3)} {g(X4m_orig)} {g(X5)} {'Y' if liab0 else 'N':>4} | "
              f"{g(reconZ)} {flip(rz):>3} | {g(debtZ)} {flip(dz):>3} | {g(Zpp)} {flip(zz):>3}")
        out.append({"ticker": tk, "sector": sector, "stored_z": stored_z, "stored_zone": stored_zone,
                    "repro": repro, "liab_tag_present": bool(liab0),
                    "X1": X1, "X2": X2, "X3": X3, "X4m_orig": X4m_orig, "X5": X5,
                    "reconZ": reconZ, "recon_zone": rz, "debtZ": debtZ, "debt_zone": dz,
                    "Zpp": Zpp, "zpp_zone": zz})

    (ROOT / "data" / "cache").mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "cache" / "altman_variant_test.json").write_text(json.dumps(out, indent=2, default=str))

    def cleared(group, key):
        tot = sum(1 for r in out if r["ticker"] in group and r["stored_zone"] == "distress")
        clr = sum(1 for r in out if r["ticker"] in group and r["stored_zone"] == "distress"
                  and r[key] is not None and r[key] != "distress")
        return clr, tot

    print("\n=== FIX EFFECTIVENESS (distress -> non-distress) ===")
    for key, label in [("recon_zone", "reconLiab (data fix)"), ("debt_zone", "debtX4"), ("zpp_zone", "Z''")]:
        fp_c, fp_t = cleared(FALSE_POS, key)
        tc_c, tc_t = cleared(TRUE_CATCH, key)
        print(f"  {label:22s}: FALSE-POS cleared {fp_c}/{fp_t} | TRUE-CATCH still caught {tc_t - tc_c}/{tc_t}")
    print("\n  liab_tag_present = N  ->  engine DROPPED the X4 leg (missing-liabilities artifact)")
    print("=== DONE ===")


if __name__ == "__main__":
    main()
