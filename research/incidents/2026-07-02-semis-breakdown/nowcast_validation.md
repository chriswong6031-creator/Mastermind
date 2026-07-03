# Price-action regime NOWCAST — pre-registered walk-forward validation (W-I task 3b)

**Module:** `brain/regime_nowcast.py` · **Verdict: GATE FAILED → ships ADVISORY-ONLY**
(`BUDGET_INPUT_QUALIFIED = False`). The nowcast captures the 2026-07 incident but has **no
generalizable forward edge**; it may inform a lens row and the DEF_SLEEVE-unthrottle input, it may
**not** wire into `budget()`.

This result is expected, not a surprise. Two prior negative results constrain this wave — the
cycle-phase veto on held leaders was walk-forward *refuted*, and the repo's own signal-engine EXIT
rule was a *NO-GO*. Pre-registering this gate before writing the promotion note is exactly the
discipline that keeps a third un-validated per-name/posture predictor off the budget path. The gate
was written to fail honestly, and it did.

---

## 1. Pre-registration (fixed BEFORE the run)

**Signal.** `nowcast()` = 2-of-3 `doubt` / 3-of-3 `strong-doubt` over three price-action legs
(defensive-vs-offensive 20d RS diff > 0; SMH 3D-MACD bearish state; %sectors>50d falling over 5d),
applied only under a risk-on / Goldilocks-class label. Shrink-only by construction.

**Data.** `/Users/chriswong/Documents/Cluade/Macro Dashboard/data/yahoo` parquet (adjusted close),
2015-01 … 2026-06-26. Injected as the nowcast's `series_fn`; `asof=d` bars every series at ≤ d so
the forward window (d+1 … d+20 trading days) is strictly out-of-sample. Risk-on label assumed at
every point (the gate tests the price legs in isolation, the regime the module is meant to
second-guess).

**Points.** Every month-end 2015-01…2026-06 (the pre-registered monthly grid) **and** every session
(the doubt-episode grid). Forward horizon: 20 trading days. Offense = SMH; defense = equal-weight
{XLV, XLU, XLP}; spread = offense − defense.

**Gate (both must pass to qualify as a budget input):**
- **G1** — `doubt` episodes show forward-20d **(offense − defense) spread < 0** (mean) with
  **hit-rate ≥ 0.55** (fraction of doubt points where spread < 0).
- **G2** — false-positive cost < ½ the drawdown saved: mean forward offense return on **wrong**
  doubt-days (offense actually rallied, `off_fwd > 0` = missed upside) must be `< 0.5 ×` the mean
  drawdown saved on **right** doubt-days (`off_fwd < 0`).

---

## 2. Results

### 2.1 Forward-20d spread by stance (offense − defense)

**All sessions, 2015-2026:**

| stance | n | mean spread | median | hit (spread<0) | mean off | mean def |
|---|---:|---:|---:|---:|---:|---:|
| confirm | 1748 | **+0.0146** | +0.0136 | 0.392 | +0.0229 | +0.0083 |
| doubt (2/3) | 736 | **+0.0266** | +0.0233 | 0.357 | +0.0330 | +0.0064 |
| strong-doubt (3/3) | 383 | **+0.0228** | +0.0242 | 0.347 | +0.0272 | +0.0043 |
| **doubt-any (≥2/3)** | **1119** | **+0.0253** | +0.0241 | **0.354** | +0.0310 | +0.0057 |

**Month-end grid only (the pre-registered monthly points):**

| stance | n | mean spread | median | hit (spread<0) | mean off | mean def |
|---|---:|---:|---:|---:|---:|---:|
| confirm | 81 | +0.0218 | +0.0216 | 0.370 | +0.0288 | +0.0070 |
| doubt (2/3) | 38 | +0.0134 | +0.0002 | 0.500 | +0.0186 | +0.0052 |
| strong-doubt (3/3) | 17 | +0.0373 | +0.0511 | 0.294 | +0.0457 | +0.0085 |
| doubt-any (≥2/3) | 55 | +0.0208 | +0.0096 | 0.436 | +0.0270 | +0.0062 |

The sign is **backwards** from the thesis: after `doubt` fires, offense *outperforms* defense over
the next 20 days (spread mean **+0.025** all-session / **+0.021** month-end), not underperforms. The
signal fires far too often in healthy uptrends — the persistent SMH 3D-MACD-bear STATE leg and the
choppy breadth leg keep it lit through most of a bull tape — and offense mean-reverts up out of
these pullbacks.

