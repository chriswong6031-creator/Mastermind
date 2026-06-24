# Chapter 05 — The Accountability & Learning Loop

> *"Investing is serious work and there is no room for errors."* The desk earns the right to act
> autonomously only because every act it takes is recorded as a falsifiable claim, graded against
> a benchmark with no look-ahead, and fed back so each seat is paid in proportion to its realized
> reliability — not its rhetoric. This chapter is the engine room of that contract. It specifies how
> **every decision type** becomes a gradable prediction, exactly **what counterfactual** each is
> graded against, the **real modules** that compute the grades, the **self-mirror** memory loop that
> turns grades into better prompts, the **CIO / META-PM** weekly review that can re-weight the desk,
> and the **statistical-honesty guards** that prevent the loop from learning noise.

This chapter assumes the desk roster and authority model of *Chapter 02 — The Desk: Organizational
Structure & Decision Rights*, the buy-side verdicts of *Chapter 03 — The Buy Pipeline & Watchlist
Subsystem*, and the judgment-exit mechanics of *Chapter 04 — The Sell Pipeline & Risk Officer /
Exit Manager*. It is the consumer of all of them: it grades what they decide. The
build mapping for every module named below lands in *Chapter 07 — Data Contracts, Module Mapping &
Phased Build Plan*.

---

## 5.1 The Core Loop

Every seat — SCOUT, MACRO STRATEGIST, ANALYST/FORGE, TECHNICIAN/TACTICIAN, ADVERSARY/SENTINEL,
PM–CONVICTION, PM–GATE OFFICER, RISK OFFICER/EXIT MANAGER — emits **claims**, not just actions. A
claim is a `(stance, confidence, horizon, success-condition)` tuple. NEXUS (the deterministic spine)
serialises every claim into a falsifiable thesis record the moment it is made, with the price/regime
state frozen at decision time (leakage-free anchor). The grader subsystem later replays realized
prices and labels each claim **HIT / MISS / UNRESOLVED** against its own success condition, vs SPY
(or the book's benchmark for non-US books, §5.6). Grades flow into four sinks: **per-role
calibration** (confidence shrinkage), **per-role KPIs** (the scorecard the CIO reads), **reputation**
(quorum/influence weight), and **self-mirror memory** (each role's track record injected back into
its own next prompt).

```
                    ┌──────────────────────────────────────────────────────────┐
                    │  DESK (Ch 02–04): each seat emits a CLAIM at decision time │
                    │  (stance, confidence, horizon_d, explicit success cond.)   │
                    └───────────────┬──────────────────────────────────────────┘
                                    │  NEXUS serialises → falsifiable thesis
                                    │  (entry anchor frozen; raw_confidence kept)
                                    ▼
        data/committee/<asof>/<TICKER>/<seat>.json     (the decision ledger; Ch 07 §7.2.1 canonical)
                                    │
                 (≥ horizon_d business days elapse; prices realize)
                                    ▼
   ┌────────────────────────────────────────────────────────────────────────────┐
   │ GRADER  (brain/outcomes.label_thesis — leakage-free, rel_return vs SPY)     │
   │   HIT / MISS / UNRESOLVED  +  rel_return  +  triple-barrier (TARGET/STOP/TIME)│
   └───────────────┬───────────────┬───────────────┬───────────────┬────────────┘
                   ▼               ▼               ▼               ▼
          ┌──────────────┐ ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
          │ CALIBRATION  │ │   KPIs       │ │  REPUTATION  │ │ SELF-MIRROR  │
          │ multiplier   │ │ (CIO reads)  │ │  quorum wt.  │ │ memory inject│
          │ (shrink conf)│ │ Brier/IC/…   │ │  (Ch 06)     │ │ → next prompt│
          └──────┬───────┘ └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
                 │                │                │                │
                 └────────────────┴───────┬────────┴────────────────┘
                                          ▼
                         CIO / META-PM weekly review (§5.4)
                       (tunes weights/mandates, gated on effective-n)
                                          │
                                          ▼
                              back to the DESK next build
```

**Invariant LL-1 (everything gradable).** No seat may emit an action that is not also a logged,
falsifiable, horizon-bound claim. A verdict with no success condition is rejected by NEXUS at
serialise time — it cannot enter the book. **There are no ungraded decisions.**

**Invariant LL-2 (leakage-free).** A claim is graded only on prices at/after its own entry anchor,
capped at `asof` (`req_end = min(final_end, asof)`), with both the subject and the benchmark anchored
to the last close ≤ entry. This is the existing `brain/outcomes.py` discipline; the loop reuses it
verbatim for every new decision type. No grader ever reads a price the seat could not have seen.

**Invariant LL-3 (raw-vs-shrunk separation).** Calibration grades the **raw** stated confidence
(`raw_prob_correct`, `raw_confidence`), never the already-shrunk value — otherwise the loop
oscillates (shrink → look calibrated → un-shrink → overconfident). This is enforced today in
`brain/calibration.py:69` and `:105`; every new role inherits a `raw_*` field.

---

## 5.2 Per-Decision-Type Grading Specification

Each decision type below is graded against an explicit **counterfactual** — the alternative world we
compare the seat's call to. Without a counterfactual a grade is meaningless ("the stock went up" is
not credit; "it beat the benchmark *and* beat the would-have-waited entry" is). All horizons are 21
business days unless the falsifier overrides, matching the conviction sleeve.

