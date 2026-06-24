# Chapter 06 — KPIs, Rewards / Reputation & Credit Assignment

> **Scope.** This chapter specifies how every seat on the Flagship desk is *measured*, what "reward" concretely means for a non-paid AI seat, how we defend the reward function against Goodhart, and how — when an outcome resolves — we decompose the realized P&L across the chain of seats that touched it. It is the accountability machinery that consumes the role roster defined in *Chapter 02 — The Desk: Organizational Structure & Decision Rights*. It depends on the learning-loop inventory of *Chapter 05 — The Accountability & Learning Loop* (which catalogues what exists vs. what is MISSING) and feeds the weekly **CIO / META-PM** review specified there. Build artifacts are pinned in *Chapter 07 — Data Contracts, Module Mapping & Phased Build Plan* and the core invariants in *Chapter 08 — Failure-Mode Register & Residual Risk*.
>
> **Grounding rule.** Every metric here resolves through the *same* leakage-free machinery already in the tree: `brain/outcomes.py::label_thesis` (rel-return vs SPY over a 21-bday horizon, both legs anchored to the last close ≤ entry), `brain/scorer.py::track_record` (Brier / hit-rate), `brain/calibration.py` (the de-confidencing multiplier, `MIN_N=12`, `FLOOR=0.5`), `portfolio/predictions.py` (date-clustered rank-IC / Newey-West / effective-n), and `portfolio/shadow_books.py` (forward counterfactual books). **No new grading definition is invented; every KPI is a view over the existing resolved-thesis substrate.** This is non-negotiable: *Honesty over Alpha* means a KPI that cannot be computed leakage-free does not ship.

---

## 6.1 Measurement substrate & invariants

Before per-role KPIs, three substrate invariants that every metric inherits:

| Invariant | Source | Consequence for KPIs |
|---|---|---|
| **Leakage-free resolution** | `outcomes.label_thesis` anchors both subject and SPY to last close ≤ entry; `req_end=min(final_end,asof)` | A KPI can only move when a thesis *resolves*; no metric ever peeks at a price after `asof`. |
| **Directional-only grading** | `scorer.py:53,92` skip `kind!="rel_return"` | Non-directional decisions (a WATCHLIST park, a CONDITIONAL adversary stance) are **not** scored right/wrong — they get their own ledgers (§6.2.10) but never pollute Brier. |
| **Significance gating** | `calibration.MIN_N=12`; `predictions._MIN_DATES=8`; `readiness.py` thresholds | A KPI is displayed once `n≥1` but is **INERT for reward** (cannot change influence/budget/authority) until it clears its effective-n floor. Luck is shown, never rewarded. |

Every decision a seat emits is logged as a **falsifiable, gradable decision record** with: `{seat, decision_type, subject, asof, stance, confidence, raw_confidence, falsifier{check{kind,op,threshold}}, check_by}`. This is the schema `calibration._sentinel_reliability` already reads for SENTINEL (`stance`, `raw_confidence`); §6.2 generalises it to every seat.

---

## 6.2 Per-role KPI definitions

Conventions: `r` = rel-return vs SPY at resolution (`outcomes.label_thesis(...)["rel_return"]`). `H` = horizon (default 21 bday). `n_eff` = effective-n = count of *independent, non-overlapping ~21-bday* clusters (`predictions._thin_independent`). A metric is **HOT** (reward-eligible) only when `n_eff ≥ MIN_N` for that metric's floor; otherwise **COLD** (display-only). All KPIs persist to `data/brain/kpis/<seat>.json` per build.

### 6.2.1 SCOUT — sourcing yield

SCOUT emits candidate ideas; it does not size. Its job is *recall of eventual winners* at *acceptable precision*, cheaply.

| KPI | Formula | Floor (n_eff) | Good |
|---|---|---|---|
| **Idea yield** | `candidates_surfaced / build` (volume) | — | context only |
| **Conversion rate** | `# candidates that became positions / # surfaced` | 20 | higher = better targeting |
| **Surfaced-winner rate** (recall proxy) | of names that *anyone* later bought-and-won, `# SCOUT surfaced first / total` | 20 | high recall |
| **Dud rate** | `# surfaced names that, had they been bought at surface date, would have r < −5% over H / # surfaced` | 20 | **low** |
| **Cost-adjusted yield** | `winning_conversions / scout_token_cost` | 20 | efficiency |

