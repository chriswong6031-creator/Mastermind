# Chapter 04 — The Sell Pipeline & Risk Officer / Exit Manager

> Cross-references: §1.4 / the current-state baseline for the mechanical baseline; Chapter 02 *The Desk: Organizational Structure & Decision Rights* for the Risk Officer's authority and the quorum/subtract-only invariants; Chapter 03 *The Buy Pipeline & Watchlist Subsystem* for the buy-side quorum and the watchlist state; Chapter 07 *Data Contracts, Module Mapping & Phased Build Plan* for the `committee.py`/`nexus()` API extension; Chapter 05 *The Accountability & Learning Loop* for how exits are graded and fed back.

The buy side of the Flagship has a documented structural flaw (Chapter 01): passing the two gates *is* an automatic buy. The sell side has the symmetric flaw and it is **worse**, because a bad exit is irreversible in P&L terms and a missed exit compounds. Today the desk owns held names with *no judgment seat at all* — only mechanical detectors fire, and a thesis that has been quietly invalidated mid-flight (guidance cut, competitive shock, the leadership rotating away) is carried until a price-based or time-based detector happens to trip. This chapter installs the **RISK OFFICER / EXIT MANAGER** as the human-analyst seat on the sell side, layered *on top of* — never replacing — the existing mechanical exits.

---

## 4.1 The RISK OFFICER / EXIT MANAGER mandate

**One sentence:** the Risk Officer owns every held position and may only **de-risk** it.

| Property | Specification |
|---|---|
| Scope | HELD positions only. Never sees or scores un-owned candidates (that is SCOUT → MACRO STRATEGIST → FORGE, Chapter 02/03). |
| Verb set | `HOLD`, `TRIM(fraction)`, `EXIT`, `TIGHTEN_STOP(level)`, `FLAG_REVIEW`. **No `ADD`, no `UPSIZE`, no `REENTER`.** |
| Subtract-only invariant | Output weight ≤ current weight, always. Enforced by NEXUS at write time (§4.5); a Risk Officer verdict that would raise weight is rejected as malformed, not clamped silently. |
| Lens | Pure risk. The Risk Officer is **not** asked "is this still a good company?" — that is FORGE. It is asked "is the *reason we own this* still intact, and is the *risk we are carrying* still acceptable?" |
| Cadence | **Daily**, on every held lot, *independent of the `gate.py:should_run` build trigger*. This is the critical departure from current behaviour: `should_run` (gate.py:22) can return `run=False` for days at a time, but the exit review still runs every session (it already does for the mechanical hard-exit sweep — we extend that path, not the build path). |
| Model tier | Opus (judgment quality on irreversible decisions; per `config/agents.yml` this is a `deep-reasoner` task). Degrades gracefully: no LLM → judgment layer is skipped, mechanical detectors still run (§4.3). |
| Authority | Final on exits (§4.5). The Gate Officer does **not** veto an exit — subtract-only safety means de-risking is always permitted. CIO reviews exits post-hoc for disposition error, does not pre-approve. |

The Risk Officer is the **mirror image** of PM-CONVICTION (Chapter 02): Conviction may only argue *for* adds and cannot self-approve; the Risk Officer may only argue *for* de-risking and needs no approval. Both directions are subtract-only by construction — judgment can only ever reduce risk.

---

## 4.2 The Exit Thesis — every position carries one FROM ENTRY

**Rule:** a position may not be opened without an Exit Thesis. The buy and its kill-conditions are written in the same transaction. This closes the gap where a name is bought on a bull paper and then held with no pre-committed disinvestment plan.

The Exit Thesis is attached at the `position_log.update()` call in the nightly build (`bot/phase2.py`), derived from the FORGE paper's falsifier plus engine geometry, and stored on the lot. Schema:

