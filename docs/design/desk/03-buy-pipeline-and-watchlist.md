# Chapter 03 — The Buy Pipeline & Watchlist Subsystem

> **Scope.** This chapter specifies the multi-layer BUY funnel for the Flagship alpha book and the WATCHLIST subsystem that backstops it. It is the operational core of the design: the mechanism that converts "score-passes → auto-buy" (diagnosed in *Chapter 01 — Executive Summary, Design Philosophy & Diagnosis*, with the current-state mechanics cited in §1.4 / the current-state baseline) into a **right-name-AND-right-time funnel** governed by separation of powers and subtract-only safety. Role profiles are defined in *Chapter 02 — The Desk: Organizational Structure & Decision Rights* (the roster in the Canonical Brief §D); the buy-side verdicts and the entry decision flow are specified here in this chapter; final-authority wiring (`committee.py`/`nexus()` extension) lives in *Chapter 07 — Data Contracts, Module Mapping & Phased Build Plan*; grading of every artifact here lives in *Chapter 05 — The Accountability & Learning Loop*; the module map lives in *Chapter 07 — Data Contracts, Module Mapping & Phased Build Plan*.

The current pipeline (`bot/phase2.py` → `portfolio/conviction.py:build()` → `paper_account.rebalance()`) treats the two gates — engine confluence and Conviction Index ≥ 60 — as a buy authorization. This chapter replaces that single decision point with a **seven-stage state machine** in which the Conviction Index is *necessary but not sufficient*, and in which any stage may divert a name to WATCHLIST or REJECT without ever forcing a buy.

---

## 3.1 The Idea State Machine

Every candidate is a record that walks a fixed set of states. The pipeline runs nightly inside the regime-level build window opened by `brain/gate.py:should_run` (see §1.4 / the current-state baseline). State is persisted per ticker in a new `data/pipeline/ledger.jsonl` so that a name's history (every gate verdict, every day) is reconstructable for grading.

### 3.1.1 States

| State | Meaning | Owning seat |
|---|---|---|
| `UNIVERSE` | In the feed (`_us_standouts` TOP_US=100 + TOP_BASKET=100, Leadership universe) but not yet examined tonight. | NEXUS (clerk) |
| `SOURCED` | SCOUT surfaced it with a one-line thesis + why-now hint. | SCOUT |
| `CANDIDATE` | Passed engine+research candidacy: `combined ≥ 60`, `viability ≠ "avoid"`, `recommend == True`. **Necessary, not sufficient.** | ANALYST/FORGE + engine |
| `MACRO_OK` / `MACRO_HOLD` | Strategist judged the regime/sector backdrop supportive-now (`OK`) or unsupportive (`HOLD`). | MACRO STRATEGIST |
| `TECH_NOW` / `TECH_STAGE` / `TECH_WAIT` | Technician's entry-timing verdict for *this chart right now*. | TECHNICIAN |
| `ADVERSARY_PASS` / `ADVERSARY_FLAG` | Survived the broadened SENTINEL bear case, or was flagged REJECT/WITHHOLD. | ADVERSARY/SENTINEL |
| `CHAMPIONED` | PM-CONVICTION proposed it with a target sizing intent. | PM-CONVICTION |
| `APPROVED` / `VETOED` / `WITHHELD` | Gate-Officer final ruling. | PM-GATE OFFICER |
| `STAGED` / `BOUGHT` | Sizing resolved by the engine; order queued/filled by `paper_account.rebalance()`. | NEXUS |
| `WATCH` | First-class parked state (good-but-not-now). Re-reviewed daily / at catalyst. | NEXUS + re-review |
| `REJECT` | Hard-failed candidacy or a hard veto; out of the funnel for a cooldown. | any seat (subtract-only) |
| `EXPIRED` | Aged out of WATCH without promotion. | NEXUS |

### 3.1.2 Flow (ASCII)

