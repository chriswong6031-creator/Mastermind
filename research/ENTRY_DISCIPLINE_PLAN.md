# ENTRY DISCIPLINE PLAN — buy-timing audit + fix program

**Date:** 2026-07-03 · **Author:** entry-discipline session (Opus, Fable-supervised)
**Status:** independent audit + plan. NOT yet reviewed by Fable. Every proposed gate ships ADVISORY first.
**Companion docs (same dir):** [MASTERMIND_FIX_MASTERPLAN.md](MASTERMIND_FIX_MASTERPLAN.md) (the canonical 74-item program — this plan is a *dependent* of it, not a replacement) · [MASTERMIND_PROBLEM_AUDIT_FOR_FABLE.md](MASTERMIND_PROBLEM_AUDIT_FOR_FABLE.md) · [MASTERMIND_V2_ARCHITECTURE.md](MASTERMIND_V2_ARCHITECTURE.md)

**Hard invariant this plan inherits (charter P2, MAINTENANCE.md §0):** missing/stale/wrong data may
coarsen identity, freeze the book, or shrink size — it may NEVER un-cap, raise authority, or flip
direction. Every mechanic below is **subtract-only** and **fail-open** (a missing signal withholds
nothing and vetoes nothing).

---

## 0. The complaint, and the honest correction (read this first)

> User's words: *"It doesn't assess whether to use confluence gating or other technicals, it just
> randomly buys, and usually it buys at the highest point and it will lose money chasing in — it's
> not disciplined at all."*

**The literal claim is partly false, and saying so is the point.** The bot is NOT ungated and does
NOT buy randomly:

