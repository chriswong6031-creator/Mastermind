"""Forward-proof readiness watcher — the system tells YOU when to revisit, instead of tracking dates.

Each forward-proof mechanism (calibration, the universe-wide cross-sectional IC, the shadow books)
is gated on DATA ACCRUAL and crosses its "now statistically honest / now readable" threshold months
out. This module checks those thresholds and records the moment one is NEWLY crossed — exactly once
per crossing — so a persistent alert can surface on the dashboard.

It rides the DAILY BUILD (bot.phase2 calls check_and_record at the end), which is the always-on
heartbeat — no Claude session, no 7-day-expiring cron. Cheap reads only (no panel loads); never raises.

Watched thresholds:
  calibration_forge / calibration_sentinel  — agent left cold-start (n >= MIN_N) → confidence self-corrects
  prediction_xsec                            — >= MIN_DATES independent entry-date clusters → cross-sectional IC honest
  shadow_forward                             — shadow books have their first resolved forward outcomes (risk-tilt A/B readable)
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_STATE = _ROOT / "data" / "shadow" / "readiness_state.json"
_ALERTS = _ROOT / "data" / "shadow" / "readiness_alerts.jsonl"

_LABELS = {
    "calibration_forge": "Calibration (FORGE) left cold-start — its stated confidence now self-corrects "
                         "to realized reliability. Worth a look at the Track Record / calibration readout.",
    "calibration_sentinel": "Calibration (SENTINEL) left cold-start — the adversary's confidence now "
                            "self-corrects to its realized hit-rate.",
    "prediction_xsec": "Cross-sectional edge is now statistically honest — the prediction log reached "
                       "enough independent date-clusters. Go read the Cross-Sectional Edge panel.",
    "shadow_forward": "Shadow books have their first resolved forward outcomes — the risk-tilt A/B vs "
                      "prod (and the policy leaderboard) is now readable.",
}


def _load(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return {}


def status() -> dict:
    """Current readiness flags + the supporting counts. Cheap; never raises."""
    flags, detail = {}, {}
    try:
        cal = _load(_ROOT / "data" / "brain" / "calibration.json")
        ag = cal.get("agents") or {}
        minn = cal.get("min_n", 12)
        for who in ("forge", "sentinel"):
            n = int((ag.get(who) or {}).get("n", 0) or 0)
            flags[f"calibration_{who}"] = n >= minn
            detail[f"calibration_{who}"] = {"n": n, "need": minn}
    except Exception:  # noqa: BLE001
        pass
    try:
        from portfolio import predictions as P
        led = P._load_ledger()
        res_dates = sorted({r["asof"] for r in led if r.get("status") == "resolved" and r.get("asof")})
        indep = len(P._thin_independent([(d, 1) for d in res_dates]))
        flags["prediction_xsec"] = indep >= P._MIN_DATES
        detail["prediction_xsec"] = {"independent_clusters": indep, "need": P._MIN_DATES}
    except Exception:  # noqa: BLE001
        flags.setdefault("prediction_xsec", False)
    try:
        lb = _load(_ROOT / "data" / "shadow" / "leaderboard.json")
        books = lb.get("books") or {}
        n_res = max((int((b or {}).get("n_resolved", 0) or 0) for b in books.values()), default=0)
        flags["shadow_forward"] = n_res >= 5
        detail["shadow_forward"] = {"max_resolved": n_res, "need": 5}
    except Exception:  # noqa: BLE001
        flags.setdefault("shadow_forward", False)
    return {"flags": flags, "detail": detail}


def check_and_record(asof: str | None = None) -> dict:
    """Detect thresholds that JUST crossed (not previously recorded), append an alert per new crossing
    to readiness_alerts.jsonl, and update the once-per-crossing state. Returns {new, flags}. Best-effort."""
    asof = str(asof or date.today().isoformat())[:10]
    st = status()
    flags = st["flags"]
    recorded = _load(_STATE)            # {flag: True once alerted}
    new = [k for k, v in flags.items() if v and not recorded.get(k)]
    try:
        if new:
            _ALERTS.parent.mkdir(parents=True, exist_ok=True)
            with _ALERTS.open("a") as fh:
                for k in new:
                    fh.write(json.dumps({"date": asof, "flag": k,
                                         "message": _LABELS.get(k, k)}) + "\n")
            for k in new:
                recorded[k] = True
            _STATE.write_text(json.dumps(recorded, indent=2))
    except Exception:  # noqa: BLE001
        pass
    return {"new": new, "flags": flags}


def alerts(limit: int = 20) -> list:
    """Recorded readiness alerts (most recent last). For the dashboard banner."""
    try:
        rows = [json.loads(l) for l in _ALERTS.read_text().splitlines() if l.strip()]
        return rows[-limit:]
    except Exception:  # noqa: BLE001
        return []