```
            ┌─────────────┐
            │  UNIVERSE   │  feed: us_standouts(100) + baskets(100) + Leadership
            └──────┬──────┘
                   │ SCOUT sources (thesis + why-now)
            ┌──────▼──────┐
            │   SOURCED   │
            └──────┬──────┘
   FAIL candidacy  │  combined≥60 & viability≠avoid & recommend  ── PASS ──┐
   ┌───────────────┤                                                       │
   │               │  (hard: viability=="avoid" OR recommend==False)       │
   ▼               │            └──────────────► REJECT (cooldown)         │
 REJECT            ▼                                                       ▼
            ┌─────────────┐                                        ┌─────────────┐
            │ CANDIDATE   │◄───────────────────────────────────────│   (gate 1)  │
            └──────┬──────┘                                        └─────────────┘
                   │ MACRO STRATEGIST: backdrop supportive NOW?
        HOLD ──────┤
   (regime/sector  │ OK
    unsupportive)  ▼
        │   ┌─────────────┐
        │   │  MACRO_OK   │
        │   └──────┬──────┘
        │          │ TECHNICIAN: enter-now / staged / wait?
        │   WAIT ──┤
        │  (broken │ NOW or STAGE
        │   setup) ▼
        │   ┌──────────────┐
        │   │ TECH_NOW/    │
        │   │ TECH_STAGE   │
        │   └──────┬───────┘
        │          │ ADVERSARY/SENTINEL: thesis-breaker?
        │  REJECT/ │
        │  WITHHOLD│ PASS
        │   (bear  ▼
        │   wins)  ┌─────────────┐
        ▼          │ ADVERSARY_  │
   ┌─────────┐     │   PASS      │
   │  WATCH  │◄────┴──────┬──────┘
   │(parked, │            │ PM-CONVICTION champions (sizing intent)
   │ re-rev  │     ┌──────▼──────┐
   │ daily)  │     │ CHAMPIONED  │
   └────┬────┘     └──────┬──────┘
        │                 │ PM-GATE OFFICER: APPROVE / VETO / WITHHOLD
        │ promote   ┌─────┼───────────┬─────────────┐
        │ when      │APPROVE        VETO          WITHHOLD
        │ armed     ▼                ▼             ▼
        │     ┌──────────┐      ┌────────┐   ┌─────────┐
        └────►│ STAGED/  │      │ REJECT │   │  WATCH  │
              │ BOUGHT   │      └────────┘   └─────────┘
              └──────────┘
```

**The rule that defines the chapter:** between `CANDIDATE` and `BOUGHT` sit four judgment gates (Macro, Technical, Adversary, Gate-Officer) plus a Champion. Each is **subtract-only**: it can divert a name to WATCH or REJECT, or pass it forward, but **no gate can force a buy or inflate size**. The deterministic engine (NEXUS) owns the additive sizing floor and the caps. This is the structural fix for "buys too many random things at the wrong time."

### 3.1.3 Branch triggers at each stage

| From → To | Trigger condition (concrete) | Branch |
|---|---|---|
| SOURCED → REJECT | Not in feed universe at all / SCOUT dedup vs an already-held or `_MANUAL_EXCLUDE` name (`{"NVDA","AVGO"}`). | REJECT (no cooldown; just not surfaced) |
| CANDIDATE gate FAIL | `combined < 60` (held names: `< 56`, the 4-pt hysteresis). | not surfaced; held name continues |
| CANDIDATE gate HARD FAIL | `viability == "avoid"` **or** `recommend == False`. | REJECT, 5-trading-day cooldown |
| CANDIDATE → MACRO_HOLD → WATCH | Strategist: regime quad hostile to the name's factor **or** its sector below-200d / outside top-6 RS **or** crowding alert. | WATCH (reason `macro_unsupportive`) |
| MACRO_OK → TECH_WAIT → WATCH | Technician: no valid base, extended > +2σ above 50dma, or distribution signature (see §3.3). | WATCH (reason `await_setup`), schedule re-review at level/date |
| MACRO_OK → TECH_STAGE | Acceptable but imperfect setup: enter a **starter** only. | continue at reduced size |
| TECH_* → ADVERSARY_FLAG → WITHHOLD | SENTINEL argues REJECT or WITHHOLD with a falsifiable bear thesis the desk cannot rebut. | WATCH (reason `adversary_withhold`) or REJECT (reason `adversary_reject`) |
| CHAMPIONED → no champion | PM-CONVICTION declines to propose (slot pressure: at max-names with weaker idea). | WATCH (reason `no_slot`) |
| CHAMPIONED → VETOED | Gate-Officer concentration/mandate veto (sector cap breach, no-ETF, would exceed max-names with insufficient edge). | REJECT (reason `gate_veto`), 3-day cooldown |
| CHAMPIONED → WITHHELD | Gate-Officer: good name, wrong moment (size budget exhausted tonight, awaiting one more confirmation). | WATCH (reason `gate_withhold`) |
| APPROVED → STAGED/BOUGHT | Quorum met (§3.7) AND no veto. Engine resolves size & stage. | BUY |