Dud rate is computed by labeling a *shadow thesis* for **every** surfaced name (bought-at-surface counterfactual), exactly as `predictions.py` labels the 1,600-name universe — SCOUT is graded on names it surfaced *whether or not the desk bought them*. Tie-break: a name surfaced by SCOUT on day T and again on T+3 counts once (dedup on `(subject, open-while-unresolved)`, mirroring the ledger dedup in `shadow_books._update_theses`).

### 6.2.2 MACRO STRATEGIST — backdrop & timing

Two distinct calls, graded separately: the **regime/sector call** (allocation) and the **entry-timing** verdict (was *now* supportive for this name).

| KPI | Formula | Floor | Good |
|---|---|---|---|
| **Sector-call accuracy** | hit-rate of "sector X supportive" → that sector's fwd-21d rel-return vs SPY > 0 | 8 dates | >0.55 |
| **Regime-rank IC** | rank-IC between Strategist sector-support score and realized sector rel-return, Newey-West t-stat (HAC lag 2) | 8 dates | t>2 |
| **Entry-timing alpha** | mean `r` of names Strategist marked "backdrop supportive NOW" **minus** mean `r` of names it marked "supportive but unsupported-timing" | 12 | >0 |
| **False-withhold cost** | mean `r` of names Strategist withheld on backdrop that the desk *later* bought — measures regret of its WITHHOLDs | 12 | should be ≤0 (it withheld correctly) |

`False-withhold cost` requires the withheld name to eventually enter via WATCHLIST promotion so a real entry price exists; until then it sits in the watchlist ledger (§6.2.10). The sector-call leg reuses `predictions.rank_ic` / `ic_summary` directly — sectors are just a coarser cross-section.

### 6.2.3 ANALYST / FORGE — thesis quality

FORGE already has the only live calibration grade (`calibration._forge_reliability`). KPIs formalise it.

