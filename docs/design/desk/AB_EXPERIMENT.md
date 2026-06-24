# Pre-Registered A/B Experiment — Do the Desk Levers Add Alpha?

**Version 1.0 · 2026-06-22 · Status: PRE-REGISTERED (locked before any verdict read)**

> This document is a **gate**. It is written and committed **before** the desk's
> expensive LLM seats are built. Its purpose is to decide — empirically, leakage-free,
> and against pre-committed thresholds — whether the desk's *judgment levers* add alpha
> over the existing Flagship gate, and whether that alpha is captured by **cheap
> deterministic rules** (in which case we ship the rules and defer the seats) or requires
> the **holistic LLM read** (in which case the seat build is justified).
>
> Per **Honesty over Alpha** (`00-index.md` Decision #12): the n=1 day-one result and the
> IREN/KMT/FSS anecdotes tune **nothing** here. No threshold below was chosen to make the
> one-day outcome "come out right." Nothing changes desk behavior until it clears the
> pre-registered significance bars (`effective-n ≥ _MIN_DATES=8`, DSR/PBO on the historical
> arm). The diagnostic (GT) is explicit that the day-one "Brain beat Flagship" claim is
> **uncomputable** from stored data; this experiment exists precisely because we *cannot
> score the levers yet* — it instruments the marks and runs the books forward so that we can.

---

## 1. Objective & The Decision This Gates

### 1.1 Objective

Test whether the desk's four judgment **levers**, expressed as **deterministic, leakage-free
proxies** (no LLM), add risk-adjusted active return over the current Flagship gate
(engine confluence + Conviction Index ≥ 60 → auto-buy). The levers:

| Lever | Name | One-line | Historically testable? | Forward shadow? |
|---|---|---|---|---|
| **L1** | No broad ETFs | Zero-weight any sector/index/factor ETF in the alpha sleeve. | No (panel is single-name S&P 1500; GT4) | Yes |
| **L2** | Concentration | Keep only top-N names by Conviction Index; renormalize. | Yes (`frac`; GT4) | Yes |
| **L3** | Entry-timing gate | Withhold names with poor entry technicals at decision time. | Yes (price-only proxy; GT4) | Yes |
| **L4** | Autonomous-clone | Replay the US Brain's actual submitted books. | No (1 day of Brain books; GT4) | Yes (once depth accrues) |

### 1.2 The decision it gates

**Build the LLM desk (the Opus/Sonnet seats in `02-organization-structure.md`) only if the
combined deterministic proxy clears the §5 bar AND a residual gap remains that the cheap
rules do not capture.** Concretely, this experiment forces one of three outcomes:

- **BUILD THE SEATS** — `desk_proxy`/`desk_full` clears the bar **and** beats the best single
  cheap lever and `autonomous_clone`: the holistic LLM read adds alpha beyond rules → the
  seat spend is justified.
- **SHIP THE CHEAP RULES, DEFER THE SEATS** — `no_etf` and/or `concentrated_topN` alone
  capture (most of) the edge and `desk_proxy` adds no significant increment → enforce L1/L2
  as deterministic rules in `shadow_books`/`phase2`, hold the seats.
- **KILL / REDIRECT** — no proxy clears the bar over `prod`, or `autonomous_clone` dominates
  everything (the edge is *holistic name selection*, not any factorable lever) → do not build
  the buy-side seats as specified; redirect effort (e.g. to the sell-side Risk Officer or to
  cloning the Brain's selection rather than the desk's veto stack).

---

## 2. Policies Under Test

Each policy is a **deterministic function over stored decision-inputs** (forward arm:
`data/shadow/inputs/<asof>.json`, per GT2/GT3) or over the **frozen panel** (historical arm:
`_closes_deep/_closes_delisted/sp1500_pit_membership.parquet`, per GT4). All forward policies
delegate sizing to `shadow_books.apply_policy` after a pure pre-filter; none touches prod state.

Implemented in a **new standalone module** `portfolio/desk_ab.py` (never imported by
`bot.phase2`; pattern per GT2 §5) plus `loop/desk_levers.py` for the historical arm.

### 2.1 `prod` — baseline (control)
Exactly the live policy already in `shadow_books.POLICIES`: `committee=True, calibration=True,
sizing="research"` → `weight_prod`. No pre-filter. This reproduces the Flagship auto-buy and is
the control both arms are measured against.

### 2.2 `no_etf` — L1
Pre-filter: drop every candidate where `etf_universe.is_etf(ticker) == True`
(`portfolio/etf_universe.py:115`, O(1) allowlist; GT3 confirms this is the only machine-readable
ETF gate and is leakage-free with zero new data). Then delegate to `apply_policy(prod-sizing,
filtered)` and renormalize gross ≤ 1.0. Scope: alpha/conviction sleeve only (Leadership-sleeve
ETFs are out of the shadow-input universe by construction).

### 2.3 `concentrated_topN` — L2 (**N = 8**)
Pre-filter: among `forge_confirmed==True` records, sort by `combined` (Conviction Index 0–100;
already persisted, GT3) descending, keep the **top 8**, drop the rest. Then delegate to
`apply_policy` and renormalize to sum ≤ 1.0. **N=8 is pre-registered** (not tuned): it matches
the desk's `MAX_NAMES=12` ceiling minus headroom and the US Brain's observed 8-name book (GT1
§4), and sits between the Brain's concentration and Flagship's 19-name tail. Tie-break at the
boundary: higher `engine_score`, then higher `research_score`, then alphabetical (deterministic).

### 2.4 `timing_gated` — L3 (exact entry-technical filter)
Pre-filter: withhold a candidate if **any** of the following fire. Signals are read from
`vendor/macro/site/stockdata/<TICKER>.json` at replay time (GT3 confirms each is AVAILABLE and
leakage-free — computed from prices ≤ the stockdata `asof`). To make the forward arm self-contained,
these fields are also **emitted into the shadow-input record** via `_emit_shadow()` in
`bot/phase2.py` (additive; GT3 §summary):

| Sub-lever | Rule (withhold if) | Source field (AVAILABLE per GT3) |
|---|---|---|
| **Extension** | `conviction.ext.grade ∈ {"stretched","parabolic"}` OR `conviction.ext.parabolic == true` OR `tech.pct_vs_200dma ≥ 30` | `conviction.ext.*`, `tech.pct_vs_200dma` |
| **Entry urgency** | `ladder.action != "ADD"` OR `ladder.entry.urgency == "hold"` OR `ladder.entry.urgency == "avoid"` | `ladder.action`, `ladder.entry.urgency` |
| **Weak RS** | `alpha.rs < 50` (below-median 90-day RS pctile vs SPY) | `alpha.rs` |
| **Entry quality** | `ladder.eq_grade == "weak"` OR `ladder.eq_dir == "down"` | `ladder.eq_score/eq_grade/eq_dir` |

**Distribution sub-lever — DEFERRED.** GT3 marks the "distribution" divergence pattern as
*derived*, not a raw stockdata field (requires running `lenses.full()` at replay). For v1 we
**approximate** distribution by the Weak-RS + Extension rules above and explicitly defer the true
distribution flag to v2 (it requires either pre-computing `lenses.full()` per candidate or
emitting a `distribution_flag` into `_emit_shadow()`). `dist_from_base` / base-quality and
per-name `mom_20d/60d` are **NOT AVAILABLE** (GT3) → not used; no approximation invented.

Surviving names delegate to `apply_policy` and renormalize.

### 2.5 `autonomous_clone` — L4
Replay the US Brain's **actual submitted books**: for each `asof`, set targets to the Brain's
submitted `{ticker: weight}` from its book log (`data/portfolios/autonomous/` — `latest.json`
holdings / a per-day `decisions.jsonl` if present; GT3 §L4). No engine sizing — the clone *is*
the Brain's sizing. **Status: instrumented now, scored later.** Only 1 day of Brain books exists
(GT1/GT4); this policy accrues power forward and is excluded from any verdict until it has
`effective-n ≥ _MIN_DATES`.

### 2.6 `desk_proxy` — L1 + L2 + L3 (the combined cheap desk)
Compose the pre-filters in order: **L1 (drop ETFs) → L3 (drop poor-timing) → L2 (keep top-8 of
survivors by `combined`)** → delegate to `apply_policy` → renormalize. This is the deterministic
stand-in for the full desk's subtract-only funnel (no LLM). The decision rule (§5) is written
primarily around this policy.

---

## 3. Pre-Registered Hypotheses (H1–H5)

All directions and metrics are fixed **before** reading any forward result. Metrics are defined
in §4.3. "Active return" = book return − SPY return over matched 21-bday windows, leakage-free
(`brain/outcomes.py:label_thesis`, `rel_return`).

| # | Hypothesis | Direction | Primary metric | Decision link |
|---|---|---|---|---|
| **H1** | Removing broad ETFs (`no_etf`) does not *reduce* active return vs `prod` (forces single-name selection, cuts ETF-sleeve fill slippage observed in GT1). | `no_etf` active return ≥ `prod`, and `no_etf` info ratio ≥ `prod` | Active return vs SPY; Info Ratio | L1 cheap-rule case |
| **H2** | Concentration to top-8 (`concentrated_topN`) raises active return and info ratio vs `prod` (kills the 19-name <2% tail). | `concentrated_topN` > `prod` on both | Active return; Info Ratio | L2 cheap-rule case |
| **H3** | The entry-timing gate (`timing_gated`) raises **hit-rate** and reduces **max drawdown** vs `prod` (withholds extended / weak-RS / poor-urgency entries). | `timing_gated` hit-rate ≥ `prod`; `timing_gated` max DD ≤ `prod` | Hit-rate (falsifier-consistent); Max Drawdown | L3 case |
| **H4** | The combined cheap desk (`desk_proxy`) beats `prod` **and** the best single lever on info ratio (the levers are complementary, not redundant). | `desk_proxy` IR > max(`prod`, best single lever) IR | Info Ratio (primary); active return | Cheap-rules-suffice test |
| **H5** | The holistic Brain (`autonomous_clone`) beats `desk_proxy` on active return (selection alpha the factorable levers cannot replicate). | `autonomous_clone` active return > `desk_proxy` | Active return vs SPY | **Seat-build justification** |

Falsification: H5 **failing** (clone ≤ desk_proxy) is the strongest signal that the edge is
factorable → defer the seats. H4 **failing** (combo ≯ best single lever) says one cheap rule
already captures it → ship that rule alone.

---

## 4. The Two Arms

### 4.1 Arm A — Historical backtest (frozen gauntlet)

Tests the **historically expressible** levers (**L2, L3** only; L1 and L4 are not panel-testable
per GT4) over the survivorship-safe ~S&P 1500 panel, 2002–2026, through the **frozen judge**
(`loop/harness.py:score` — DSR, purged-5-fold, Newey-West HAC, crisis windows, PBO/CSCV, BH-FDR)
with the **locked 2022+ holdout**.

- **Module:** new `loop/desk_levers.py` subclassing `FactorCandidate`
  (`loop/factor_experiment.py:148`), pattern per `ZooFactor` (GT4). Frozen modules
  (`harness`, `cluster`, `pbo`, `engine.validation`) are **not** touched.
- **Candidates:** `BaselineLever` (control, `frac=0.10`), `ConcentrationLever` (`frac=0.05`
  ≈ top-N, = L2), `EntryTimingLever` (price-only proxy: exclude bottom-quartile
  `high52w_proximity` *extension* and bottom-quartile `consistency`/`mom_12_1` at each rebalance,
  = L3), `ComboLever` (L2+L3).
- **Runner:** `scripts/run_desk_levers.py` (copy of `scripts/run_engine_backtest.py`), writes
  `data/backtest/desk_levers.json`. Runtime ~20–60 s, single-threaded, fully offline (GT4 Q2/Q4).
- **Bars (pre-registered):** a lever passes Arm A only if, vs baseline, it shows **positive DSR**,
  **PBO < 0.5** (CSCV), survives **BH-FDR** at q=0.10 across the candidate set, has **effective-n**
  (re-deflated at cluster count) above the harness floor, **and holds in the 2022+ holdout**
  (one-shot, no peeking).

### 4.2 Arm B — Forward shadow A/B

Runs **all six policies** (L1, L2, L3, L4, desk_proxy, prod) forward on prod's stored
decision-inputs via `portfolio/shadow_books.run` + the standalone `portfolio/desk_ab.py`
consumer (GT2). Each book is isolated under its own directory, marked daily vs SPY, and every
newly-bought name opens a falsifiable thesis graded leakage-free
(`shadow_books._update_theses` → `brain/outcomes.label_thesis`, GT2 §4).

- **Significance (pre-registered, per GT3 `predictions.py` contract):** no forward verdict is
  read until **effective-n ≥ `_MIN_DATES` = 8** independent, date-clustered, non-overlapping
  21-bday observations (`_thin_independent`, greedy ≥21-bday gap). Test stat: **Newey-West HAC**
  (`_HAC_LAGS=2`) t-stat on the per-cluster active-return series; a policy "beats" another only
  with **HAC p < 0.05 AND 95% CI lower bound > 0** on the paired (same-cluster) active-return
  difference.
- **Minimum runtime before ANY verdict:** **≥ 8 independent clusters**, i.e. ≥ 8 × 21 ≈ **168
  calendar days** from first instrumented `asof` (GT3). Until then the leaderboard reports
  **"building,"** never "winner." (See §7.)

### 4.3 Metric definitions (both arms, fixed)

- **Active return vs SPY** — `rel_return` = subject_return − SPY_return over exactly `horizon_d`
  (21) trading days, both legs anchored to the last common close ≤ entry (`label_thesis`; GT3).
- **Info Ratio** — mean(per-cluster active return) / stdev(per-cluster active return); reported
  with HAC-adjusted CI.
- **Hit-rate** — fraction of resolved theses not falsified, using the **same falsifier predicate**
  as `brain/scorer.track_record` (bullish thesis falsified iff `rel_return < −5%`), so shadow and
  prod hit-rates are directly comparable (GT2 §4).
- **Max Drawdown** — max peak-to-trough of each book's NAV series vs its own peak (forward arm)
  and per the harness crisis-window output (historical arm).

---

## 5. Pre-Registered Decision Rule (verbatim)

> **Read this rule only after BOTH arms have cleared their pre-registered significance bars
> (Arm A: DSR>0, PBO<0.5, BH-FDR q=0.10, 2022+ holdout held; Arm B: effective-n ≥ _MIN_DATES=8,
> Newey-West HAC p<0.05 with 95% CI lower bound > 0). If neither arm has cleared its bar, the
> verdict is "INSUFFICIENT POWER — keep running"; no build decision is made and no threshold is
> revised.**
>
> Define, on the **forward shadow arm**, the paired (same-cluster) active-return-vs-SPI
> difference between two policies as significant only when HAC p < 0.05 AND the 95% CI lower
> bound on that difference is > 0.
>
> **BUILD THE LLM DESK SEATS** — iff **all** hold:
> 1. `desk_proxy` beats `prod` on Info Ratio, significantly (HAC p<0.05, CI lower > 0); **and**
> 2. `desk_proxy` beats the **best single cheap lever** (max of `no_etf`, `concentrated_topN`,
>    `timing_gated`) on Info Ratio, significantly (i.e. the combo adds a real increment — H4 holds); **and**
> 3. `autonomous_clone` beats `desk_proxy` on active return, significantly (H5 holds — a holistic
>    selection edge remains that the deterministic proxy does NOT capture); **and**
> 4. on the **historical arm**, the testable levers (L2/L3) do **not** already explain the entire
>    forward edge — i.e. at least one of {the L1 (no_etf) effect, the L4 (clone) gap} is forward-only
>    and not reproducible by the frozen-gauntlet factor levers.
>
> Rationale: the seats are justified **only** when the cheap rules clear the bar *and* there is a
> residual holistic edge (the clone) that no factorable lever reproduces. That residual is exactly
> what an LLM seat could capture and a rule cannot.
>
> **SHIP THE CHEAP RULES, DEFER THE SEATS** — iff:
> - `no_etf` and/or `concentrated_topN` each beat `prod` significantly (H1/H2 hold), **and**
> - `desk_proxy` does **NOT** significantly beat the best single cheap lever (H4 fails — the combo
>   is redundant), **OR** `autonomous_clone` does **NOT** significantly beat `desk_proxy` (H5 fails
>   — no residual holistic edge).
> - **Action:** enforce the winning lever(s) as **deterministic rules** in `phase2`/`shadow_books`
>   (L1 hard wall on the alpha sleeve; L2 top-N cap; L3 timing gate if it cleared) and **defer the
>   entire seat build**. The desk's alpha was factorable; pay for rules, not for Opus.
>
> **KILL / REDIRECT THE BUY-SIDE BUILD** — iff:
> - **No** policy (`no_etf`, `concentrated_topN`, `timing_gated`, `desk_proxy`) beats `prod`
>   significantly on either arm → the levers add nothing; **do not build the buy-side seats**.
>   Redirect effort to the sell-side Risk Officer / Exit Manager (`04-sell-pipeline.md`) and the
>   accountability loop, which this experiment does not test. **OR**
> - `autonomous_clone` significantly beats **every** other policy including `desk_proxy` **and**
>   no deterministic proxy beats `prod` → the edge is *pure holistic name selection*, not any
>   desk lever (veto stack, ETF rule, concentration, timing). **Redirect:** do not build the
>   subtract-only veto desk as specified; instead clone/operationalize the Brain's *selection*
>   (e.g. promote `autonomous_clone` to a live book, or build a single Brain-style selector seat),
>   and re-pre-register a new experiment for the veto stack.
>
> **Binding constraints on this rule (Honesty over Alpha):**
> - No threshold above (N=8, p<0.05, PBO<0.5, q=0.10, `_MIN_DATES`=8, 168-day floor) may be revised
>   after data is seen. Revising any of them voids the pre-registration and requires a new dated spec.
> - The day-one result and IREN/KMT/FSS are **not** admissible evidence for any branch.
> - A tie or sub-threshold result is **"keep running,"** never a build trigger. Failing closed
>   (no build) is the default.

---

## 6. Leakage Guards & Artifacts

### 6.1 Leakage guards
- **Forward arm:** policies consume only `data/shadow/inputs/<asof>.json` (written at decision
  time by `phase2.write_inputs`; GT2) and stockdata fields whose `asof ≤ decision asof`. Grading
  uses `label_thesis` which enforces `req_end = min(final_end, asof)` — never reads closes past
  `asof`; both subject and SPY anchored to the same last common close ≤ entry; disk caching only
  when the 21-bday window is fully elapsed (GT3).
- **Historical arm:** PIT S&P 1500 membership (`sp1500_pit_membership.parquet`) gates the eligible
  set at each rebalance; survivorship-safe close panel = `_closes_deep` ∪ `_closes_delisted`; the
  **2022+ holdout is locked and read once** (GT4). Frozen judge applies purged-5-fold + DSR + PBO.
- **Isolation:** `portfolio/desk_ab.py` and `loop/desk_levers.py` **never** import the live
  run-path (`bot.phase2` only calls `write_inputs`); each policy writes to its own isolated
  directory; no prod state (`paper_account`, brain ledger, store singletons) is touched (GT2 §5).
- **N, the filters, and all thresholds are fixed in this file before any read.**

### 6.2 Artifacts / paths

| Artifact | Path |
|---|---|
| This spec (pre-registration) | `docs/design/desk/AB_EXPERIMENT.md` |
| Forward policy module | `portfolio/desk_ab.py` |
| Historical lever module | `loop/desk_levers.py` |
| Historical runner | `scripts/run_desk_levers.py` |
| Forward decision-inputs (existing) | `data/shadow/inputs/<asof>.json` |
| Per-policy forward books | `data/shadow/desk_ab/books/<policy_id>/{account.json, nav_history.jsonl, theses.jsonl}` |
| Forward leaderboard | `data/shadow/desk_ab/leaderboard.json` |
| Historical verdict (DSR/PBO/FDR/holdout) | `data/backtest/desk_levers.json` |
| Brain submitted books (L4 source) | `data/portfolios/autonomous/{latest.json, decisions.jsonl}` |

---

## 7. Honest Power Analysis — When Does Each Arm Produce a Verdict?

The books are **1–2 calendar days old** with **no forward marks stored** (GT1). This is the
defining constraint: the experiment is mostly a *forward* instrument.

| Arm / policy | Data today | First admissible verdict |
|---|---|---|
| **Arm A (historical, L2/L3)** | Full 2002–2026 panel present (GT4) | **Immediately** — one ~20–60 s run yields DSR/PBO/FDR + 2022+ holdout. This is the only verdict available now. |
| **Arm B — `no_etf`, `concentrated_topN`, `timing_gated`, `desk_proxy`** | 1–2 inputs files; books days old | **≥ 168 calendar days** (8 independent 21-bday clusters; `_MIN_DATES`=8, GT3). Reports "building" until then. |
| **Arm B — `autonomous_clone` (L4)** | 1 day of Brain books (GT1/GT4) | **≥ 168 calendar days of Brain books**, contingent on the Brain submitting daily — likely later than the deterministic policies. |

**Consequences, stated honestly:**
1. **No forward branch of §5 can fire before ~2026-12 (≈168 days from instrumentation).** Until
   then the only evidence is Arm A. Arm A alone can **kill** a lever (if L2/L3 fail the gauntlet)
   but **cannot trigger the seat build** — the build requires the forward H4/H5 increments that
   are historically untestable (L1, L4).
2. **Pre-condition to start the clock:** instrument marks (write daily `current_price`/NAV rows
   for all shadow books) and emit the L3 fields into `_emit_shadow()` — without these the forward
   arm produces no resolvable theses (GT1 data-gap #1, #2).
3. The 2026-07-17 calibration deadline (memory: edge-roadmap) is **before** any forward verdict;
   the desk build decision therefore leans on **Arm A first**, with the forward arm as the
   confirming/redirecting evidence that lands in Q4. Build sequencing must respect that: do not
   commit seat-build resources on Arm A alone — Arm A can only *clear or kill the factorable
   levers*, never justify the seats.

---
