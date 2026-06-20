"""Daily Quiver competitor pull — snapshot all 4 AI strategies + show trade changes.

Run:  QUIVER_USER=... QUIVER_PASS=... python -m scripts.quiver_pull
(Store the credentials in the server env / a secret manager — never in code. A Quiver API
key is preferable to the web password where the endpoint exists; rotate the password.)
"""
from __future__ import annotations

import bot  # noqa: F401

from data_layer import quiver


def main() -> int:
    out = quiver.pull_all()
    print("=== Quiver AI strategies (snapshotted) ===")
    for k, d in out.items():
        h = d["holdings"]
        m = d["metrics"]
        print(f"\n{k}  ({d['model']})  sharpe={m.get('Sharpe Ratio')} maxDD={m.get('Max Drawdown')} "
              f"win={m.get('Win Rate')} alpha={m.get('Alpha')}")
        print("  holdings: " + ", ".join(f"{x['ticker']} {x['pct_nav']}%" for x in h))
        diff = quiver.diff_holdings(k)
        if "added" in diff and (diff["added"] or diff["dropped"]):
            print(f"  CHANGES vs last pull: +{diff['added']} -{diff['dropped']} reweight={diff['reweighted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