**Tie-breaks.** (a) If two stages would simultaneously divert a name, the *more conservative* destination wins (REJECT > WATCH > continue). (b) If the Gate-Officer is offline (no LLM), the name **defaults to WITHHELD→WATCH**, never to APPROVE — the inverse of today's SENTINEL default-CONFIRM failure mode (see §1.4 / the current-state baseline). (c) A name that is *already held* skips Champion/Gate-Officer for *adds* but is owned by RISK OFFICER for trims/exits (*Chapter 04*); pyramiding a held name re-enters at TECHNICIAN (§3.5).

---

## 3.2 Candidacy Redefined: Necessary, Not Sufficient

Today, `confirmed = combined≥60 ∧ viability≠avoid ∧ recommend` *is* the buy (`bot/phase2.py`). We retain that exact boolean but **rename its meaning**: it produces a `CANDIDATE`, i.e. the *necessary* precondition that the name is researchable and not vetoed on fundamentals. Each downstream gate adds an orthogonal, *also-necessary* dimension:

| Gate | Adds the question | Signal source (Brief §A / finding 5) | Today's coverage |
|---|---|---|---|
| Engine + ANALYST/FORGE | Is the thesis sound and the name solvent/viable at a defensible price? | `engine_score`, `research_score`, FORGE paper | **Exists** |
| MACRO STRATEGIST | Is the top-down backdrop supportive *now*? | `get_regime` (quad, liquidity_overlay, sector_rs_top), `get_standouts`, `get_intel_hub` sector_heat, `get_themes`, `get_divergences` | **Absent** |
| TECHNICIAN | Does *this chart* deserve an entry now? | `get_fundamentals` tech block (`pct_vs_50dma`, `pct_vs_200dma`, `rsi14`, `off_52w_high_pct`), `get_anticipation` (vol_cone, earnings `next_date`/`sue_z`), `get_options` (gamma_flip, magnets, expected_move, walls) | **Absent** |
| ADVERSARY/SENTINEL | What breaks this, fundamentally and tactically? | broadened beyond macro/portfolio fit (Chapter 02 §2.2) | **Macro-only** |
| PM-CONVICTION → GATE OFFICER | Is this among our *best* uses of a slot, and does it pass concentration discipline? | book context, sizing intent | **Absent** |

The Conviction Index keeps its arithmetic (`round(0.5·engine_score + 0.5·research_score)`, ≥60 confirm / 56 held, size-mult `clamp(0.5+(combined−60)/40, 0.5, 1.3)`). But that size-mult now feeds the engine's *floor* **after** all judgment gates clear — it is no longer self-authorizing.

---

## 3.3 Entry-Quality / Timing Criteria

The Strategist and Technician jointly answer one verdict: **BUY-NOW**, **STAGE-STARTER**, or **WAIT**. Both produce a 0–100 sub-score so the decision is mechanical and gradable; the LLM supplies the reasoning, the rule supplies the threshold.

### 3.3.1 MACRO STRATEGIST entry-context score (`E_macro`, 0–100)