| KPI | Formula | Floor | Good |
|---|---|---|---|
| **Thesis hit-rate** | `scorer.track_record` over FORGE theses: `hits/n`, hit = NOT falsified by own threshold | 12 | >0.50 |
| **Brier** | `mean((prob_correct − outcome)²)` on **raw_prob_correct** (`scorer._brier`) | 12 | <0.25 (beat climatology) |
| **Calibration multiplier** | `calibration.multiplier("forge")` = `clamp(reliability/mean_conf, 0.5, 1.0)` | 12 | →1.0 (well-calibrated) |
| **Fair-value error** | `median(|realized_price_at_check_by − fair_value| / fair_value)` over resolved theses | 12 | low |
| **Selection alpha** | mean `r` of FORGE-confirmed names (the picker's raw edge) | 12 | >0 |

Grading uses `raw_prob_correct` deliberately (`calibration.py:69`) so the loop measures *native* overconfidence and converges to a fixed point — never the already-shrunk value.

### 6.2.4 TECHNICIAN / TACTICIAN — entry quality

The Technician's verdict is `enter_now | staged_starter | wait`. It is graded on whether the *entry it endorsed* was timely — measured with MAE/MFE over the entry window, computed leakage-free from the macro breadth panel (`predictions._load_panel`).

| KPI | Formula | Floor | Good |
|---|---|---|---|
| **Setup→outcome hit** | hit-rate of `enter_now` verdicts where fwd-10d `r > 0` | 12 | >0.55 |
| **Premature-entry rate** | `# enter_now names whose MAE < −8% within 10 bday before recovering / # enter_now` | 12 | **low** |
| **MAE / MFE** | per endorsed entry: `MAE = min(P_t/P_0−1)`, `MFE = max(P_t/P_0−1)` over [0, H]; report medians | 12 | MFE/|MAE| high |
| **Wait-was-right rate** | of `wait` verdicts, `# where the name was cheaper within 10 bday / # wait` | 12 | high = good patience |
| **Staged-starter edge** | mean `r` of staged starters vs full entries (did fractioning help?) | 12 | context |

MAE/MFE windows anchor to the *endorsed entry date's* last close ≤ entry (same anchor rule as `outcomes`). A `wait` that the desk obeyed is a *non-directional* record graded by the counterfactual "cheaper-within-10d" predicate, not by Brier.

### 6.2.5 ADVERSARY / SENTINEL — veto value

SENTINEL already has a calibration grade (`calibration._sentinel_reliability`: OPPOSE→correct if `r<0`, SUPPORT→correct if `r≥0`, CONDITIONAL excluded). KPIs add a confusion-matrix view and a **bps-saved** ledger.

| KPI | Formula | Floor | Good |
|---|---|---|---|
| **Veto precision** | `# OPPOSE where r < 0 / # OPPOSE` (TP / (TP+FP)) | 12 OPPOSEs | >0.55 |
| **Veto recall** | `# losers (r<0) it OPPOSEd / # losers it saw` | 12 losers | high |
| **bps of loss saved** | `Σ over downsized/blocked names of (−r_realized × foregone_weight × NAV)` in bps; only counts names where its stance *changed* the size | 12 | high positive |
| **False-oppose cost** | `Σ r_realized × foregone_weight` over OPPOSEd names that *won* (opportunity lost) | 12 | low |
| **Calibration multiplier** | `calibration.multiplier("sentinel")` | 12 | →1.0 |

"bps saved" only credits SENTINEL when subtract-only actually bit — i.e. NEXUS's `scale<1.0` traced to SENTINEL (`shadow_books` already isolates `no_committee` to measure exactly this delta). The counterfactual is the `no_committee` shadow book's realized return on that name.

### 6.2.6 PM — CONVICTION (Idea Champion)

The champion proposes adds and target sizing *intent* (it cannot self-approve). Graded on the quality and conviction-monotonicity of its picks.

| KPI | Formula | Floor | Good |
|---|---|---|---|
| **Idea win-rate (batting avg)** | `# championed names with r>0 / # championed` | 20 | >0.50 |
| **Slugging (avg win / avg loss)** | `mean(r | r>0) / |mean(r | r<0)|` | 20 | >1.5 |
| **Contribution-to-return** | `Σ (championed weight × r) ` — its share of book active return | 20 | high |
| **Conviction monotonicity** | rank-IC between its *intended* size and realized `r` (did it size winners bigger?) | 12 dates | >0 |
| **Over-conviction penalty** | `Σ over named where intended_size was top-quartile and r<−5%` | 12 | low |

Batting avg and slugging are reported **together** by design (Goodhart guard §6.4): a champion can win the batting title with tiny wins and one ruinous loss; slugging catches it.

### 6.2.7 PM — GATE OFFICER (Veto authority)

Subtract-only final authority. Graded on portfolio-level discipline and the *cost of its vetoes*.

| KPI | Formula | Floor | Good |
|---|---|---|---|
| **Approved-book hit-rate** | hit-rate of names it APPROVED | 20 | > the unfiltered candidate hit-rate |
| **Avoided drawdown** | `Σ over VETOed names of (−min(0, r) × proposed_weight × NAV)` in bps | 12 vetoes | high (it blocked losers) |
| **False-veto opportunity cost** | `Σ over VETOed-then-watchlisted-then-won names of (r × proposed_weight)` | 12 | low |
| **Concentration discipline** | realized `% NAV in top-5` vs mandate ceiling; breaches/build | — | 0 breaches |
| **Mandate-violation rate** | `# approvals that breached no-ETF / max-names / min-conviction / 0` | — | **must be 0** (hard) |

The Gate Officer's *no-ETF / max-names / min-conviction* breaches are not a soft KPI — they are NEXUS invariants (`committee.py` subtract-only line 9, caps); a nonzero count is a **build alarm**, not a score.

### 6.2.8 RISK OFFICER / EXIT MANAGER — held-name discipline

Owns trims/exits only. Graded on exit *timing* relative to the counterfactual hold.

| KPI | Formula | Floor | Good |
|---|---|---|---|
| **Exit-timing alpha** | `r_at_exit − r_if_held_to_H` per exit (positive = exited before further loss / before mean-reversion gave it back) | 12 exits | >0 |
| **Premature-sell rate** | `# exits where name's r rose > +5% in the 10 bday after exit / # exits` | 12 | **low** |
| **Disposition score** | `mean(holding_days | winners) − mean(holding_days | losers)` — negative = the disposition effect (riding losers, cutting winners) | 12 | **>0** |
| **Drawdown avoided** | `Σ over exits of (−min(0, r_if_held) × weight × NAV)` bps | 12 | high |
| **Falsifier-catch lag** | median bday between a held thesis's falsifier firing and the Risk Officer's exit | — | low |

`r_if_held` reuses the same panel labeling — we mark the name forward to `H` as if never sold, leakage-free. The disposition score is the institutional check that the seat is not behaviorally biased.

### 6.2.9 CIO / META-PM — did tuning help

The CIO does not trade; it tunes role weights/budgets/authority weekly. It is graded on whether its tuning *improved forward book metrics* — a difference-in-differences on the books it touched.

| KPI | Formula | Floor | Good |
|---|---|---|---|
| **Tuning lift** | `Δ(book information ratio)` over the 4 weeks *after* a tuning action vs the 4 weeks before, vs the untouched shadow books as control | 4 actions, n_eff≥8 dates each | >0 |
| **Reversal rate** | `# tuning actions reversed within 2 weeks / # actions` | 4 | low (no thrashing) |
| **Calibration of its own calls** | Brier on CIO's "this reweight will help" predictions | 12 | <0.25 |

The `shadow_books` leaderboard (`prod` vs `no_calibration` vs `engine_only` …) **is** the CIO's control group: an untouched counterfactual book isolates whether the tuning, not the market, moved the metric.

### 6.2.10 Non-directional ledgers (the records Brier ignores)

WATCHLIST parks, CONDITIONAL adversary stances, and `wait` technician verdicts are **not** falsifiable rel-return bets and are excluded from Brier (`scorer.py:92`). They get dedicated ledgers graded by *predicate*, not probability:

| Record | Ledger | Resolution predicate |
|---|---|---|
| Watchlist park | `data/brain/watchlist_ledger.jsonl` | promoted-and-won / promoted-and-lost / still-parked / dropped-and-name-ran (regret) |
| CONDITIONAL stance | committee artifacts | excluded from veto precision; counted in "stance coverage" only |
| `wait` verdict | technician ledger | cheaper-within-10d (right) / ran-away (wrong-to-wait) |

### 6.2.11 BOOK-LEVEL KPIs

Computed per book (Flagship + the 5 shadow books) from `nav_history.jsonl` and the resolved-thesis set. These are the top of the funnel the CIO reads.

| KPI | Formula | Source |
|---|---|---|
| **NAV vs benchmark** | `nav/starting_nav − 1` minus SPY over same window | `shadow_books._book_summary.vs_spy_pct` |
| **Information ratio** | `mean(active_daily_ret) / std(active_daily_ret) × √252` | NAV series − SPY |
| **Sharpe / Sortino** | `mean(ret)/std(ret)×√252` ; Sortino uses downside dev only | NAV series |
| **Hit-rate** | `hits/n` resolved theses | `scorer.track_record` |
| **Avg win / avg loss** | `mean(r|r>0) / |mean(r|r<0)|` | resolved set |
| **Max drawdown** | `min_t (NAV_t / max_{s≤t} NAV_s − 1)` | NAV series |
| **Turnover** | `Σ|Δweight| / 2` per build, trailing mean | account deltas |
| **Concentration** | HHI of weights; `% in top-5` | account |
| **% green** | `# positions with unrealized r>0 / # positions` | marks |

---

## 6.3 Reward / reputation design — what "reward" means for an AI seat

A seat cannot be paid. "Reward" is **five concrete, bounded levers**, each tied to a HOT (significance-cleared) KPI and each respecting **subtract-only**. The per-seat reward vector is recomputed weekly by the CIO and persisted to `data/brain/reputation/<seat>.json`.

| Lever | Mechanism | Bounds & subtract-only guard |
|---|---|---|
| **(a) Influence weight** | A seat's stated confidence is multiplied by its calibration multiplier before NEXUS reads it (exactly `committee.py:111-115` today, generalised to all seats). A well-calibrated seat's vote counts *more* up to 1.0. | `multiplier ∈ [FLOOR=0.5, 1.0]` — **never >1.0**. A hot seat can de-shrink toward its true reliability but can **never amplify additive size**. Influence only ever *removes* the de-confidencing haircut. |
| **(b) Risk / capital budget** | A book (or the conviction-budget share a seat's championed names draw) earning risk-adjusted alpha *keeps* its mandate; a losing book is throttled. | Budget changes are **multiplicative in [0.5, 1.0]** of mandate, applied only when `IR` is significant (n_eff≥8). **There is no automated additive growth lever** — a well-performing book keeps its mandate, an underperforming one is throttled. Growth in risk mandate is a human/CIO config decision, never an automated reward lever. (Preserves the subtract-only invariant: no seat performance can *add* risk.) |
| **(c) Reputation score** | A 0–100 leaderboard rank per seat = significance-weighted blend of its HOT KPIs (§6.4 multi-metric). Cosmetic + an input to (e). | Display + CIO input only; no direct sizing effect. |
| **(d) Persistent memory (self-mirror)** | Each seat's own graded track record (recent wins/losses, calibration, characteristic failure modes) is injected into its daily prompt — retrospective + prospective reflection. | Memory writes are **quality-gated + contradiction-checked** (Chapter 05): a claim only persists if it survived resolution; a refuted "lesson" is pruned. Self-mirror cannot grant authority. |
| **(e) Authority promotion/demotion** | The CIO may raise/lower a seat's mandate (e.g. expand SCOUT's daily quota, grant the Technician a `staged_starter` size knob, or **demote** a chronically miscalibrated seat to advisory-only). | Promotions require **two consecutive** significant epochs of positive lift; demotions can fire on one (subtract-only asymmetry: easy to remove power, hard to grant). Never grants additive-sizing authority to a judgment seat — NEXUS keeps the floor. |

**Per-Brain reward structure (explicit).** Each LLM seat ("Brain") carries a reward record:

```json
{
  "seat": "SENTINEL",
  "epoch": "2026-W29",
  "kpis_hot": {"veto_precision": 0.61, "bps_saved": 38, "calibration_mult": 0.92},
  "kpis_cold": {"false_oppose_cost": -0.4},
  "reputation": 71,
  "influence_multiplier": 0.92,
  "risk_budget_mult": 1.0,
  "authority": "full",
  "self_mirror_digest": "Your OPPOSE on high-vol names paid (avg saved 38bps); your OPPOSE on quality names at extension cost 0.4% — distinguish distribution from healthy pullback.",
  "significance": {"n_eff": 14, "status": "HOT"}
}
```

**Tie-break / cold-start.** A COLD seat (n_eff < floor) receives `influence_multiplier = 1.0`, `risk_budget_mult = 1.0`, `authority = full` (its mandated default), and **no** reputation rank (shown as "building"). Reward is strictly inert until evidence exists — identical in spirit to `calibration._mult` returning `1.0` below `MIN_N`.

---

## 6.4 Anti-reward-hacking / Goodhart defenses

Four structural defenses, each mapped to a mechanism already in the tree:

1. **Reward risk-adjusted + attribution-based, never raw return.** Budget (lever b) keys off **Information Ratio / Sortino**, not NAV. A seat cannot earn budget by levering into beta — book-level IR penalises volatility, and §6.5 attribution strips out market/sector beta so only *seat-attributable* active return scores. (This is why `shadow_books.risk_tilt` exists: low-risk is a Sharpe lever, *not* a picker — its leaderboard slot proves the distinction.)

2. **Significance / effective-n gating.** No reward lever moves while a seat is COLD. `n_eff` is the date-clustered, non-overlapping count (`predictions._thin_independent`, `_MIN_DATES=8`), **never** a raw n. A 3-day hot streak (the n=1 caveat, Chapter 01) literally cannot change influence, budget, or authority. Newey-West HAC (lag 2) gates the IC-based KPIs.

3. **Subtract-only firewall.** The single most important Goodhart guard: every reward lever is bounded `≤1.0` — influence multiplier ∈ [0.5, 1.0] and risk-budget multiplier ∈ [0.5, 1.0]. **There is no automated additive-risk lever at all**, so **a hot seat physically cannot force size into its favourite name.** A seat that games its KPI still cannot translate the score into additive risk — it can only earn back de-confidencing. This is the structural reason reward-hacking is low-stakes here.

4. **Diversity of metrics (no single number).** Reputation (lever c) is a **blend of orthogonal KPIs deliberately chosen to conflict** — batting avg *and* slugging; precision *and* recall; hit-rate *and* Brier *and* calibration; contribution *and* over-conviction penalty. Gaming one degrades another. The reputation blend uses **min-of-percentile-ranks with a floor**, not a sum, so a seat cannot bury one terrible metric under several mediocre ones:

   `reputation = 100 × ( 0.5·mean(pct_ranks) + 0.5·min(pct_ranks) )`, computed only over HOT KPIs.

---

## 6.5 Credit assignment / attribution

When a thesis resolves (`outcomes.label_thesis → r`), its realized active return (`a = r`, already net of SPY) is **decomposed across the chain of seats that touched it**, Brinson-adapted to this pipeline. The decomposition runs in `brain/attribution.py` (new, Chapter 07) per resolved name and accumulates into each seat's KPI ledger.

### 6.5.1 The chain & the four Brinson-adapted components

Classical Brinson splits active return into **allocation** (sector call) × **selection** (name pick) × **interaction**. We extend it with **timing** and **sizing/exit** legs to match the desk's separation of powers:

| Component | Captures | Owner seat | Definition |
|---|---|---|---|
| **Allocation** | was the *sector* the right place to be | MACRO STRATEGIST | `(w_sec − w_sec^bench) × (r_sec^bench − r_total^bench)` |
| **Selection** | was *this name* better than its sector | ANALYST/FORGE, PM-CONVICTION | `w_sec^bench × (r_name − r_sec^bench)` |
| **Timing** | did entering *now* (vs sector-average entry date) add return | TECHNICIAN, MACRO STRATEGIST | `r_name(actual entry) − r_name(sector-cohort entry date)` |
| **Sizing** | did the *weight* amplify the right names | PM-CONVICTION (intent) / NEXUS (floor) | `(w_actual − w_equal) × r_name` |
| **Exit** | did selling early/late add return | RISK OFFICER | `r_at_exit − r_if_held_to_H` (only for exited names) |
| **Veto/Gate** | did blocking/passing add return | SENTINEL, PM-GATE OFFICER | counterfactual: `−r_realized × foregone_weight` (credited only when their action changed the size — measured vs `no_committee` shadow book) |

These sum to `a` up to an **interaction residual** `ε = a − (alloc+sel+timing+sizing+exit)`, which is logged but not attributed to any seat (institutional convention: interaction is shared, not gamed).

### 6.5.2 Algorithm (per resolved name)

```
on resolve(name):
  a        = label_thesis(name).rel_return                 # active vs SPY, leakage-free
  r_sec    = sector_fwd_relret(name.sector, entry, H)      # from breadth panel, vs SPY
  r_cohort = mean r of same-sector names entered ±2 bday   # the "average entry" counterfactual
  alloc    = strategist_sector_tilt(name) * r_sec
  select   = a_relative_to_sector(name)                    # r_name − r_sec
  timing   = r_name_from(actual_entry) − r_name_from(r_cohort_date)
  sizing   = (w_actual − w_equal_weight) * a
  exit     = (r_at_exit − r_if_held_to_H) if exited else 0
  veto     = counterfactual_block_credit(name)             # vs no_committee shadow book
  eps      = a − (alloc+select+timing+sizing+exit)
  # route bps to seats (a seat may share a component; split by logged contribution share)
  credit[STRATEGIST]   += alloc + share(timing, STRATEGIST)
  credit[FORGE]        += w_share(select, FORGE)
  credit[CONVICTION]   += w_share(select, CONVICTION) + sizing_intent_share
  credit[TECHNICIAN]   += share(timing, TECHNICIAN)
  credit[RISK_OFFICER] += exit
  credit[SENTINEL]     += veto_share
  credit[GATE_OFFICER] += veto_share
  ledger.append({name, a, alloc, select, timing, sizing, exit, veto, eps, asof})
```

**Split rule for shared components.** When two seats co-own a component (e.g. Strategist + Technician both shaped *timing*), the bps are split **proportional to each seat's logged contribution stake** recorded at decision time (`contribution_share` field on the decision record), defaulting to equal split if unrecorded. This is the one place subjectivity enters; it is *logged and auditable*, never inferred after the fact.

### 6.5.3 Edge cases & tie-breaks (all holes patched)

| Case | Rule |
|---|---|
| Name **never bought** (SCOUT surfaced, desk passed) | No P&L attribution; SCOUT graded on the counterfactual shadow thesis only (§6.2.1). |
| Name **vetoed by Gate Officer**, later won | `veto` is **negative** credit to Gate Officer (false-veto cost); SCOUT/FORGE keep their idea credit on the counterfactual. |
| **Sector data missing** for a name | `alloc = 0`, full residual to `select`; flagged `partial_attribution=true` (never silently dropped). |
| Name **trimmed not exited** | `exit` leg computed on the *trimmed fraction* only; held fraction continues accruing. |
| **Re-entry** (sold then re-bought) | Each leg is a *separate* resolved record with its own attribution; ledgers never net across re-entries. |
| Resolution **vs SPY undefined** (offline/CI) | `label_thesis` returns unresolved → **no attribution runs**; the name stays open. KPIs honestly stay COLD. |
| Seat **emitted no stance** (e.g. SENTINEL never ran) | That seat gets **zero** credit/blame on that name — absence is never scored (mirrors `calibration` skipping un-graded committee artifacts). |
| **Interaction residual `ε` large** (|ε| > |a|/2) | Logged as `attribution_unstable`; excluded from that build's reputation update (don't reward noise). |