| # | Decision (role) | Success condition | Counterfactual | Grader module | Status |
|---|---|---|---|---|---|
| 1 | **FORGE buy thesis** | `rel_return` not falsified by its own `falsifier.check` (op/threshold) over `horizon_d` | SPY | `outcomes.label_thesis` → `calibration._forge_reliability` | **EXISTS** |
| 2 | **SENTINEL adversary** | directional: `OPPOSE`→correct if subject `rel_return<0`; `SUPPORT`→correct if `≥0` (CONDITIONAL excluded) | SPY (sign) | `calibration._sentinel_reliability` | **EXISTS** |
| 3 | **STRATEGIST regime call** | sector/regime "supportive-now" call correct: the named sector's fwd `rel_return` sign matches the call | SPY-relative sector return | `outcomes.label_thesis` on a sector-ETF-subject thesis | **NEW** |
| 4 | **TECHNICIAN entry timing** | the chosen entry mode (`enter_now` / `staged` / `wait`) beat its alternative | would-have-waited price + staged-vs-full | `predictions.py` (entry-grid) + shadow A/B | **NEW** |
| 5 | **GATE-OFFICER veto** | vetoed name underperformed the bought set **and** the benchmark | the bought cohort + SPY | veto-shadow cohort (`shadow_books`-style) | **NEW** |
| 6 | **WATCHLIST park** | promotion was timely; parked names that ran cost us, parked names that fell saved us | the run-while-parked path vs the avoided-loss path | watchlist-shadow ledger | **NEW** |
| 7 | **RISK-OFFICER exit/trim** | post-exit path fell (good sell) or rose (premature sell); drawdown avoided | the hold-counterfactual (mark the position as if never sold) | exit-shadow ledger | **NEW** |
| 8 | **PM-CONVICTION sizing intent** | larger-sized names out-returned smaller-sized names (size-weighted hit) | equal-weight counterfactual | `predictions.py` size-bucket IC | **NEW** |

### 5.2.1 FORGE buy thesis (exists)
Graded today. The thesis carries `falsifier.check = {kind:"rel_return", op, threshold}`;
`outcomes.label_thesis` resolves it; `_forge_reliability` rolls all DUE+resolved theses into a
`(hit, raw_prob_correct)` row set. **No change** beyond making sure every FORGE paper writes a
`raw_prob_correct` (it does). This is the reference implementation every new type imitates.

### 5.2.2 SENTINEL adversary (exists, to be broadened)
Graded today on **directional correctness** of stance vs realized `rel_return` sign. The SENTINEL
profile (*Chapter 02 §2.2*) broadens SENTINEL beyond macro/portfolio-fit; the grading contract is
**unchanged** — a broadened SENTINEL still emits `(stance ∈ {OPPOSE, SUPPORT, CONDITIONAL},
raw_confidence)` and is graded by `_sentinel_reliability`. New bear lenses (fundamental,
technical) get **their own sub-call rows** so the CIO can see *which* bear lens earns its keep, but
they fold into one SENTINEL reliability number for the calibration multiplier.