```
E_macro = 100 * (0.30 * regime_fit          # name factor vs get_regime quad/band
               + 0.30 * sector_support       # sector in top-6 RS AND above_200d_trend
               + 0.20 * theme_leadership      # name leads / is in a leading get_themes basket
               + 0.20 * (1 - crowding_penalty))  # get_divergences / intel_hub divergence_alerts
```
`sector_support` is 1.0 if the name's sector is a current Leadership sector (top-4 of top-6 RS with `above_200d_trend`, the exact `phase2.py:193-209` rule), 0.5 if top-6 but not a leader, 0 otherwise. `crowding_penalty` rises with divergence/crowding alerts on the name or its cohort.

### 3.3.2 TECHNICIAN entry-timing score (`E_tech`, 0–100)

```
E_tech = 100 * (0.25 * rs_vs_leader          # name RS ≥ sector-leader RS
              + 0.25 * base_quality           # valid base / clean breakout, not broken
              + 0.20 * (1 - extension_pen)     # pct_vs_50dma, rsi14 → penalty if stretched
              + 0.15 * impulse                 # short-term momentum turning up
              + 0.15 * (1 - distribution_pen)) # off_52w_high_pct + down-volume signature
```
`extension_pen → 1` when `pct_vs_50dma > +12%` or `rsi14 > 78` (parabolic — also a hard veto in `conviction.py`). `distribution_pen → 1` on lower-high under declining 50dma. Catalyst proximity from `get_anticipation` (`next_date` within the horizon, `sue_z` favorable) and `get_options` gamma walls inform the *stage* timing, not the gate itself.

### 3.3.3 The crisp decision rule

| `E_macro` | `E_tech` | Verdict | Action |
|---|---|---|---|
| ≥ 60 | ≥ 65 | **BUY-NOW**† | full sizing path (size-mult up to 1.3) |
| ≥ 60 | 45–64 | **STAGE-STARTER** | starter only (`_INITIAL_SIZE_FRACTION=0.7` × starter cap, §3.5) |
| ≥ 60 | < 45 | **WAIT** | WATCH, reason `await_setup`; re-review at level/date |
| 45–59 | any | **WAIT** | WATCH, reason `macro_soft`; re-review daily |
| < 45 | any | **MACRO_HOLD** | WATCH, reason `macro_unsupportive` |

† This table governs the **WATCH / STAGE / NOW routing**. The **Full-vs-Starter split within NOW** is governed by §3.5: a BUY-NOW entry where `combined` is 60–72 is sized as **STAGE-STARTER** (`_INITIAL_SIZE_FRACTION=0.7`); BUY-NOW at **Full** size requires `combined ≥ 73` **AND** `E_tech ≥ 75`.

**Hard timing vetoes** (subtract-only, fire regardless of score): parabolic (`pct_vs_50dma>+12%` or `rsi14>78`) → STAGE-STARTER downgraded to WAIT; earnings inside 2 trading days with no defined risk → STAGE-STARTER max (no full entry into a binary). These reuse the existing `conviction.py` parabolic veto and extend it.

---

## 3.4 Concentration & Universe Mandates

These fix the specific "too many random things" failure. They are enforced by the engine (NEXUS), not by any LLM seat, and they bind *after* judgment.