### 2.2 Gate evaluation

- **G1 (doubt-any, all sessions):** mean spread **+0.0253 (want < 0)** → FAIL; hit-rate **0.354
  (want ≥ 0.55)** → FAIL. **G1 FAILS.** (Month-end grid no better: +0.0208 / 0.436.)
- **G2:** right doubt-days (n=390) mean offense −0.0602 (drawdown saved |0.0602|); wrong doubt-days
  (n=729) mean offense **+0.0798** (upside missed). Missed upside +0.0798 is **not** < 0.5 × 0.0602 =
  +0.0301 → **G2 FAILS**. (Wrong doubt-days outnumber right ones ~1.9:1, and the missed rallies are
  larger than the drawdowns dodged — the classic whipsaw cost.)

**OVERALL GATE: FAIL.**

### 2.3 Post-hoc characterisation (does NOT move the pre-registered goalposts)

| subset | fwd-20d SPY mean | P(SPY down) | fwd-20d (def − SPY) mean | hit(def>SPY) |
|---|---:|---:|---:|---:|
| confirm | +0.0113 | 0.309 | −0.0030 | 0.441 |
| doubt-any (≥2/3) | +0.0116 | 0.322 | −0.0059 | 0.416 |
| strong-doubt (3/3) | +0.0088 | **0.373** | −0.0044 | 0.420 |
| ALL (unconditional) | +0.0114 | 0.314 | — | — |

Two honest reads for the record:
1. **Strong-doubt (3/3)** carries a *mild* directional tilt — P(SPY down next 20d) rises to 0.373
   vs 0.314 unconditional. Small, and it is a fraction of the sample (n=383 sessions / 17 month-ends).
   It is *not* enough to earn a budget lever, but it is why the module is retained as an advisory
   lens rather than deleted.
2. **The DEF_SLEEVE-unthrottle thesis also fails to validate on this data**: defensives beat SPY
   *less* often after doubt (41.6%) than after confirm (44.1%), and the def−SPY spread is *more*
   negative on doubt days. So even the "raise the defensive floor when doubt fires" wiring is not
   supported by a forward edge — task 6 should treat the DEF_SLEEVE-unthrottle input as an **honest
   lens signal, not a validated sizing input**, and any actual floor bump it drives must itself be
   graded downstream (W5 benchmark ledger) before it earns standing.

---

## 3. Why it still ships (and how)

The incident is real: on 2026-07-01 all three legs fired (`strong-doubt`, replay-tested), the day
before Autonomous bought back 41 SMH into the breakdown. A shrink-only second opinion that *sees*
that day has value as a **surfaced disagreement** — a lens row that says "the price tape doubts the
Goldilocks label today" — even when it has no forward return edge on average. That is the honest
frame:

- **`BUDGET_INPUT_QUALIFIED = False`** (module constant, asserted in
  `tests/test_regime_nowcast.py::test_budget_input_qualified_is_false_pending_validation`). Task 6 is
  **forbidden** from wiring the nowcast into `budget()`. Flipping the flag requires a *passing*
  re-run of this walk-forward, wired as a paired change — the test is a tripwire against silent arming.
- **Permitted surfaces (advisory-only):** (a) a lens/display row reporting the stance + firing legs;
  (b) a DEF_SLEEVE-**unthrottle** input — but per §2.3 that too lacks a validated forward edge, so it
  is a lens-grade input whose downstream floor effect must be graded by the W5 ledger before it
  earns standing. Neither surface can *add* offense (shrink-only by construction — the module has no
  add-risk verdict).

**Falsifier for any future promotion.** Re-run this exact walk-forward on the then-current data. The
nowcast qualifies as a budget input only if G1 (doubt spread < 0, hit ≥ 0.55) **and** G2
(false-positive cost < ½ drawdown saved) both pass. Until then it is a lens, not a lever.

---

## 4. Reproduce

- Walk-forward: `/tmp/nowcast_wf/wf.py` (raw output `/tmp/nowcast_wf/results.txt`). Injects the
  Macro-Dashboard yahoo parquet as `series_fn`, `asof`-bars every point, measures forward-20d
  offense/defense/SPY conditional on `nowcast()` stance.
- Replay dates (06-24 soft / 07-01 hard / calm 2025-05 clean): `tests/test_regime_nowcast.py`
  (classes 4), reading `tests/fixtures/regime_nowcast/{incident_2026_0607,calm_2025_05}/`.