### 5.2.3 STRATEGIST regime/rotation call (new)
The MACRO STRATEGIST's daily "is the backdrop supportive for this name NOW" reduces to a directional
sector call: it names the candidate's sector and asserts `supportive ∈ {yes, neutral, no}` with a
confidence. NEXUS materialises this as a thesis whose **subject is the sector ETF** and whose
success condition is `sector rel_return vs SPY` over the horizon (sign-matched to the call). Reuse
`outcomes.label_thesis` directly — sector ETFs are in the local panel. Counterfactual: SPY. This
finally **grades the top-down judgment that no seat owns today.**

### 5.2.4 TECHNICIAN entry-timing (new — the marquee addition)
This is the decision type the current Flagship has **no grade for at all**. The TECHNICIAN emits one
of three verdicts with a stop/level geometry:

```
ENTRY VERDICT STATE MACHINE
   candidate ── chart read ──▶ ┌─ enter_now  ─▶ buy at today's close (full intent)
                               ├─ staged     ─▶ buy starter (_INITIAL_SIZE_FRACTION), re-arm rest
                               └─ wait       ─▶ no buy; name → WATCHLIST with trigger level
```

Grading uses a **timing grid** logged into `predictions.py` (universe log, for sample power). For
each candidate, on the decision date, we record forward the four counterfactual fills and compare at
horizon:

| Counterfactual leg | Definition | Verdict it validates |
|---|---|---|
| `fill_now` | enter at decision-day close | `enter_now` |
| `fill_waited` | enter at the best close within a `wait_window` (e.g. 5 bdays) at/below the trigger | `wait` |
| `fill_staged` | half now + half at the staged re-arm fill | `staged` |
| `fill_never` | benchmark only (the name was correctly skipped) | a hard `wait`/skip |

A `wait` verdict is a **HIT** iff `fill_waited`'s horizon `rel_return` ≥ `fill_now`'s by a material
margin (the patience earned alpha); a `staged` verdict is a HIT iff `fill_staged` ≥ `max(fill_now,
fill_waited)` on a risk-adjusted basis (lower drawdown counts). An `enter_now` is a HIT iff
`fill_now` ≥ both alternatives. This directly answers **"did staged entries beat full?"** and **"did
waiting beat buying now?"** as a per-verdict hit-rate feeding `calibration.multiplier("technician")`.
Because every candidate (bought or not) gets a timing grid, the sample reaches MIN_N in weeks via the
universe log, not the ~7 owned names.

### 5.2.5 GATE-OFFICER veto (new)
A veto is subtract-only (Ch 02) but is itself a **gradable claim**: "this name, had we bought it,
would have done worse than what we *did* buy and worse than SPY." NEXUS logs every vetoed/downsized
candidate as a phantom long (subject = the vetoed ticker, entry = decision-day close). At horizon:

- **Good veto** — vetoed name's `rel_return` < the **bought-cohort mean** `rel_return` for that
  build **and** < 0 (underperformed both peers and benchmark).
- **Costly veto** — vetoed name ripped: `rel_return` > bought-cohort mean **and** > 0.

The bought-cohort mean is the counterfactual (not just SPY) so a veto in a strong-tape build is
judged against what the desk *actually had access to that night*, not an absolute bar. Veto hit-rate
feeds `calibration.multiplier("gate_officer")` and a dedicated KPI; a chronically **costly** Gate
Officer is throttled by the CIO (§5.4), never silently obeyed.

### 5.2.6 WATCHLIST park (new)
Watchlist is a first-class state (Ch 03): every parked name is an open claim "not yet, but on the
trigger." Two graded sub-metrics:

- **Opportunity cost** — for names parked and *never promoted*, the run-while-parked `rel_return`. A
  positive run that we never caught is a **miss against the watchlist policy** (we were too slow),
  unless the name's risk made the skip correct (cross-checked against the drawdown it would have
  imposed).
- **Promotion timeliness** — for names parked then promoted, the `rel_return` from **park date** vs
  from **promotion date**. If most of the move happened pre-promotion, the watchlist was a
  *bottleneck*, logged as a latency penalty; if the move came post-promotion, the park *added* value
  (we avoided a worse entry).

