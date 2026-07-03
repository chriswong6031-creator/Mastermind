# Perception validation — `crash_risk` — FAIL

*Run 2026-07-03T01:24:28.439262+00:00 · harness `scripts/validate_perception.py` (W-E.1 task E1.4) · charter P3 (honest pass/fail)*

> This verdict DECIDES what E2/E3 may arm. The harness WIRES NOTHING — a human (Fable) reads this and makes the arming call.

## Verdict: FAIL (cold-start, UNCOMPUTABLE with vendored history)

**Reason.** the walk-forward inputs do not exist over 2011-2026 with vendored data — the regime file is a single live snapshot, not a historical series

**What would make it computable.** a DAILY historical series of regime.risk_radar.drawdown_prob (h21 + base + lift + dominant_scare), regime.vol_shock, and regime.market_gamma — i.e. the dashboard must vendor a dated risk_radar/forward_log with drawdown_prob, or replay its engine over history (handoff H4). Today only ~4 live forward-log rows exist.

**Precedent (P3).** regime_nowcast (walk-forward gate failed 0.354 → ships ADVISORY-ONLY). CRASH-RISK ships the same way: advisory plane of the view, severity notch DARK.

**Arming decision.** crash_risk ships ADVISORY / cold_start=true — the notch/tilt seam stays DARK until a forward-graded historical series exists to gate it

Per the charter's degrade-never-fabricate rule, the harness does NOT substitute a price-only proxy and call it this alarm — a truthful non-result is the correct output.