- A **multi-sided confluence gate** decides every buy (`portfolio/lenses.py:960` `synthesize()`):
  `size_authority=='up'` requires `confluence>0.3 AND leadership_ok AND not price_downtrend AND not
  price_falling_fast AND not weak_asymmetry` (lenses.py:1062). It has a **parabolic hard veto**
  (lenses.py:880), a **falling-knife veto** (lenses.py:373, don't buy INTO a 4-day −9%/10-day −14%
  collapse), a **downtrend veto** (don't buy a rolling-over name), and a **graded 200dma-extension
  brake** (conviction.py:138 — ≥30% vs 200dma → initial size only; ≥45% → no add).
- An **entry-timing gate is already ON by default** (`bot/phase2.py:632`, `MASTERMIND_TIMING_GATE`
  default `"1"` at phase2.py:150). It WITHHOLDS a would-be buy whose entry technicals are poor and
  **parks it on a patience queue** for daily re-review (`portfolio/watchlist.py`, a WATCH→ARMED→EXPIRED
  state machine) instead of chasing it.

**So the "patience queue" and "chase brake" my mandate asked me to design already exist.** Proposing
them again would be duplication — exactly what the masterplan forbids ("Do NOT build … stop
re-proposing these", MASTERPLAN §1).

**But the mechanism the user *describes* — buying at local highs and chasing in — survives, for three
specific reasons the existing gates do not close.** This is where the real work is:

1. **The trend lens treats proximity-to-52w-high as BUY CONFIRMATION, not as extension risk.**
   `_trend_row` marks a name `bull` ("price confirmed, uptrend intact") only when `off_52w_high_pct >
   -12` — i.e. *within 12% of the 52-week high* (lenses.py:377). A name pinned at its high with MACD+
   and healthy RSI is the *strongest* buy the gate can see. The gate's entire notion of "good entry"
   is "near the top and going up." That is the buy-high mechanism, encoded.
2. **The only chase-brake is the slow 200dma-distance number.** Both the extension lens
   (lenses.py:584) and the L3 timing gate (watchlist.py:61) gate on `pct_vs_200dma >= 30`. A name can
   be +25% above its 200dma, sitting at the very top of its *own recent range*, having gone vertical
   over 10 sessions, and clear every brake — because none of them measures distance from the *recent*
   range or the *short-term* moving average. The 200dma is a trailing, slow anchor; the chase happens
   on the fast timescale it cannot see.
3. **The dashboard's VALIDATED entry gate is never consulted.** The Macro Dashboard side has a
   validated per-stock entry gate — **MACD-2D × StochRSI-3D confluence** — and per-sector clean-entry
   / heat state. The bot reads ~20 fields from the regime file and the standout board's *coarse*
   `entry_signal` (stop/buy_zone/entry_grade) as **provenance only** (`brain/intake.py:129`,
   comment: *"these do NOT size or gate the name"*). The one signal the dashboard proved works for
   entry timing is on the shelf, unused.

And one true structural gap the user's word "randomly" points at correctly:

4. **Buys are all-at-once. There is no staged entry.** `paper_account.rebalance()` fills straight to
   `target_weight × NAV` in a single shot (paper_account.py:451-490). An 8% name goes to 8%
   immediately, at whatever price the tape is at that build, with no averaging over pullbacks. Nothing
   in the pipeline tranches.

**This plan's thesis:** the discipline scaffold exists (gate, brakes, patience queue, grading ledger);
what's missing is (a) a *fast-timescale* chase-guard, (b) consuming the dashboard's validated entry
signal instead of the coarse published grade, (c) removing the "near-high == good" bias, and (d) an
optional staged-entry ladder. All four are additive, subtract-only, and gradeable on the existing
outcome ledger before they bind.

---

## 1. Findings (evidence-cited, severity-graded)

Severity: **CRIT** = directly produces the buy-high behavior; **HIGH** = materially enables it;
**MED** = related discipline gap; **INFO** = context that constrains the fix.

| # | Sev | Finding | Evidence (file:line) |
|---|-----|---------|----------------------|
| F1 | **CRIT** | **Near-52w-high is treated as buy confirmation.** `uptrend`(=bull) requires `offhi > -12` (within 12% of the high); a name at its high with MACD+ reads "price confirmed." The gate's best entry IS the top. | `portfolio/lenses.py:376-378`, note string 400 |
| F2 | **CRIT** | **No fast-timescale chase-guard.** Every extension/timing brake keys on `pct_vs_200dma>=30` only. A name vertical over 10 sessions at the top of its 60-day range but <30% over its 200dma clears all brakes. | `portfolio/lenses.py:584-585`; `portfolio/conviction.py:158-165`; `portfolio/watchlist.py:61-62` |
| F3 | **HIGH** | **Dashboard's validated MTF entry gate (MACD-2D × StochRSI-3D) is not consumed.** Bot uses the coarse published `entry_signal.{stop,buy_zone,entry_grade,urgency}` and `conviction.ext.grade` only, and even those flow in as *provenance, not a gate*. | `brain/intake.py:126-135` (comment "do NOT size or gate"); `bot/phase2.py:114-138` `_entry_tech_fields`; `portfolio/watchlist.py:49-72` |
| F4 | **HIGH** | **Buys are all-at-once; no staged entry / tranche exists anywhere.** Approved weight is filled to target in one rebalance. No scale-in on pullback. (grep for tranche/scale_in/ladder across brain+portfolio+bot = 0 hits in the buy path.) | `portfolio/paper_account.py:451-490` |
| F5 | **MED** | **Extension is one diluted vote among ~10, then de-correlated.** The extension bear vote sits outside both correlated blocs but is a single vote; a hot leader with bull trend+sector_rs+narrative+flows clears `confluence>0.3` easily despite it. Extension only *reduces size* (via `_ext_mult`), it does not *block* a moderately-extended entry. | `portfolio/lenses.py:995-1002, 1062`; `portfolio/conviction.py:449-456` |
| F6 | **MED** | **The L3 timing withhold's RS input is a single median cut (`rs<50`), not a rotation/heat read.** It cannot see that a name's *sector* is cooling/broken — only the name's own RS pctile. A name leading a rolling-over sector passes. | `portfolio/watchlist.py:65-67`; `bot/phase2.py:131` (`momentum.alpha.rs`) |
| F7 | **INFO** | **The grading substrate for validating any new gate already exists.** Outcome ledger (`resolve/lens_edge/lens_weights`), shadow books, and an existing timing A/B arm (`desk_ab.apply_timing_gated`, `_timing_ok`) all accrue forward outcomes with leakage-free `asof` semantics. Any new gate can ship advisory and be kill-tested here. | `brain/outcome_ledger.py:103,236,257`; `portfolio/desk_ab.py:147,187,292,326`; `portfolio/shadow_books.py` |
| F8 | **INFO** | **The publish path for the dashboard's new `sector_pulse.json` already exists in the vendored read tree.** `vendor/macro/site/basketdata/` holds `etf_pulse.json`, `baskets.json`, etc.; the bot already reads `site/basketdata/*.json`. `sector_pulse.json` will be a drop-in sibling. | `ls vendor/macro/site/basketdata/`; `portfolio/lenses.py:24` (`_load`), `brain/intake.py:40-49` |

**Not-a-finding (checked and cleared):** the confluence gate itself is sound and the fail-closed /
freeze semantics from W0 are correct — a data outage cannot mint a buy (lenses.py:1006-1061). The
problem is not *whether* it gates; it is *what "good entry" means* inside the gate.

---

## 2. Root-cause synthesis — WHY buys cluster at local highs (mechanism, not vibes)

Three mechanisms compound:

**M1 — The gate's definition of a confirmed entry is literally "near the high."** Because
`_trend_row` only says `bull` when the name is within 12% of its 52w high (F1), momentum-ranked
candidate feeds (`_basket_top_picks` ranks by 20d return, conviction.py:177; `regime_seed` ranks by
20d rel-perf) surface names that *just ran*, and the trend lens then *rewards* them for having run.
Selection sorts to the top of the range; confirmation blesses the top of the range. The two stages
point the same direction — up, and late.

**M2 — Every brake is slow, so the fast chase is invisible.** The 200dma-distance brake (F2) is the
only quantitative extension control. A parabolic 10-day move only trips it once price is >30% over a
200-day average — by which point the move is largely over. The falling-knife veto (lenses.py:373)
catches sharp *drops* but there is no symmetric "sharp *rip*" guard. So the specific bad entry — "name
went vertical this week, buy it now" — falls exactly in the blind spot between "not a knife" and "not
yet 30% over the 200dma."

**M3 — All-at-once sizing removes the averaging that would forgive a bad entry.** With no tranching
(F4), the book takes its full 8% at one tick. If that tick is the local high, the entire position is
underwater immediately and the exit stops (D5 dead-capital, hysteresis) then fight to hold it. Staged
entry would convert "bought the high" from a full-size mistake into a 1/3-size probe with dry powder
to average — the doctrine's own A5 ("sizing & rotation capacity dominate selection") applied to
*entry* rather than book construction.

**The through-line:** the system was built to answer *"is this a real leader?"* (confluence) and
*"is it broken?"* (downtrend/knife/parabolic). It was never built to answer *"is NOW a good price to
start?"* on the timescale a chase happens. The dashboard already answers that question well
(MACD-2D × StochRSI-3D) — the fix is mostly *wiring an existing answer in*, plus one new fast-extension
number, plus optional tranching.

---

## 3. Proposed architecture — the Entry Quality Officer (EQO)

A single new advisory stage, **between conviction/research-confirm and execution**, that stamps an
`entry_quality` verdict onto each would-be buy. It sits exactly where the L3 timing gate already sits
(`bot/phase2.py:624-659`) and generalizes it from "one coarse withhold" to "a graded entry-quality
read with a pluggable evidence stack."

```
candidates → conviction.build (confluence gate + 200dma brake)   [unchanged]
           → research gate (Conviction Index)                    [unchanged]
           → committee (SENTINEL bear case)                       [unchanged]
           → ┌───────────────────────────────────────────────┐
             │  ENTRY QUALITY OFFICER  (new, advisory-first)   │
             │   inputs (all fail-open):                       │
             │    • sector_pulse.json  → sector heat/clean_entry│
             │    • dashboard MTF gate → MACD-2D × StochRSI-3D  │
             │    • fast-extension     → pct vs 20d MA, range%  │
             │    • existing L3 fields → pct_vs_200dma, rs, eq  │
             │   output: entry_quality ∈ {clean, extended,      │
             │           chase, cooling_sector, wait}           │
             │           + advisory note stamped on the record  │
             │   binding (only after grading passes, per §5):   │
             │    • chase/wait → withhold → patience queue      │
             │    • extended   → staged-entry ladder (½/¼ probe) │
             └───────────────────────────────────────────────┘
           → paper_account.rebalance / staged fills             [staging is new]
```

### 3.1 Consuming `sector_pulse.json` (the new shared data product)

Schema (as provided): `{schema:1, as_of, region, themes:[{id, label, reco, score, rank,
rank_delta_5d, heat: heating|hot|cooling|broken|idle, clean_entry{flag,quality}, momentum_score,
long_sign}], heating:[], cooling:[]}`.

- **Read path:** a new `portfolio/sector_pulse.py` loader mirroring `lenses._load` — reads
  `vendor/macro/site/basketdata/sector_pulse.json`, `{}`-on-miss, `schema==1` guarded, `as_of`
  staleness-checked against the build date (reuse the W0 staleness anchors pattern). **Fail-open:**
  absent file → EQO contributes nothing (no withhold, no veto). This honors P2: a missing pulse can
  only *remove* a brake.
- **Name→theme resolution:** the bot already maps a name to its theme via
  `baskets_membership.slug` → `allocation.json ranks[].id` (lenses.py:702-713). `sector_pulse.themes[].id`
  should key on the same slug so the join is direct; if it keys on GICS sector, fall back through the
  existing `_SECTOR_ETF` map (lenses.py:411).
- **Use (advisory → binding):**
  - `heat ∈ {cooling, broken}` for a NEW buy → EQO note `cooling_sector`; when armed, **withhold
    unless the committee writes an explicit contrarian thesis** (the "sector-tailwind gate" below).
  - `clean_entry.flag==False` → EQO note `not_clean`; contributes to the chase read.
  - `heat==heating` + `clean_entry.flag==True` → the *positive* case: this is the entry the doctrine
    wants (Stage-1 ignition, clean entry — DOCTRINE.md lifecycle row "1 ignition").

### 3.2 Consuming the dashboard's validated MTF gate

The dashboard computes MACD-2D × StochRSI-3D confluence per stock (validated entry gate on the
dashboard side). Two ingestion options, in preference order:

1. **If the dashboard publishes the MTF confluence flag per name** (check
   `site/stockdata/{t}.json` for an `entry_signal.mtf_confluence` / `tech.macd_2d` / `stochrsi_3d`
   field, or a per-name field in `sector_pulse` / a new `entry_gate.json`): read it directly. This is
   the cheapest and the most faithful to the validated signal. **This is the preferred path** — argue
   for the dashboard to publish it if it does not yet (a wishlist handoff, per MASTERPLAN §4).
2. **If not published:** the bot can approximate MACD-2D × StochRSI-3D from the price series it already
   loads (`lenses._closes(ticker)`, the breadth parquet + yahoo store). This is a *reconstruction* and
   must be tagged `(unverified)` vs the dashboard's canonical computation, and validated against it
   before it can bind. Prefer option 1; only build option 2 if the publish handoff stalls >1 week.

### 3.3 The fast-extension number (the missing chase-guard input)

A new advisory field computed from the existing price series (`lenses._closes`):
- `pct_vs_20dma` — distance above the 20-day MA (the fast analog of the 200dma brake).
- `range_pctile_60d` — where today's close sits in its trailing 60-day high-low range (0=low, 100=high).
- `ret_10d` — 10-session return (the symmetric partner of the falling-knife `ret_5d/ret_10d`).

`chase` fires when the name is **at the top of its own recent range AND just ripped**: e.g.
`range_pctile_60d >= 90 AND (pct_vs_20dma >= X OR ret_10d >= Y)`. Thresholds start as
`(unverified-prior)` and are tuned on the ledger (§5). This is the number that closes the M2 blind spot.

---

## 4. Novel mechanics — argued for and against, honestly

### 4.1 Chase-guard (fast-extension percentile veto) — **BUILD (advisory→bind)**
*For:* directly closes M2/F2, the single most load-bearing gap. Uses only price series the bot already
loads. Symmetric with the existing falling-knife veto (which the repo already trusts) — this is just
its up-side twin. Cheap, subtract-only, fail-open.
*Against:* momentum genuinely persists; a hard chase-veto could forfeit real leadership entries (the
walk-forward that refuted the *cycle* veto, MASTERPLAN resolution #2, is a standing warning that
"topping leaders keep leading"). **Mitigation:** the chase-guard is an ENTRY-TIMING withhold that PARKS
the name on the existing patience queue (it does not drop it or exit a held name), so a persistent
leader is re-reviewed daily and bought on the next clean entry — it delays, it does not forfeit. And it
ships advisory: it must beat "buy anyway" on the ledger before it withholds.
*Verdict:* **strongest single mechanic. Build first, advisory.**

### 4.2 Staged-entry ladder (1/3 tranches on pullbacks) — **BUILD, but scoped**
*For:* closes M3/F4. Converts a bad entry from a full-size loss into a probe. Aligns with doctrine A5
(sizing dominates) and A3 (survive being early). Especially valuable for `extended` names the desk
wants but shouldn't chase full-size.
*Against:* real complexity — it requires the paper account to hold a *target ladder* (remaining
tranches + trigger prices), the settle job to fire tranches on subsequent builds, and the marking/NAV
math to handle partially-filled targets. This touches the sizing spine (Fable sign-off territory,
MAINTENANCE.md §2). It also risks never completing the ladder if the pullback never comes (opportunity
cost).
*Verdict:* **build, but as its own wave (W3) with a deterministic, minimal ladder** (probe now at
1/3–1/2, remainder queued at defined pullback levels with a TTL; if the TTL expires unfilled, the probe
is the position — no forced completion). Ships behind a flag, default OFF, shadow-graded first.

### 4.3 Sector-tailwind gate (block buys in cooling/broken sectors) — **BUILD (advisory→bind), with a written-thesis escape**
*For:* closes F6; this is exactly what `sector_pulse.heat` is for. The 2026-07-02 incident was the book
sitting offensive as semis broke — a cooling/broken-sector gate is a direct antibody.
*Against:* (a) the cycle-*veto* was walk-forward-REFUTED (MASTERPLAN resolution #2) — a sector-state
gate must be scoped so it is NOT that refuted veto. The distinction: the refuted veto blocked *held
leaders in topping sectors*; this gate blocks only *NEW entries into cooling/broken sectors*, never
touches a held name, and offers a written-contrarian-thesis escape. That is the "entry-tilt only"
carve-out the walk-forward left open. (b) `sector_pulse` is brand-new and unvalidated in this loop.
*Verdict:* **build as advisory only until `sector_pulse` earns forward grades; the committee's
written contrarian thesis is a required escape** so the gate informs rather than dictates. Never a
held-name exit.

### 4.4 Patience queue — **ALREADY EXISTS; extend, do not rebuild**
`portfolio/watchlist.py` is the patience queue (WATCH→ARMED→EXPIRED, TTL 20/10 td, MAX_WATCH 40,
daily re-review via `review(asof, still_withheld=…)`). **Do not rebuild it.** The EQO's chase/wait/
cooling verdicts should feed *this* queue's `still_withheld` predicate so the existing re-review state
machine ages and promotes them. The only extension needed: the predicate reads EQO's new fields, not
just the coarse `timing_withhold` set.

### 4.5 Better idea — **"clean-entry trigger" promotion, not just time decay**
The current patience queue promotes a parked name when its *withhold reason clears* (watchlist.py:219)
or expires on TTL. That is passive. A stronger mechanic: promote a parked name the moment the
dashboard's MTF gate flips to a **clean entry** (MACD-2D × StochRSI-3D confluence turns positive) OR
`sector_pulse.clean_entry.flag` flips True for its theme. This turns the queue from "wait N days and
retry the same coarse check" into "wait for the validated entry trigger" — the exact discipline the
user wants ("wait for a clean entry instead of chasing"). It reuses the queue's existing promotion
plumbing (`promote_candidates`), just with a sharper `still_withheld` predicate. **Recommended; it is
the highest-value/lowest-risk novel piece after the chase-guard.**

### 4.6 Rejected: hard per-name entry veto / re-weighting survivors
A hard entry veto that *drops* names (rather than parking them) re-introduces the forfeit risk the
walk-forward warned about, and re-weighting survivors violates the judgment-layer's deterministic
authority clamp (phase2.py:745, "forbids re-weighting a survivor"). **Do not build.** Withhold-and-park
is the correct primitive; it is already there.

---

## 5. Validation protocol — kill-test every mechanic before it binds

**Principle (charter P3, and the whole point of the repo's learning substrate): every new gate ships
ADVISORY — logged and graded — and may only bind after it beats the null on forward outcomes. No gate
self-promotes; promotion is a Fable decision.**

The infrastructure to do this already exists and must be reused, not rebuilt:

1. **Shadow A/B arm.** Add each EQO mechanic as a policy in `portfolio/desk_ab.py` alongside the
   existing `apply_timing_gated` arm (desk_ab.py:187). The A book buys as today; the B book applies the
   mechanic. `desk_ab.run(asof, …)` (desk_ab.py:326) already marks both forward with leakage-free
   `asof` semantics and `grade_forward` (desk_ab.py:292). **Acceptance = the B arm's forward
   realized-return distribution beats A's over ≥40 graded entries** (same floor the shadow trim ladder
   uses, improvement_agenda.py:575).
2. **Outcome ledger grading.** Every EQO verdict is logged as a falsifiable record and resolved via
   `brain/outcome_ledger.resolve()` (outcome_ledger.py:103) at the 21-day horizon, so `lens_edge`
   (outcome_ledger.py:236) can report whether `entry_quality=='chase'` names that were *bought anyway*
   actually underperformed `clean` names — the direct empirical test of the whole thesis.
3. **The counterfactual that proves the mechanism.** Log, for every NEW buy, the EQO verdict AND the
   forward drawdown-to-entry. If the thesis is right, `chase`-verdict buys should show materially worse
   entry drawdown than `clean` buys. **If they do not, the chase-guard is refuted — flatten it to
   display-only** (this is a pre-committed kill criterion, matching MASTERPLAN §W7's ablation style).
4. **Replay battery.** Add the 2026-07-02 semis-breakdown window as a fixture: with `sector_pulse`
   showing semis `broken`, the sector-tailwind gate must WITHHOLD new semis entries that build. This
   becomes a permanent CI replay (tests/incident_replays/) once the gate arms — the same pattern W-I used.
5. **Calm-tape invariance.** On a benign tape with `heat==hot`+`clean_entry==True`, the EQO must be a
   no-op (byte-identical book). A gate that changes the calm-tape book is over-firing — ship-blocker,
   mirroring the W2 calm-tape invariance test.

**Pre-committed kill criteria (per mechanic):**
- Chase-guard: if `chase`-buys' forward entry-drawdown ≤ `clean`-buys' over ≥40 graded → demote to display.
- Sector-tailwind gate: if `broken`-sector entries do NOT underperform over ≥40 graded → advisory-only forever.
- Staged ladder: if the laddered book's Sharpe over ≥8 weeks ≤ the all-at-once book's → keep flag OFF.

---

## 6. Phased rollout W0–W3

Every wave: full pytest (in an **isolated worktree**, never the prod checkout — it wipes bot.db,
MAINTENANCE.md §7), adversarial diff review, advisory-first, squash-merge same-day, masterplan status
log entry. Model-tier: Sonnet for well-specified code, Opus for threshold/design calls, Fable sign-off
for anything touching the sizing spine (the staged ladder in W3).

### W0 — Advisory annotation only (SHIPPED in this session — the minimal safe slice)
- `portfolio/entry_quality.py`: given a candidate ticker, fetch available context (pct vs 20d/60d MA,
  60d range percentile, distance from 52w high, `pct_vs_200dma`, and — when present — `sector_pulse`
  heat/clean_entry) and return a structured advisory `entry_quality` note. **Pure, read-only,
  fail-open, changes no decision.** Tests included.
- Phase2 stamps this note onto the shadow-input / decision record (advisory field only) so it starts
  accruing forward-gradeable data **from day one**, with zero behavior change.
- **Acceptance:** live build produces `entry_quality` notes on every candidate; the book is
  byte-identical (no weight changes); tests green. *(This wave's scope is the annotation + its test —
  see §7.)*

### W1 — Wire `sector_pulse.json` + dashboard MTF gate as EQO inputs (advisory)
- `portfolio/sector_pulse.py` loader (fail-open, schema-guarded, staleness-anchored).
- EQO reads `sector_pulse.heat` / `clean_entry` and the dashboard MTF confluence flag (option 1 of
  §3.2; open a wishlist handoff if the flag isn't published).
- Still advisory: notes only, no withhold. Forward grades begin accruing via the ledger + a `desk_ab`
  shadow arm.
- **Acceptance:** EQO note includes sector heat + MTF state on names where the data exists; degrades to
  W0's price-only note where it doesn't; calm-tape invariance test green; a `desk_ab` arm is recording.

### W2 — Arm the chase-guard + clean-entry-trigger promotion (bind, gated on §5 grades)
- **Precondition:** ≥40 graded entries show `chase`-verdict buys underperform `clean` on entry drawdown
  (§5.3), AND Fable sign-off (P3 promotion of a shadow signal to a sizing/gating input).
- Chase-guard feeds the existing L3 withhold → patience queue (extend `watchlist.timing_withhold`'s
  predicate, do not rebuild the queue). Clean-entry-trigger promotion sharpens the queue's
  `still_withheld` predicate (§4.5).
- Sector-tailwind gate arms as a withhold **only** for `broken` sectors, with the committee's
  written-contrarian-thesis escape; `cooling` stays advisory pending more grades.
- **Acceptance:** 2026-07-02 replay withholds new semis entries; calm-tape invariance holds; the
  pre-committed kill criteria are wired as CI ablations.

### W3 — Staged-entry ladder (flag OFF, shadow-first; Fable sign-off — sizing spine)
- Deterministic minimal ladder: probe at 1/3–1/2 now, remainder queued at defined pullback triggers
  with a TTL; unfilled remainder expires (the probe becomes the position — never force-completed).
- Requires paper_account target-ladder state + settle-job tranche firing + partial-target NAV math.
  This is the only wave that touches the sizing spine → **Fable sign-off before any live arm.**
- Shadow-graded ≥8 weeks vs the all-at-once book before the flag can flip on.
- **Acceptance:** laddered shadow book's entry-drawdown and Sharpe beat all-at-once over the window, or
  the flag stays OFF (pre-committed kill).

---

## 7. What this session implemented (the minimal safe slice — W0)

Per the mandate's "implement ONLY the minimal safe slice, if cleanly additive": **`portfolio/entry_quality.py`**
— a pure, read-only advisory annotator + its tests. It:
- takes a candidate ticker, reads the price series the bot already loads (`lenses._closes`) and the
  published `pct_vs_200dma` / `off_52w_high_pct`, and (when present) `sector_pulse.json`;
- returns `{verdict, notes, metrics, sources}` where `verdict ∈ {clean, extended, chase,
  cooling_sector, wait, unknown}` — **advisory; it sizes nothing and gates nothing**;
- fails open on every missing input (no data → `verdict='unknown'`, empty notes);
- is stamped onto the phase2 decision/shadow record as an advisory `entry_quality` field, changing no
  weight, no gate, no fill.

This is deliberately the smallest change that starts the forward-grading clock (§5) without any risk to
the live book. Everything that *binds* (W2/W3) is explicitly gated behind forward grades + Fable
sign-off, per charter P3/P8.

---

## 8. Alignment with the existing masterplan (what it covers vs what this adds)

**Already covered by MASTERMIND_FIX_MASTERPLAN.md — this plan does NOT duplicate:**
- The confluence gate, parabolic/Altman/cycle hard vetoes, falling-knife veto, graded 200dma-extension
  brake (W2).
- The L3 timing gate + patience queue (W2, armed default-ON).
- Fail-closed/freeze data semantics, the circuit breaker (W0).
- Cluster/firm caps, the budget equation, defensive rotation, the additive judgment mind (W2/W3/W4).
- The perception-era read-in of the regime file's MTF sub-planes (W-E) — *note:* W-E reads the regime
  file's `mtf_signals` for perception/wake; this plan proposes consuming the per-name MACD-2D ×
  StochRSI-3D **entry gate** specifically as a *sizing/timing* input, which W-E's advisory perception
  layer does not do.

**What this plan ADDS (the delta):**
1. The finding that the gate's "good entry" == "near the 52w high" (F1) — a *definition* bug, not a
   missing gate. Not in the register.
2. The **fast-timescale chase-guard** (pct vs 20d MA / 60d range percentile / 10d rip) — the register's
   extension controls are all 200dma-slow (F2).
3. Consuming the dashboard's **validated MTF entry gate** and the new **`sector_pulse.json`** as
   *entry-timing* inputs (F3/F8) — the masterplan's wishlist §4 lists cluster-map/breadth handoffs but
   not the per-name MTF entry gate or sector_pulse as a sizing input.
4. The **staged-entry ladder** (F4) — no tranching mechanic exists or is planned anywhere in the repo.
5. The **clean-entry-trigger promotion** upgrade to the existing patience queue (§4.5).

This plan should be adopted as a **sub-program dependent on the masterplan's W2 brakes** (its
precondition is that the confluence gate + patience queue exist, which they do). Recommend adding it to
the masterplan status log as "Entry-Discipline sub-program W0 shipped (advisory annotator); W1–W3
planned, all advisory-first / spine-changes Fable-gated."

---

## 9. The four self-interrogation questions (MAINTENANCE.md §8)

1. **Autonomous enough?** Not for entry timing — it buys at the top on the fast timescale with no
   antibody. W0 starts measuring it; W2 gives it one.
2. **Would it repeat the last mistake?** The 2026-07-02 semis chase: partially. The 200dma brake and
   the (default-ON) L3 timing gate would trim some, but a fast vertical entry into a not-yet-broken
   semis name still clears. The W2 chase-guard + sector-tailwind gate + the replay fixture close it.
3. **Visibility → sizing?** The dashboard's *best* entry signal (MTF confluence) and the sector heat
   product reach the bot as provenance but **not** as a sizing/timing input. This plan is the wire that
   makes perception reach entry sizing.
4. **Highest-leverage next enrichment?** The fast-timescale chase-guard (§4.1) — one number the bot can
   compute today, closing the single most load-bearing gap. Scheduled as W2, measured starting W0.