| Mandate | Rule | Machinery (cite) |
|---|---|---|
| **No broad sector ETFs in the alpha book** | Sector ETFs may enter **only** via the Leadership sleeve, never the conviction/alpha sleeve. Hard. | Already true: `phase2.py:193-209` (Leadership) vs `conviction.build()`; `_LEADER_NAME` is vestigial. We make it an *invariant*, not an accident. |
| **Per-name cap** | ≤ 8% of book per name. | `name_cap = 0.08` (`conviction.py`) |
| **Sector cap** | ≤ 50% of conviction budget per sector; over-weight sectors scaled **down** proportionally, freed weight to cash (subtract-only). | `SECTOR_MAX_FRACTION = 0.50` (`portfolio/conviction.py:53`) |
| **Manual hold-out** | Do-not-auto-re-add guard. | `_MANUAL_EXCLUDE = {"NVDA","AVGO"}` (`conviction.py:60`) |
| **Min-conviction floor** | No add below `combined = 60` (held 56). | research_paper.py:51, hysteresis 131 |
| **Max-names** | New mandate: hard cap `MAX_NAMES = 12` concurrent conviction-sleeve positions. At cap, a new add requires the Champion to nominate a *swap* (new idea must out-score the weakest held by ≥ 8 combined pts) or the name goes to WATCH (`no_slot`). | new — Gate-Officer enforced, NEXUS validated |
| **Max new-adds per day** | New mandate: `MAX_NEW_ADDS = 3` per build. Excess APPROVED names spill to WATCH ranked by `combined·size_mult`, re-reviewed first next session. | new — paces gross deployment, kills "buy the whole cohort in one night" |

The sector cap is explicitly the **crowding / cohort de-gross** control (its own docstring): "a book that piles a whole homogeneous cohort into one sector is fragile even when each name scores well in isolation." `MAX_NEW_ADDS` is its temporal twin.

---

## 3.5 Entry Staging / Scaling-In

A `BOUGHT` name does not arrive at full target size. Staging is deterministic, driven by the Technician verdict and confirmation:

| Stage | Trigger | Size taken | Caps |
|---|---|---|---|
| **Starter** | Verdict STAGE-STARTER, or BUY-NOW with `combined` 60–72. | `0.7 × size_mult × name_cap` (existing `_INITIAL_SIZE_FRACTION=0.7`, the catalyst-gate scalar from `conviction.py:168-173`). | ≤ 4% per name at starter |
| **Full** | Verdict BUY-NOW with `combined ≥ 73` and `E_tech ≥ 75`. | `size_mult × name_cap`, up to 8%. | `name_cap=0.08` |
| **Pyramid** | Held starter, **only on confirmation**: price advanced ≥ +1 ATR off entry, thesis intact, Technician re-affirms NOW, RISK OFFICER no flag. | one additional `0.7×` increment to full. | never above `name_cap`; never average *down* |

**Pyramiding only on confirmation** is the doctrine's "confirmation over prediction" applied to position-building: we add to winners that the chart confirms, never to losers. A starter that *falls* below its stop is exited by the RISK OFFICER (*Chapter 04*), not averaged into. The catalyst gate (`conviction.py:168-173`) already *scales* size and *never blocks* — we keep that property: staging only ever *reduces* initial commitment, consistent with subtract-only.

---

## 3.6 The Watchlist Subsystem

WATCHLIST is a **first-class state**, not a discard bin. A good-but-not-now name is parked, re-reviewed every build, and promoted the moment it arms — never force-bought, never dropped to cash silently.

### 3.6.1 Watchlist sub-states

```
   ┌──────────┐  enters from any gate diversion (macro/tech/adversary/gate WITHHOLD)
   │  WATCH   │
   │ (candidate)
   └────┬─────┘
        │ daily re-score
   ┌────▼─────┐   E_macro≥60 & E_tech 45-64 & adversary pass
   │  ARMED   │◄──  (one confirmation short of NOW)
   └────┬─────┘
        │ trigger fires (level / date / catalyst / E_tech≥65)
   ┌────▼─────┐
   │   BUY    │──► re-enters funnel at MACRO STRATEGIST (stale-`combined` re-check;
   └──────────┘     skips SCOUT sourcing + FORGE since the thesis is already
                    research-confirmed and `combined` is daily-re-scored; if
                    `combined` has drifted <60 the name is dropped from WATCH,
                    not promoted) — still must clear quorum + gate-officer veto
        │ decay / no promotion in window
   ┌────▼─────┐
   │ EXPIRE   │──► leaves WATCH; may be re-SOURCED later by SCOUT (fresh)
   └──────────┘
```