```jsonc
// exit_thesis  (one per open lot; written at entry, mutated only by Risk Officer/NEXUS)
{
  "lot_id": "FLAG-NVDA-20260622-01",
  "ticker": "NVDA",
  "entry_date": "2026-06-22",
  "entry_price": 131.40,
  "entry_rel_anchor": "SPY",           // benchmark for rel_return grading (outcomes.py)

  "invalidation": {                    // the FALSIFIER — copied from the FORGE paper
    "condition": "next-quarter DC revenue guide cut, OR sector_rs lens flips bear for >5 sessions",
    "check": { "kind": "rel_return", "horizon_bd": 21, "threshold": 0.0 },  // gradable
    "check_by": "2026-07-17",          // explicit check-by date; anchors calibration window
    "prob_thesis_holds": 0.62          // FORGE's stated probability at entry
  },

  "stop_geometry": {                   // TECHNICIAN-supplied at entry (Chapter 02)
    "structural_stop": 118.90,         // price below which the technical base is broken
    "stop_basis": "below 200dma + prior pivot low",
    "trail": { "mode": "chandelier", "atr_mult": 3.0, "active_above": 152.0 }
  },

  "time_stop": {
    "window_td": 63,                   // doctrine default_window_td (config/doctrine.yml:47)
    "time_stop_by": "2026-09-21",      // entry + 63 trading days
    "unresolved_rel_max": 0.0,         // doctrine unresolved_rel_entry_max (line 48)
    "rs_leader_gap_min": 0.10          // doctrine rs_leader_gap_min (line 49)
  },

  "profit_plan": {                     // the TRIM LADDER (§4.4)
    "rungs": [
      { "trigger_rel": 0.15, "trim_frac": 0.25, "reason": "take initial risk off" },
      { "trigger_rel": 0.30, "trim_frac": 0.25, "reason": "scale into strength" }
    ],
    "let_run_above_rel": 0.30,         // beyond top rung, trail only — do not auto-trim a winner
    "runner_floor_frac": 0.25          // never trim below this; let the runner run
  },

  "review_log": []                     // append-only: each daily Risk Officer verdict (§4.6)
}
```

**Derivation contract (no field may be null at entry):**

| Field | Source | Fallback if source silent |
|---|---|---|
| `invalidation` | FORGE paper falsifier (`research_paper.py`) | engine-derived: "confluence ≤ `_EXIT_CONFLUENCE_FLOOR`" + 21bd rel_return check |
| `stop_geometry` | TECHNICIAN at entry (Chapter 02) | engine: `pct_vs_200dma` pivot from `get_fundamentals` tech block |
| `time_stop` | doctrine `time_stop` block, verbatim | hard default 63 td (matches `d5_dead_capital`) |
| `profit_plan` | PM-CONVICTION sizing intent + TECHNICIAN | default ladder above; runner_floor 0.25 |

If FORGE's falsifier is **not** `kind=="rel_return"`, NEXUS still records it but additionally synthesises a parallel `rel_return` shadow-check so the lot is gradable leakage-free (Chapter 05). A position is never opened with an ungradable exit thesis.

---

## 4.3 The daily exit review — mechanical ⊕ judgment

Two layers run every session over every held lot. The mechanical layer is the existing code (we cite it, we do not rewrite it). The judgment layer is new. They combine by a strict **OR on de-risking, AND on holding** rule: *either layer can force or accelerate an exit; both must be quiet for a lot to simply HOLD.*

### Layer A — mechanical detectors (existing; cite-and-reuse)

These already run and already force exits. The Risk Officer **consumes their output as input**, it does not duplicate them.