Counterfactual: the "bought-at-park-date" phantom long. This makes the watchlist itself
accountable — it cannot become a graveyard of missed winners without the metric screaming.

### 5.2.7 RISK-OFFICER exit/trim (new)
Every exit/trim is a claim "this position's forward path is now unfavourable." NEXUS freezes a
**hold-counterfactual**: mark the exited position as if it were still held, forward, for the horizon.

- **Good exit** — post-exit `rel_return` of the sold name < 0 (it fell after we left) → drawdown
  avoided, logged in basis points.
- **Premature sell** — post-exit `rel_return` > a tolerance band (e.g. +3% vs SPY) → we left money
  on the table; increments the **premature-sell rate** KPI.

The premature-sell rate is the Risk Officer's signature metric; a high rate means the exit lens is
trigger-happy and the CIO tightens its mandate. Trims grade identically on the trimmed *fraction*.

### 5.2.8 PM-CONVICTION sizing intent (new)
Sizing is graded as a **size-weighted selection** metric: do the names PM-CONVICTION wanted *bigger*
out-return the names it wanted *smaller*? Reuse `predictions.py` size-bucket rank-IC: bucket
candidates by intended size multiplier, compute the cross-sectional IC of size vs forward
`rel_return`. Positive IC = the champion's conviction-sizing carries information; ~0 = sizing is
noise and should defer entirely to the deterministic floor. Counterfactual: equal-weight.

---

## 5.3 The Mechanisms (mapped to real modules)

### 5.3.1 Per-role calibration multipliers — extend `brain/calibration.py`
Today `compute()` returns `{"agents": {"forge": …, "sentinel": …}}`. The extension adds one
`_<role>_reliability(asof)` function per new graded role, each returning the **same**
`_summarize(rows)` shape `(reliability, mean_confidence, multiplier, n, status)`. The
multiplier formula is **unchanged** and shared:

```
multiplier = round(max(FLOOR, min(1.0, reliability / mean_confidence)), 3)     # FLOOR = 0.5
           = 1.0   if  n < MIN_N (=12)  or  mean_confidence falsy  or  reliability None
```

It is **de-confidencing only** — bounded in `[0.5, 1.0]`, never inflates, inert below MIN_N. Each
role reads its own number via `calibration.multiplier("technician")`, `…("gate_officer")`, etc., and
shrinks its stated confidence *before* NEXUS aggregates the quorum. Raw confidence is preserved on
the claim for grading (Invariant LL-3). New `agents` keys: `strategist`, `technician`,
`gate_officer`, `risk_officer`, `pm_conviction`. SCOUT is graded for *idea yield* (KPI only) but does
not carry a confidence multiplier — it proposes, it does not size.