The promotion `ARMED → BUY` **does not bypass authority**: it re-injects the name at the **MACRO STRATEGIST** stage (skipping SCOUT/FORGE, since the thesis is already research-confirmed and `combined` is re-scored daily) and must traverse Technician → Adversary → Champion → Gate-Officer again, so the full quorum and veto still apply. *(Distinct from the Ch04 §4.4 re-entry rule: a name **exited on a broken thesis** is re-sourced from the full funnel beginning at SCOUT, not promoted from WATCH at STRATEGIST.)*

### 3.6.2 Cadence, scheduling, decay

| Mechanism | Rule |
|---|---|
| **Daily re-review** | Every WATCH name is re-scored each build: refresh `combined`, `E_macro`, `E_tech`; re-run adversary if its stored bear thesis has a check-by today. Cost-managed: re-score is Haiku/Sonnet (SCOUT/Technician light pass); only `ARMED` names get an Opus look. |
| **Anticipated-catalyst scheduling** | Each WATCH record carries a `review_trigger`: `{kind: "level", value: 142.0}` (re-review when price crosses), `{kind: "date", value: "2026-07-15"}` (earnings/Fed/expiry — pull from `get_anticipation.next_date`), or `{kind: "tech", metric: "rsi14", op: "<", value: 55}` (reset from overbought). A name with a near `review_trigger` is prioritized in the re-score queue. |
| **Promotion criteria** | `WATCH → ARMED`: `E_macro ≥ 60` AND `E_tech ≥ 45` AND adversary pass AND `combined ≥ 60`. `ARMED → BUY`: `review_trigger` fires OR `E_tech ≥ 65` (BUY-NOW threshold). |
| **Demotion criteria** | `ARMED → WATCH`: any of `E_macro` or `E_tech` falls below its threshold. `WATCH → REJECT`: candidacy hard-fails (viability→avoid or recommend→False) or a hard adversary REJECT lands. |
| **Decay / expiry** | TTL = **20 trading days** in WATCH without reaching ARMED, or **10 trading days** in ARMED without firing. On expiry: `EXPIRED` with reason `decayed`; thesis archived for grading. A name may be re-SOURCED fresh later, but its *prior* WATCH record's predictions are still graded (Chapter 05: watchlist-non-promotion is a gradable decision — did we correctly avoid it?). |
| **Max watchlist size** | `MAX_WATCH = 40`. At cap, the lowest-ranked WATCH (by `0.5·combined + 0.25·E_macro + 0.25·E_tech`) is EXPIRED to make room. Prevents an unbounded parked book that is never actually reviewable. |
| **Re-scoring a parked name** | Deterministic: pull fresh signals (regime, sector RS, tech block, anticipation), recompute `E_macro`/`E_tech`, re-evaluate triggers, write a new dated artifact. The LLM is invoked only when a threshold is crossed or a trigger fires — keeping the daily pass cheap. |

**Edge cases.** (a) A WATCH name that the desk *already holds* (added earlier) is not a duplicate — held-state is owned by RISK OFFICER; the WATCH record then governs only *pyramiding*. (b) If a name simultaneously hits BUY trigger and a fresh adversary REJECT on the same day, REJECT wins (conservative tie-break, §3.1.3). (c) `_MANUAL_EXCLUDE` names are never admitted to WATCH (the do-not-auto-re-add guard holds across the whole subsystem).

---

## 3.7 Quorum & the Decision Artifact

### 3.7.1 Quorum rule

An **add requires a quorum of positive sign-offs AND no Gate-Officer veto.** Concretely, to reach `APPROVED`:

```
quorum_met =  CANDIDATE (engine+FORGE pass)        # necessary
          AND MACRO STRATEGIST verdict ∈ {OK}       # not HOLD
          AND TECHNICIAN verdict ∈ {NOW, STAGE}     # not WAIT
          AND ADVERSARY/SENTINEL ∈ {PASS}           # not REJECT/WITHHOLD
          AND PM-CONVICTION championed it
          AND PM-GATE OFFICER ruling == APPROVE      # subtract-only authority
```