| Detector | Module / line | What it catches | Self-mode severity |
|---|---|---|---|
| **D5 dead-capital time-stop** | `detectors.py:34 d5_dead_capital` | held past `time_stop_by` (63 td) **AND** still `rel_return_since_entry ≤ 0` **AND** `rs_leader_gap ≥ 0.10` | `veto` → forced exit |
| **Hard-exit sweep** | `conviction.py:161` | any hard veto (parabolic / Altman / cycle-blocked) **OR** confirmed `price_downtrend` **OR** `size_authority=="blocked"` — *no hysteresis, immediate drop* | forced exit |
| **Hold-floor** | `conviction.py:163` | held name whose `confluence ≤ _EXIT_CONFLUENCE_FLOOR` falls out of `hold_ok` | drop on next build |
| **D6 cap breach** | `detectors.py:51` | single-name > 8% or single-theme > 25% of book | `veto` → forced trim to cap |
| **Hysteresis** | conviction held-name bar 56 (`conviction.py`) | prevents churn on a name dipping just under the 60 confirm line | keeps a name *in* |
| **D1 disposition (warn)** | `detectors.py:64` | held loser past *half* its time-stop window, still underwater | `flag` → Risk Officer prompt input |
| **D4 avg-down-into-divergence (warn)** | `detectors.py:97` | adding to an underwater lagging lot | `flag` |

Note the cadence subtlety from the current-state baseline (§1.4): the hard-exit sweep already runs **when `should_run` returns `run=False`** (the gate.py:140-160 path described in the brief). The Risk Officer's daily judgment review **rides this same always-on sweep**, so it never depends on a regime change to fire.

### Layer B — judgment review (new; what the detectors structurally miss)

The mechanical layer is blind to thesis-invalidation that has not yet shown up as price downtrend, an Altman flip, or a 63-day clock. The Risk Officer catches the gap. It is a **blind-to-cost** review in the sense that it is not asked "are we up or down on this" first — it is asked "is the reason intact." Inputs (all from the existing dashboard surface, brief §A / finding 5):