### 5.3.2 Shadow books — policy A/B via `portfolio/shadow_books.py`
The existing five policy books (`prod`, `no_committee`, `no_calibration`, `engine_only`,
`risk_tilt`) become the substrate for **role-ablation A/B**. New counterfactual books, each
re-derived purely from stored decision inputs (no LLM re-invocation, no future price at decision
time — the module's leakage-free guarantee), isolate one *new* seat:

| New shadow book | Policy | Question it answers |
|---|---|---|
| `no_technician` | buy `enter_now` size for every confirmed name, ignore staged/wait | What did entry-timing save or cost? |
| `no_strategist` | confirm names even when the regime call was `no` | Does top-down gating help? |
| `no_gate_officer` | apply every veto'd/downsized name at full size | What did the veto seat net? |
| `desk_full` | the full new desk (all seats, quorum, watchlist) | The headline: does the new desk beat `prod`? |

Each book runs through the existing `apply_policy` → `_rebalance` → `_update_theses` →
`_book_summary` pipeline, graded forward with the same Brier/hit-rate/`vs_spy_pct` and the
holdings-divergence annotation (`extra_vs_prod`, `missing_vs_prod`). The leaderboard *is* the role
attribution: if `no_technician` matches `desk_full`, the Technician earned nothing and the CIO
demotes it. **This is how a skeptic verifies the desk before trusting it: the ablation book must
beat its own removal, forward, leakage-free.**

### 5.3.3 Universe predictions — statistical power via `portfolio/predictions.py`
The owned book resolves a handful of theses per month — far too few to grade six new roles in any
useful timeframe. `predictions.py` already logs + forward-labels the engine's directional opinion on
~1,600 names every build, with date-clustered rank-IC, `_thin_independent` non-overlapping windows,
Newey-West HAC, `effective_n`, and `_MIN_DATES = 8`. The new roles **piggyback** on this universe
log: the STRATEGIST's sector calls, the TECHNICIAN's entry grids, and PM-CONVICTION's size buckets
are logged for *every candidate the desk sees*, not just the few bought. This is the **sample
unlock** — calibration for the new seats reaches MIN_N in weeks because the universe log is the
denominator, while the owned-book P&L stays the honest but slow ground truth.

### 5.3.4 Self-mirror memory injection — the cheapest, highest-ROI lever
**Mechanism.** Before each daily build, each role's prompt is prepended with a compact, factual
digest of **its own** recent graded calls: the last *k* resolved claims (ticker, stance,
stated confidence, realized `rel_return`, HIT/MISS), its rolling hit-rate and Brier, its current
calibration multiplier, and one **mined pattern** ("your `enter_now` calls on extended names — RSI14
> 75 — have missed 7 of 9; your `staged` calls on fresh breakouts hit 11 of 13"). The digest is
produced deterministically by the grader from the role's own claim ledger — no extra LLM call to
generate it, only to *consume* it.

**Why it is the highest-ROI lever.** It costs only the tokens of a short context block, requires
**zero training infrastructure**, and updates **every build** as new outcomes resolve — the feedback
latency is one night, not one fine-tune cycle. It exploits the model's in-context learning: a seat
that sees "you have been overconfident on lottery-pop names" adjusts *this build*, reversibly. It is
the perfect complement to the calibration multiplier — calibration mechanically shrinks the number;
self-mirror changes the *reasoning* that produced it.

**Why it differs from fine-tuning.** Fine-tuning bakes a fixed policy into weights: slow, expensive,
hard to audit, hard to reverse, and prone to catastrophic forgetting; it cannot say *why* it changed.
Self-mirror is **transparent** (the injected digest is human-readable and logged), **reversible**
(drop the block and the behaviour reverts), **per-role** (no cross-contamination), and **falsifiable**
(if mirrored roles don't out-perform un-mirrored ones in a shadow A/B — `mirror_on` vs `mirror_off`
— we turn it off). We adopt fine-tuning only if and when self-mirror's marginal value is exhausted
and the A/B proves a weights-level gain.

**Memory-write guardrails.** A pattern is written into a role's durable self-mirror memory only if it
(a) clears `MIN_N` resolved supporting observations, (b) is **contradiction-checked** against
existing memory (a new claim that reverses an old one requires a higher `effective_n` to overwrite),
and (c) is quality-gated — vague self-praise ("I have good judgment") is rejected; only specific,
falsifiable, outcome-anchored patterns persist. The memory is **append-with-supersede**, never blind
append, so it cannot bloat into self-justifying narrative.

---

## 5.4 The CIO / META-PM Weekly Review

The CIO/META-PM **does not trade**. It runs once weekly (and is the only seat allowed to change other
seats' influence). Its job is to read the whole accountability surface and re-weight the desk —
**within hard guardrails** that stop it overfitting to the last week.

**Cadence.** Weekly, off the daily heartbeat. It never fires mid-week on a single bad day (that would
be chasing n=1).

**What it reads (inputs):**

| Surface | Source |
|---|---|
| Per-role KPIs (hit-rate, Brier, IC, premature-sell rate, idea-yield) | grader KPI rollup |
| Per-role calibration block (n, reliability, multiplier, status) | `calibration.load()` |
| Shadow leaderboard + role-ablation books | `shadow_books.load_leaderboard()` |
| Universe cross-sectional IC + effective-n + HAC t-stat | `predictions.py` summary |
| Brinson-style attribution (allocation / selection / interaction) | attribution rollup (Ch 06) |
| Readiness flags (which metrics are now statistically honest) | `readiness.status()` |

**What it may change (outputs), each logged as its own gradable meta-claim:**

| Lever | Bound |
|---|---|
| **Role influence weight** in the quorum vote (Ch 06) | ±1 step/week; floor > 0 (no seat fully silenced without a multi-week record) |
| **Mandate scope** (e.g. tighten Risk-Officer trigger after high premature-sell) | one mandate change/role/review |
| **Reward / reputation** score | monotone-smoothed (EWMA), no single-week jump > cap |
| **Authority promote/demote** | requires `effective_n ≥ MIN_N` **and** a corroborating shadow-ablation result |

**The note.** Each review writes a human-readable `data/brain/cio/<week>.md` (Ch 07 §7.2.8 canonical) — *"what is working /
who is miscalibrated / what I changed and why, with the n behind it."* The note is itself a claim:
"this re-weight will improve forward `desk_full` vs `prod`." It is graded next month. **The CIO is
accountable to its own grader** — it cannot tune freely; its tunes are scored.

**Anti-overfit guardrails (Invariant LL-4):**
1. **Significance gate** — no behaviour-changing tune unless the supporting metric clears its
   effective-n / MIN_N / `_MIN_DATES` threshold (it reads `readiness.status()` and *must* cite the
   flag that authorises each change).
2. **Bounded step** — every weight is EWMA-smoothed; a single week cannot swing a role from full to
   floor.
3. **No self-promotion** — the CIO cannot increase its *own* authority.
4. **Reversibility** — every tune is a diff with a revert; a tune whose own forward grade is a MISS
   is auto-reverted the following review.
5. **Direction-only on cold metrics** — a metric still in `building` status may *inform the note* but
   may not *move a weight*.

---

## 5.5 Statistical Honesty

This is the section that must convince the skeptic the loop does not fool itself. The single binding
fact: **US Brain beating Flagship on one day is noise** (n=1). The whole loop is engineered so that a
grade changes behaviour **only after it is statistically honest.**

**Cold-start (MIN_N).** Every calibration block is **inert** until `n ≥ MIN_N = 12` resolved
decisions (`calibration.py:34`). Below that the multiplier is hard-pinned to `1.0` and the role's
self-mirror digest is shown for *information* but tagged "pre-evidence — not yet acting." Readiness
records the exact moment each role crosses MIN_N (`readiness.py`, flags `calibration_<role>`), so the
dashboard says *when* a seat's grade went live.

**Shrinkage / Bayesian priors.** New roles start at a neutral prior (multiplier 1.0, reliability =
mean_confidence) and shrink toward realized reliability only as evidence accrues — the ratio
`reliability / mean_confidence` *is* a shrinkage estimator bounded to `[FLOOR, 1.0]`. We never let a
3-sample fluke set a multiplier; the `n < MIN_N → 1.0` rule is the prior dominating until data earns
its way in. Reputation/influence weights use an EWMA prior so early weeks are pulled toward the desk
mean.

**Effective-n / date-clustering.** Raw counts lie: 1,600 predictions from one build share a regime
and are **not** independent. `predictions.py` thins to non-overlapping ~21-bday clusters
(`_thin_independent`), requires `_MIN_DATES = 8` independent clusters, and reports a Newey-West
HAC-adjusted t-stat (`_HAC_LAGS = 2`) so every edge claim carries an autocorrelation-aware
confidence interval. **No edge is "real" on raw n.** The CIO's significance gate (LL-4.1) reads
`effective_n`, never the raw count.

**How noise-chasing is prevented (the explicit guards):**
- **Last-data-point immunity** — multipliers and weights are EWMA-smoothed; one resolved outcome
  cannot move a live weight more than its capped step.
- **Goodhart guard (multi-metric)** — no seat is judged on a single number. A role that games hit-rate
  by only making easy calls is caught by its *Brier* (confidence-weighted) and its *idea-yield /
  selection-IC*; the CIO must look at the *vector*, and a metric improving while a paired metric
  degrades blocks the tune (§5.4 reads ≥3 surfaces per role).
- **Counterfactual-anchored credit** — credit is always vs a stated counterfactual (§5.2), so a seat
  cannot claim a rising-tide win; only *relative* out-performance scores.
- **Raw-vs-shrunk fixed point** (LL-3) — grading raw confidence makes the calibration loop converge
  to a fixed point instead of oscillating.
- **Auto-revert** — any tune whose own forward meta-claim grades MISS is reverted (LL-4.4), so the
  loop cannot ratchet itself into a bad regime.

---

## 5.6 Cross-Book Application

The loop is **book-agnostic by construction**: every mechanism keys on `(book_id, role, ticker,
asof)` and grades vs that book's benchmark. It therefore runs identically over the **US Brain**,
**CN Brain**, **HK Brain**, and **Heavyweight** books, not just Flagship.

**What is shared vs per-book:**

| Element | Scope | Note |
|---|---|---|
| Grader (`outcomes.label_thesis`) | shared | benchmark is per-book: SPY (US), a CN index for CN Brain, an HK index for HK Brain |
| Calibration multipliers | **per (role, book)** | a Technician calibrated on US tape is not assumed calibrated on A-shares; separate `agents` namespaces per book |
| Self-mirror memory | **per (role, book)** | a role's CN track record is injected into its CN prompt only — no cross-market contamination |
| Shadow ablation books | per book | each book gets its own `desk_full` vs `no_<role>` ladder |
| Universe predictions | per book's universe | CN/HK use their own panel; same date-clustering math |
| CIO review | shared seat, **per-book sections** | one weekly note, segmented by book; weights tuned per book |

**Benchmark and horizon overrides.** The only per-book parameters are the benchmark ticker and (if a
market's trading calendar differs) the business-day horizon mapping. The leakage-free anchor logic,
MIN_N, FLOOR, `_MIN_DATES`, EWMA smoothing, and every invariant are **identical across books** — the
honesty guarantees are not negotiable per market.

**Tie-breaks and edge cases (holes patched):**
- **A role with no claims in a book** → multiplier `1.0`, `status: building`, excluded from that
  book's quorum-weight tuning (it cannot be promoted/demoted on zero evidence).
- **A claim whose price history is missing** → stays `UNRESOLVED`, never raises, never counts toward
  n (existing `outcomes` best-effort behaviour).
- **A name that delists / is acquired mid-horizon** → graded on the path up to the last available
  close ≤ `req_end`, barrier = `TIME`; flagged so attribution doesn't credit a phantom.
- **Conflicting grades across loops** (e.g. self-mirror says "good", shadow-ablation says "useless")
  → the **shadow forward-P&L wins** for *authority* decisions (it is the hardest leakage-free
  evidence); self-mirror only ever changes the *prompt*, never the weight, so the conflict is
  resolved by construction.
- **Cross-book CIO clash** (a role great in US, bad in CN) → weights are per-book; the CIO never
  averages across markets to set a single weight.
- **Cold-start everywhere (today, pre-2026-07-17)** → all multipliers `1.0`, all self-mirror digests
  tagged "pre-evidence"; the desk runs on raw judgment + deterministic floors until the first
  resolution window matures. The loop is *built* now so it is *armed* the day data lands.

---

### Closing invariant summary (this chapter's contract)

- **LL-1** — everything gradable: no action without a falsifiable, horizon-bound, success-conditioned
  claim.
- **LL-2** — leakage-free: graded only on prices the seat could have seen, vs benchmark.
- **LL-3** — grade the *raw* confidence; shrink only at decision time; fixed-point convergence.
- **LL-4** — the CIO may change behaviour only past the significance gate, in bounded reversible
  steps, never self-promoting, auto-reverting failed tunes.
- **Counterfactual-or-no-credit** — every grade is relative to a stated alternative world.
- **Cold-start inertia** — `MIN_N = 12`, `_MIN_DATES = 8`, `FLOOR = 0.5`: noise cannot move weights.

The desk is permitted to act autonomously precisely because it is permitted nothing it cannot later
be graded on. This loop is how Mastermind *learns without fooling itself* — and the proof obligation
is concrete: the `desk_full` shadow book must beat `prod`, forward and leakage-free, before the new
desk is trusted with the live book. See *Chapter 07 — Data Contracts, Module Mapping & Phased Build
Plan* for the module-by-module artifact map and the 2026-07-17 first-resolution anchoring.