### 6.5.4 Feedback into reputation & CIO tuning

Accumulated per-seat credit feeds:
- **Reputation (§6.3c)** — a seat's trailing-epoch credit, IR-normalised, is one of the HOT KPIs in the blend.
- **CIO tuning (§6.2.9)** — the CIO reads the attribution ledger to answer *which seat earned the bps*, then adjusts influence/budget/authority. The `shadow_books` counterfactuals are its control: if `no_committee` out-earns `prod`, the committee seats' credit is *negative* in aggregate and the CIO throttles their influence.
- **The 2026-07-17 first-resolution window** (Chapter 05) — the earliest date enough 21-bday theses resolve for *any* attribution to clear `n_eff`. Until then **the entire reward/attribution system is COLD by construction** and changes no behavior. This is not a limitation; it is the *Honesty over Alpha* invariant made operational.

---

## 6.6 Summary authority matrix

| Seat | KPI floor | Reward levers it can earn | Can it gain additive size? |
|---|---|---|---|
| SCOUT | n_eff 20 | influence (advisory), reputation, memory, quota (authority) | No |
| MACRO STRATEGIST | 8 dates | influence, budget, reputation, memory, authority | No (allocation tilt only, capped) |
| ANALYST/FORGE | 12 | influence, reputation, memory | No |
| TECHNICIAN | 12 | influence, reputation, memory, staged-size knob (authority) | No (timing/fraction only) |
| ADVERSARY/SENTINEL | 12 | influence, reputation, memory | **No (subtract-only)** |
| PM-CONVICTION | 20 | influence (intent), budget, reputation, memory | No — proposes; NEXUS sizes |
| PM-GATE OFFICER | 20 | reputation, authority | **No (subtract-only; veto/downsize/park)** |
| RISK OFFICER | 12 | influence, reputation, memory | **No (trim/exit only)** |
| CIO / META-PM | 4 actions | meta-authority (tunes others) | No (does not trade) |
| NEXUS | — | (deterministic; owns the additive floor + invariants) | **Sole additive sizer, under hard caps** |

The matrix encodes the chapter's thesis: **reward flows to judgment, additive size never does.** Every seat can earn influence, reputation, memory, and (for books) risk budget — but the right to *add* a name and *grow* its size lives only in NEXUS, under the caps and quorum defined in *Chapter 03* and enforced via `nexus()` in *Chapter 07*.