| Invalidation class the detectors miss | Signal source consumed | Risk Officer reads it as |
|---|---|---|
| Guidance cut / earnings miss | `get_anticipation` (`next_date`, `sue_z`), `get_fundamentals` | thesis falsifier *condition* may have tripped before price |
| Competitive / regulatory shock | `get_themes`, news in `get_decision_matrix`, `get_quiver_*` | the economic hypothesis is broken regardless of chart |
| Mid-thesis regime / narrative rotation **away** | `get_regime` (quad, liquidity_overlay), `get_standouts` sector_heat | "right name, wrong regime now" — the backdrop that justified entry is gone |
| RS roll / relative-strength break | `get_fundamentals` tech (`pct_vs_50dma`, `pct_vs_200dma`, `off_52w_high_pct`) | leadership is leaving this name (precedes D5's 63-day clock by weeks) |
| Distribution / topping structure | `get_options` (gamma_flip, walls, expected_move), tech block | Stage 3→4 tell; trim into strength before the break |
| Crowding unwind | `get_divergences`, intel_hub `divergence_alerts` | the trade is consensus and positioning is unwinding |

Each judgment input that fires produces a **graded, falsifiable** sub-verdict (so the Risk Officer itself is calibrated, Chapter 05), e.g. `{"signal":"rs_roll","action":"TRIM","frac":0.33,"prob":0.7,"check":{"kind":"rel_return","horizon_bd":21,"threshold":0.0},"check_by":"…"}`.

### Combination rule (the tie-breaks, all holes patched)

```
for lot in held:
    mech = mechanical_layer(lot)        # Layer A → set of forced actions {EXIT, TRIM_TO_CAP, ...}
    judg = risk_officer(lot)            # Layer B → at most one verbed verdict (subtract-only)

    # 1. EXIT dominates everything. If either layer says EXIT → EXIT.
    if "EXIT" in mech.actions or judg.verb == "EXIT":
        action = EXIT

    # 2. Otherwise take the MORE de-risking of the two (lower resulting weight wins).
    #    mechanical TRIM_TO_CAP and judgment TRIM(frac) are reconciled by min(weight).
    elif mech.has_trim or judg.verb == "TRIM":
        target_w = min(mech.trim_target_w, judg.trim_target_w)   # most conservative
        action = TRIM_TO(target_w)

    # 3. Stop tightening is additive-safe (only moves stop up): take the HIGHER stop.
    elif judg.verb == "TIGHTEN_STOP":
        action = SET_STOP(max(current_stop, judg.stop))

    # 4. Both quiet → HOLD. (HOLD requires BOTH layers silent — never one.)
    else:
        action = HOLD
```

**Tie-break invariants:**
- Mechanical can **force** an exit (it always could). Judgment can **also** force or **accelerate** an exit — but because it is subtract-only it can *never* override a mechanical exit *upward* into a HOLD. There is no path by which judgment keeps a mechanically-broken name.
- If the judgment layer is unavailable (no LLM), Layer A runs alone — strictly the current behaviour. Safe degradation: the system is never *more* invested for lack of a Risk Officer.
- A `D1` / `D4` *warning* flag is not itself an exit; it is **injected into the Risk Officer prompt** as "the disposition detector is flagging this loser." If the Risk Officer still concludes HOLD, it must record an explicit re-underwrite note in `review_log` (no silent defending of a loser — directly answers the disposition failure mode the detectors only warn about).

---

## 4.4 Trim ladder vs full exit — partial de-risking, profit-taking, re-entry

The decision is not binary. The Risk Officer chooses a point on a **de-risking ladder**, and the choice is governed by *why* it is selling.

### Decision tree (reason → action)

```
                        ┌─ thesis BROKEN (invalidation condition tripped,
                        │   guidance cut, competitive shock, regime gone)
                        │        → FULL EXIT.  Reason is binary; do not "manage" a dead thesis.
   sell pressure ───────┤
                        ├─ thesis INTACT but RISK rising (RS roll, distribution,
                        │   crowding, near structural stop, D1 disposition warn)
                        │        → TRIM one rung (de-risk, keep a runner above runner_floor_frac).
                        │
                        └─ thesis WINNING (hit a profit_plan rung)
                                 → TRIM into strength per ladder; let the rest run; trail only.
```

### The trim ladder mechanics

| Rung condition | Action | Bound |
|---|---|---|
| `rel_return ≥ rung.trigger_rel` | trim `rung.trim_frac` of remaining | never below `runner_floor_frac` (0.25) |
| `rel_return > let_run_above_rel` | **stop auto-trimming**; convert to trailing stop only | a winner is not trimmed to death |
| price ≤ `trail` level (chandelier, ATR×3) | EXIT the runner | trailing discipline, not a fixed target |
| price ≤ `structural_stop` | **FULL EXIT** immediately (overrides ladder) | the base is broken; no averaging, no waiting |
| risk-driven trim (RS roll etc.) | trim `0.25`–`0.50` per the Risk Officer's `frac` | cumulative; each trim re-anchors `runner_floor` check |

**Trailing discipline:** the trail only ratchets *up* (`stop = max(prior_stop, new_chandelier)`). It never loosens. Combined with §4.3 step 3, two independent paths can only raise a stop, never lower it — a held winner's floor monotonically rises.

### Re-entry rule (the disposition trap, sealed)

A sold name does **not** auto-rebuy. On exit:

1. The lot closes; the Exit Thesis is sealed and sent to grading (§4.6).
2. The ticker is placed on the **WATCHLIST** (first-class state, Chapter 03) — *not* deleted, *not* re-bought.
3. To re-enter, a name **exited on a broken thesis** must traverse the **full buy funnel again** from SCOUT → MACRO STRATEGIST → FORGE → TECHNICIAN → ADVERSARY → PM-CONVICTION → Gate Officer quorum (Chapter 03), because its research confirmation is now void. The Risk Officer has **no re-entry authority** (subtract-only; it cannot ADD). *(This is distinct from a routine WATCHLIST promotion of a still-confirmed name, which re-enters at MACRO STRATEGIST and skips SCOUT/FORGE — Ch03 §3.6.1.)*
4. A **cooldown**: a name exited on a *broken thesis* cannot re-clear the gate for `min(7 trading days, until a new catalyst date)` — prevents the desk from immediately re-buying the name it just declared dead on the same stale signal. A name exited on a *profit-take* or *trail* has no cooldown (it may be a perfectly good re-entry on a new base).

This makes the Risk Officer strictly subtract-only end-to-end: it can take risk off and park the name, but the *additive* decision to own it again is owned by the buy desk and the deterministic sizer — exactly the separation-of-powers invariant from Chapter 02.

---

## 4.5 Decision rights, NEXUS enforcement, and never-blow-to-cash

### Authority matrix (exits)

| Decision | Risk Officer | PM-CONVICTION | Gate Officer | CIO | NEXUS (deterministic) |
|---|---|---|---|---|---|
| TRIM a held lot | **owns** | — | — | reviews post-hoc | enforces subtract-only, caps, writes ledger |
| Full EXIT | **owns** | — | — | reviews post-hoc | enforces subtract-only, writes ledger |
| TIGHTEN_STOP | **owns** | — | — | reviews post-hoc | enforces stop-only-ratchets-up |
| Block / veto an exit | **n/a** | n/a | **cannot** (de-risking always permitted) | n/a | n/a |
| Force an ADD / re-buy | **cannot** | proposes | approves | n/a | enforces quorum |
| Override the never-to-cash floor | cannot | cannot | cannot | cannot | **enforces the floor** |

The Gate Officer is **subtract-only on entries**, not on exits. By design no seat can veto a de-risking action — the only failure mode we guard against on the sell side is *failing to sell*, never *over-selling under-instruction*. (Over-selling the whole book is a different risk, handled by the cash floor below.)

### NEXUS enforcement at write time

Every Risk Officer verdict passes through NEXUS (the deterministic spine, Chapter 07's `committee.py` API extension) which **rejects malformed** and **enforces invariants** — it does not exercise judgment:

1. `output_weight ≤ current_weight` (subtract-only). A verdict raising weight is rejected as malformed.
2. New stop ≥ prior stop (ratchet-up only).
3. Caps still hold post-trim (cannot trim *into* a D6 breach elsewhere — trims are independent per lot).
4. **Never-blow-to-cash floor.** NEXUS caps total same-session exit proceeds so the book cannot be driven to all-cash in one session. Concretely: at most **K** full exits per session (default `K = max(1, ceil(0.34 × n_holdings))`) and a hard **minimum invested fraction** `min_invested = 0.20` of book NAV. If the daily review wants to exit more than that, NEXUS executes the **highest-severity** exits up to the floor and **defers** the remainder to the next session (logged, re-evaluated). A genuine all-broken book de-risks over a few sessions, not in a single liquidation — and a single bad signal day cannot empty the book. *(This is the sell-side analogue of "never force-bought" on the buy side: never force-to-cash on the sell side.)*

Tie-break when deferral is required: exits are ranked `EXIT(broken thesis) > EXIT(structural stop) > EXIT(D5 time-stop) > TRIM`. Mechanical hard vetoes (parabolic/Altman) are **exempt from the floor** — a genuinely broken, possibly-fraudulent name is always exited immediately; the floor only throttles judgment-driven and time-stop exits.

### Logging

Every action — HOLD, TRIM, EXIT, TIGHTEN_STOP, deferral, and the *reason* — is appended to the lot's `review_log` and written to `position_log.py` + `ledger.py` as a falsifiable decision artifact (Chapter 05). A HOLD on a flagged loser is logged as an explicit decision *with a falsifier*, so "we chose to hold" is itself gradable. The CIO weekly review (Chapter 05) reads this log for disposition-error and miscalibration patterns; it does not pre-approve exits.

---

## 4.6 Every exit and trim is a graded decision

The whole point of the sell pipeline is that selling becomes **accountable**, not just executed. Each exit/trim is sealed as a thesis and graded leakage-free against the benchmark — forward-referenced in detail to Chapter 05.

**Post-exit grading (the disposition-error test).** When the Risk Officer EXITs at price `P`, we record an exit thesis: *"this name will not outperform SPY over the next 21 trading days."* Then `brain/outcomes.py:label_thesis(thesis, asof)` grades it exactly as it grades any `kind=="rel_return"` falsifier — `rel_return = subject − SPY` over a 21-bday horizon, both legs anchored to the last close ≤ the decision date (the leakage-free anchoring already proven in `outcomes.py`):

| Post-exit outcome (21bd, vs SPY) | Verdict on the exit | Calibration signal |
|---|---|---|
| name kept **falling** / lagged SPY | **good exit** — risk correctly taken off | Risk Officer credited |
| name **ripped** / beat SPY materially | **premature exit / disposition error** | Risk Officer debited; pattern → CIO note |
| roughly flat vs SPY | neutral; no strong signal | small weight |

A TRIM is graded the same way on the *trimmed fraction* (did the slice you sold underperform what you kept?). A profit-take rung is graded on whether the runner you *kept* beat the slice you *sold* — rewarding "trim into strength but let winners run" rather than "sell the whole thing on the first green day."

**Per-role calibration.** These graded exits feed `brain/calibration.py` exactly as FORGE/SENTINEL are graded today: the Risk Officer accrues a `reliability / mean_conf` multiplier (`max(0.5, min(1.0, …))`, FLOOR 0.5, **MIN_N=12** cold-start inert, never inflates). Below 12 resolved exits the multiplier is 1.0 — the Risk Officer's stated confidence is taken at face value until there is an honest sample. The first resolution window opens around the **2026-07-17** calibration deadline (Chapter 05); until then no exit grade changes behaviour. This is the Honesty-over-Alpha guard applied to the sell side: *n=1 good or bad exit is noise and tunes nothing.*

**What is still MISSING today and must be built** (enumerated in Chapter 05, built in Chapter 07): exit grading, trim grading, the "held-a-flagged-loser" HOLD-decision grading, and the Risk Officer calibration series are **not yet wired** — only FORGE/SENTINEL are graded now (`calibration.py`). The schemas and grading path above are the spec to close that gap, reusing `outcomes.label_thesis` and the existing `calibration.py` machinery rather than building new graders.

---

## 4.7 Reconciliation summary — what changes vs what is reused

| Concern | Reused as-is | Newly built |
|---|---|---|
| Time-stop | `detectors.py d5_dead_capital`, doctrine `time_stop` block | per-lot `time_stop_by` materialised at entry |
| Hard exits | `conviction.py:161` veto/downtrend/blocked sweep | nothing — kept verbatim |
| Caps / hysteresis | D6, held-bar-56 | nothing — kept verbatim |
| Disposition warnings | `detectors.py` D1/D4 (flag) | promoted from advisory-only into Risk Officer prompt input |
| Judgment exits | — | Risk Officer seat (§4.1–4.3), Layer B |
| Exit thesis at entry | partial (falsifier in FORGE) | full schema, written at `position_log.update` (§4.2) |
| Trim ladder / trailing | — | profit_plan schema + monotonic trail (§4.4) |
| Never-to-cash | — | NEXUS floor + per-session exit throttle (§4.5) |
| Exit grading | `outcomes.label_thesis`, `calibration.py` machinery | wiring exits/trims/holds through them (§4.6) |

The sell pipeline is therefore **additive and subtract-only**: it adds a judgment seat that can only de-risk, on top of the mechanical exits that already fire, with a deterministic floor that prevents both over-holding (the old flaw) and over-selling (the new failure mode), and every decision it makes is logged as a falsifiable thesis and graded against SPY.