That is a **6-of-6 positive plus no veto** for a *full* entry. A **STAGE-STARTER** entry relaxes only the Technician arm (STAGE counts as positive at reduced size). No other arm may be skipped. A missing/offline judgment seat defaults to its *conservative* value (HOLD / WAIT / WITHHELD), so an outage shrinks the funnel rather than opening it — the explicit inverse of today's default-CONFIRM hole.

### 3.7.2 Per-stage decision artifact (logged for grading)

Every stage writes one JSONL record to `data/pipeline/ledger.jsonl`. Each is a **falsifiable, gradable thesis** with a `falsifier.check.kind == "rel_return"` so `brain/outcomes.py:label_thesis` can grade it leakage-free vs SPY over the 21-bday horizon (Chapter 05).

```json
{
  "ts": "2026-06-22T20:14:00Z",
  "ticker": "FSS",
  "stage": "TECHNICIAN",
  "role": "TECHNICIAN",
  "verdict": "STAGE",                       // role-specific enum
  "scores": {"combined": 67, "E_macro": 72, "E_tech": 58},
  "rationale": "base intact, +6% vs 50dma (not extended); RS<leader → starter only",
  "probability": 0.58,                       // role's stated confidence
  "falsifier": {                             // gradable claim
    "check": {"kind": "rel_return", "subject": "FSS", "benchmark": "SPY",
              "horizon_bdays": 21, "op": ">=", "value": 0.0},
    "check_by": "2026-07-21"
  },
  "branch": "continue",                      // continue | watch | reject | buy | veto | withhold
  "branch_reason": null,
  "inputs_hash": "sha256:…",                 // signal snapshot for reproducibility
  "watch_trigger": null                      // populated if branch==watch
}
```

**What gets graded (Chapter 05):** the *add* (did the bought name beat SPY?), the *veto* (did the rejected name underperform — a correct veto?), the *withhold/WAIT* (did the parked name's later entry beat a same-day buy — was waiting right?), the *non-promotion* (did an EXPIRED watch name correctly avoid a loss?), and each role's *timing* (did STAGE-STARTER beat full entry?). Every branch in §3.1.3 produces a counterfactual that is scored, feeding per-role calibration, KPIs, attribution, and self-mirror memory.

### 3.7.3 Authority matrix (who can do what to a name)

| Seat | Source | Pass-forward | → WATCH | → REJECT | Size up | Size down | Final APPROVE |
|---|---|---|---|---|---|---|---|
| SCOUT | ✔ | ✔ | — | — | — | — | — |
| MACRO STRATEGIST | — | ✔ | ✔ (HOLD) | — | — | — | — |
| ANALYST/FORGE | — | ✔ | — | ✔ (viability/recommend) | — | — | — |
| TECHNICIAN | — | ✔ | ✔ (WAIT) | — | — | ✔ (starter) | — |
| ADVERSARY/SENTINEL | — | ✔ | ✔ (WITHHOLD) | ✔ (REJECT) | — | ✔ | — |
| PM-CONVICTION | propose | ✔ | ✔ (no_slot) | — | **intent only** | — | — |
| PM-GATE OFFICER | — | — | ✔ (WITHHOLD) | ✔ (VETO) | — | ✔ | ✔ |
| NEXUS (engine) | clerk | — | — | — | **owns floor** | enforces caps | validates quorum |

The single cell that grants additive size authority is **NEXUS / owns floor** — and even it is bounded by `name_cap`, `SECTOR_MAX_FRACTION`, `MAX_NAMES`, and `MAX_NEW_ADDS`. PM-CONVICTION expresses *intent*; the engine resolves the *number*. No LLM seat both pumps and approves — the separation-of-powers invariant, made structural.

---

*Forward references: the seat profiles are in Chapter 02; the buy-side verdicts are specified here in Chapter 03; the `committee.py`/`nexus()` extension that wires these verdicts in and the module/file map are in Chapter 07; the grading of every artifact in §3.7 (and the 2026-07-17 first-resolution window) is in Chapter 05; the per-role KPIs and attribution are in Chapter 06; the core invariants and failure modes are in Chapter 08.*
