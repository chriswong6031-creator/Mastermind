# Chapter 02 — The Desk: Organizational Structure & Decision Rights

> *Cross-references:* Chapter 01 — *Executive Summary, Design Philosophy & Diagnosis* (why the current pipeline is "score-passes → auto-buy"); Chapter 03 — *The Buy Pipeline & Watchlist Subsystem* (the entry-decision flow in detail); Chapter 04 — *The Sell Pipeline & Risk Officer / Exit Manager* (judgment exits, watchlist as a held-name state); Chapter 05 — *The Accountability & Learning Loop* (grading, calibration); Chapter 06 — *KPIs, Rewards / Reputation & Credit Assignment* (per-role KPIs, attribution); Chapter 07 — *Data Contracts, Module Mapping & Phased Build Plan* (the `committee.py`/`nexus()` API extension, artifact schemas, build plan); Chapter 08 — *Failure-Mode Register & Residual Risk*.

This chapter is the constitutional centerpiece of the Flagship desk. It defines **who sits at the desk, what each seat is allowed to do, and how an addition gets approved**. The governing principle is **separation of powers under subtract-only safety**: no LLM seat may both pump conviction and approve the size it implies; the deterministic engine (NEXUS) owns the additive sizing floor and enforces every hard invariant; an add requires a **quorum** of positive sign-offs and survives a final Gate-Officer veto. Where today "passing two gates IS a buy" (`bot/phase2.py` line 220 → `paper_account.rebalance()`), the target desk inserts a graded, falsifiable, multi-seat funnel between *candidate* and *fill*.

---

## 2.1 The Org Chart

```
                                  ┌─────────────────────────────┐
                                  │   CIO / META-PM  (weekly)   │
                                  │  oversight · reputation ·   │
                                  │  role-weight tuning · KPIs  │
                                  │      DOES NOT TRADE         │
                                  └──────────────┬──────────────┘
                                                 │ tunes weights / mandates
                                                 │ (gated on effective-n)
   ── SOURCING / TOP-DOWN ──────────────────────▼───────────────────────────────
        ┌──────────┐        ┌─────────────────────────────────┐
        │  SCOUT   │───────▶│        MACRO STRATEGIST         │
        │ Haiku/   │ ideas  │  (Regime & Rotation Officer)    │
        │ Sonnet   │        │  is the backdrop SUPPORTIVE     │
        └──────────┘        │           NOW?                  │
                            └───────────────┬─────────────────┘
                                  macro PASS │ (else → hard WITHHOLD)
   ── BOTTOM-UP RESEARCH & CRITIQUE ────────▼───────────────────────────────────
   ┌─────────────────┐   ┌──────────────────┐   ┌───────────────────────────┐
   │ ANALYST / FORGE │   │  TECHNICIAN /    │   │   ADVERSARY / SENTINEL    │
   │ thesis · FV ·   │   │  TACTICIAN       │   │  blind bear · REJECT /    │
   │ viability ·     │   │  enter / staged /│   │  WITHHOLD · never sees    │
   │ falsifier       │   │  WAIT            │   │  the bull score           │
   └────────┬────────┘   └────────┬─────────┘   └─────────────┬─────────────┘
            │                     │                           │
   ── DECISION / AUTHORITY ───────▼───────────────────────────▼────────────────
                       ┌────────────────────────┐
                       │   PM — CONVICTION      │  proposes/champions add,
                       │   (Idea Champion)      │  sets target sizing INTENT
                       │   CANNOT self-approve  │
                       └───────────┬────────────┘
                                   │ proposal + intent
                                   ▼
                       ┌────────────────────────┐
                       │  PM — GATE OFFICER     │  FINAL: APPROVE / VETO /
                       │  (Chief Investment     │  WITHHOLD.  SUBTRACT-ONLY
                       │   Risk / Veto)         │  on entries.
                       └───────────┬────────────┘
              APPROVE (+ quorum)   │   VETO / WITHHOLD → WATCHLIST
                                   ▼
   ── DETERMINISTIC SPINE ──────────────────────────────────────────────────────
                       ┌────────────────────────┐
                       │        NEXUS           │  synthesis clerk · invariant
                       │  caps · no-ETF ·       │  enforcer · additive sizing
                       │  no-leverage · quorum  │  floor · record-keeper
                       │  GRADER / CALIBRATION  │  (engine, NOT a seat)
                       └───────────┬────────────┘
                                   ▼
                  paper_account.rebalance() / queue_orders()
   ── HELD-BOOK OVERLAY (parallel, daily) ──────────────────────────────────────
                       ┌────────────────────────┐
                       │ RISK OFFICER / EXIT MGR│  TRIM + EXIT ONLY · daily
                       │  falsifier checks atop  │  mechanical detectors (D5
                       │  the mechanical engine  │  time-stop, caps, hysteresis)
                       └────────────────────────┘
```

Three flows are distinct and must not be conflated: the **entry funnel** (left-to-right, top-to-bottom into NEXUS), the **held-book overlay** (Risk Officer, runs every build over the existing book regardless of any new candidate), and the **oversight loop** (CIO weekly, reads the grader and re-weights — never injects a trade).

---

## 2.2 Role Profiles

Each profile fixes: **mandate · mindset · inputs (real signals/artifacts) · outputs · decision rights · model tier + reasoning effort · cadence · primary success metric** (graded per Chapter 05). Signal names are the live dashboard surface exposed in `brain/bot_mcp.py`.

### SCOUT
- **Mandate.** Source candidate ideas daily; widen the funnel mouth beyond what the engine already ranks.
- **Mindset.** High-recall, low-precision sieve. Cheap, fast, disposable. "What deserves a look today?"
- **Inputs.** `get_standouts` (RS / breadth leaders), `get_themes` (active baskets), `get_divergences` (crowding/divergence alerts), `get_intel_hub` (sector_heat), `get_anticipation` (catalyst index / earnings `next_date`), plus open-web search (WebSearch).
- **Outputs.** A ranked candidate list, each row `{ticker, one_line_thesis, why_now, source}` — a falsifiable *why-now* hint, not a thesis.
- **Decision rights.** **propose-add: Contributes** (surfacing only). Nothing else. Cannot size, approve, or veto.
- **Tier / effort.** Haiku default; Sonnet on a thin candidate day. Low reasoning effort.
- **Cadence.** Daily, pre-funnel.
- **Success metric.** Forward hit-rate of *surfaced* names that later cleared the desk and beat SPY (idea-generation alpha; Chapter 06 attribution credits sourcing separately from sizing).

### MACRO STRATEGIST (Regime & Rotation Officer)
- **Mandate.** Own the top-down judgment that is *absent* today: **is the macro / sector / rotation backdrop supportive for this name RIGHT NOW?**
- **Mindset.** Confirmation over prediction. Rotation, liquidity, breadth, crowding — not single-name fundamentals.
- **Inputs.** `get_regime` (quad, scores, `liquidity_overlay`, `sector_rs_top`) — `data/regime/latest.json` is canonical; `get_standouts` + `get_intel_hub` sector_heat (rotation/breadth); `get_themes` (theme leadership); `get_divergences` (crowding/divergence_alerts).
- **Outputs.** `{backdrop: SUPPORTIVE|NEUTRAL|HOSTILE, sector_stance, rotation_note, crowding_flag, falsifier, check_by, confidence}` per candidate's sector/theme.
- **Decision rights.** **withhold-to-watchlist: Owns** (a HOSTILE backdrop is a **hard withhold**, §2.5). propose-add / set-size / approve: None. **Subtract-only** — Strategist can stop or park, never force-buy.
- **Tier / effort.** Sonnet default (aligns with `config/agents.yml` `narrative-analyst`); Opus promotion when the regime `state_signature` changes from the prior build (regime calls are foundational on a regime shift). High effort.
- **Cadence.** Daily; one regime read serves all candidates that day.
- **Success metric.** Calibration of `backdrop` stance vs realized sector relative return; allocation (sector-call) component of Brinson attribution (Chapter 06).

### ANALYST / FORGE
- **Mandate.** Bottom-up underwriting (existing seat, `brain/research_paper.py`). Full bull thesis, economic hypothesis, fair value, viability, falsifier, check-by.
- **Mindset.** Buy-side underwriter; build the strongest defensible *long* case and then state what would prove it wrong.
- **Inputs.** Full engine read (`get_decision_matrix`, `get_fundamentals`), regime, theme/news context — FORGE sees everything.
- **Outputs.** The FORGE paper + `score_breakdown()`: `engine_score = clamp(50 + confluence*50,0,100)`, `research_score`, `combined = round(0.5*engine_score + 0.5*research_score)`, `viability`, `recommend`, `falsifier`. `confirmed = (combined≥60 ∧ viability≠avoid ∧ recommend=True)`.
- **Decision rights.** **propose-add: Contributes** (a name with `confirmed=False` is hard-blocked downstream — subtract-only invariant at `committee.py:9`, enforced 139-146). Does not approve or size.
- **Tier / effort.** Sonnet standard; Opus for the highest-conviction or most ambiguous names. High effort.
- **Cadence.** Daily, per surviving candidate.
- **Success metric.** FORGE calibration: `prob_correct` vs realized falsifier outcome (`brain/calibration.py:_forge_reliability`, MIN_N=12).

### TECHNICIAN / TACTICIAN
- **Mandate.** Judge the chart: does THIS name deserve an entry **now**, a **staged starter**, or a **WAIT-for-setup**? This consumes signals that *already exist in the surface but no seat reads at entry today*.
- **Mindset.** Price structure and tape, not story. RS vs leader, impulse, base/breakout quality, extension/distribution, support/stop geometry.
- **Inputs.** `get_fundamentals` tech block (`pct_vs_50dma`, `pct_vs_200dma`, `rsi14`, `off_52w_high_pct`); `get_anticipation` (vol_cone, horizons, earnings proximity); `get_options` (gamma_flip, magnets, expected_move, walls) for entry geometry.
- **Outputs.** `{verdict: ENTER_NOW|STAGED_STARTER|WAIT, rs_note, impulse, extension, stop_ref, falsifier, check_by, confidence}`.
- **Decision rights.** **set-size: Contributes** (verdict can only *reduce* intent — STAGED_STARTER caps entry tranche; **WAIT → watchlist**, §2.5); **withhold-to-watchlist: Owns** (a WAIT is binding, never an add). Subtract-only.
- **Tier / effort.** Sonnet standard; Opus for breakout-vs-distribution edge cases. Medium–high effort.
- **Cadence.** Daily, per surviving candidate; re-runs on watchlist names at catalyst.
- **Success metric.** Entry-timing alpha — forward return of ENTER_NOW fills vs the same name entered T+N (timing/entry grading, MISSING today, built in Chapter 07).

### ADVERSARY / SENTINEL
- **Mandate.** Blind devil's-advocate bear / thesis-breaker (existing, `brain/committee.py:44-127`; **broadened** per Chapter 07 beyond pure macro/portfolio fit toward thesis-fragility).
- **Mindset.** Assume the bull thesis is wrong; find the strongest reason NOT to own it now.
- **Inputs.** **Structurally restricted** — only the engine decision matrix (`engine_decision_matrix`), `engine_synthesis` (size_authority, confluence, vetoes, divergences, quad), `macro_regime`, and `portfolio` context. **Never** sees `combined`, `research_score`, `viability`, `fair_value`, or `size_mult` (proven `test_committee.py:52-61`). This blindness is a hard invariant — preserved when broadened.
- **Outputs.** `{stance: SUPPORT|CONDITIONAL|OPPOSE, strongest_bear, macro_fit, portfolio_fit, crowding, narrative_maturity, better_alternative, conditions[], confidence, raw_confidence}` (`sentinel_assess`).
- **Decision rights.** **veto-add: Contributes** (OPPOSE high-confidence → NEXUS drops; OPPOSE → trim 0.5; CONDITIONAL → trim 0.66 — `nexus()` lines 157-168). **Subtract-only**; can only de-escalate, never rescue or escalate.
- **Tier / effort.** Opus (`role="deep"`, max_tokens≈1100). High effort.
- **Cadence.** Daily, per confirmed candidate.
- **Success metric.** SENTINEL calibration: stance vs realized rel-return, CONDITIONAL excluded (`brain/calibration.py`); de-confidence multiplier applied at `committee.py:111-115`, raw preserved for grading.

### PM — CONVICTION (Idea Champion)
- **Mandate.** Champion the best ideas into the book and state **target sizing intent**; argue for concentration into highest-conviction names.
- **Mindset.** Portfolio-builder. Where should the book lean harder? Which surviving candidate earns real weight?
- **Inputs.** Full funnel output for each survivor — FORGE breakdown, Strategist backdrop, Technician verdict, SENTINEL stance, current book composition, sector/name caps.
- **Outputs.** `{ticker, proposal: ADD, target_size_intent, conviction_rationale, falsifier, check_by}`. *Intent*, not authority.
- **Decision rights.** **propose-add: Owns**; **set-size: Contributes** (intent only — NEXUS owns the additive floor). **Cannot self-approve** — the champion and the approver are separate seats by constitution.
- **Tier / effort.** Opus. High effort.
- **Cadence.** Daily, per surviving candidate.
- **Success metric.** Selection (name-pick) component of Brinson attribution; realized alpha of championed-and-filled names vs SPY, sized-weighted.

### PM — GATE OFFICER (Chief Investment Risk / Veto)
- **Mandate.** FINAL authority on an addition: **APPROVE / VETO / WITHHOLD**. Owns concentration discipline, the no-broad-ETF mandate, max-names, and the min-conviction floor.
- **Mindset.** Capital preservation and portfolio coherence over any single idea. The seat that says "no" so the desk does not own "too many random things."
- **Inputs.** The PM-Conviction proposal + full funnel dossier; live book state; caps (`name_cap=0.08`, `SECTOR_MAX_FRACTION=0.50`), `_MANUAL_EXCLUDE`, max-names.
- **Outputs.** `{decision: APPROVE|VETO|WITHHOLD, downsize_to?, watchlist_reason?, falsifier, check_by}`.
- **Decision rights.** **approve-add: Owns**, **veto-add: Owns**, **withhold-to-watchlist: Owns**, **set-size: Contributes** (downsize only). **SUBTRACT-ONLY on entries**: may reject, downsize, or park; **can NEVER force-add a name the desk did not surface**, and cannot increase size above PM-Conviction's intent or NEXUS's floor.
- **Tier / effort.** Opus. High effort.
- **Cadence.** Daily, per proposed add.
- **Success metric.** Veto/withhold calibration — counterfactual return of vetoed names vs SPY (a good veto precedes underperformance; veto grading is MISSING today, built in Chapter 07); drawdown/concentration of the live book.

### RISK OFFICER / EXIT MANAGER
- **Mandate.** Own **held positions**: trim and exit only. Daily falsifier / thesis-invalidation checks layered atop the mechanical detectors.
- **Mindset.** Pure risk lens on what is already owned. "Has this thesis quietly broken? Is the original falsifier tripped?"
- **Inputs.** Held book (`portfolio/position_log.py`), each name's stored falsifier/check-by from the ledger (`brain/ledger.py`), live tech/regime, mechanical detector state (D5 time-stop, caps, hysteresis bar 56).
- **Outputs.** `{ticker, action: HOLD|TRIM|EXIT, scale, invalidation_reason, falsifier_status, check_by}`.
- **Decision rights.** **trim: Owns**, **exit: Owns**. **SUBTRACT-ONLY** — never adds, never re-sizes up, never re-enters a sold name (that path is the full entry funnel). Operates *atop* mechanical detectors, not instead of them.
- **Tier / effort.** Opus. High effort.
- **Cadence.** Daily, over the entire held book (runs even when `gate.should_run`=False — the hard-exit sweep, `gate.py:140-160`, is its mechanical floor).
- **Success metric.** Exit-timing alpha — return avoided after EXIT vs holding to the mechanical trigger (exit grading is MISSING today, built in Chapter 07).

### CIO / META-PM (Performance & Accountability)
- **Mandate.** Weekly oversight across all books/roles. Review KPIs, attribution, calibration; tune role-influence weights, mandates, reputation; promote/demote a role's authority. **DOES NOT TRADE.**
- **Mindset.** "What is working / who is miscalibrated." Statistical honesty over narrative.
- **Inputs.** `brain/scorer.py` (Brier/hit-rate), `brain/calibration.py` (per-agent multipliers), `portfolio/predictions.py` (universe rank-IC/Brier, Newey-West HAC, `effective_n`), `portfolio/shadow_books.py` (counterfactual leaderboard), `portfolio/readiness.py` (threshold watcher).
- **Outputs.** Weekly memo + role-weight/mandate deltas; reputation updates. Changes are config/weight edits, never a trade ticket.
- **Decision rights.** **override: Owns** — but only over *role influence/mandate*, never over a single trade. Gated on significance: a weight change requires `effective_n` past `_MIN_DATES=8` / calibration `n≥MIN_N=12` / shadow `max_resolved≥5` (`readiness.py`).
- **Tier / effort.** Opus. Maximum effort.
- **Cadence.** Weekly.
- **Success metric.** Whether re-weighting decisions improved forward book Brier/IC vs the `prod` shadow book (meta-calibration).

### NEXUS (deterministic spine)
- **Mandate.** Synthesis clerk, record-keeper, and **invariant enforcer**. Not an LLM seat — a pure, exhaustively testable function (`brain/committee.py:nexus`).
- **Mindset.** None — deterministic. It encodes the constitution in code.
- **Inputs.** FORGE breakdown, Strategist/Technician/SENTINEL verdicts, PM-Conviction intent, Gate-Officer decision, caps/exclusions.
- **Outputs.** `{action: confirm|trim|drop, scale, lean, rationale, quorum_met, invariants_checked}` + durable per-name artifacts (`data/committee/<asof>/<ticker>/`).
- **Decision rights.** Enforces: **subtract-only** (never escalates/rescues — `forge_confirmed=False → drop`), **caps** (`name_cap`, `SECTOR_MAX_FRACTION`), **no-leverage**, **no-broad-ETF** in the alpha sleeve, **quorum** (§2.4), and the **additive sizing floor** (the *only* component allowed to add size — every LLM seat can only subtract from it).
- **Tier / cadence.** Deterministic; every build.
- **Success metric.** Zero invariant violations (audited via artifacts); this is a correctness guarantee, not a performance metric.

### GRADER / CALIBRATION SUBSYSTEM (engine, not a seat)
- **Mandate.** Close the accountability loop. Label every decision-type outcome leakage-free vs SPY and feed per-role calibration.
- **Inputs.** `brain/outcomes.py` (`label_thesis`, rel_return over 21 bday, leakage-free anchoring `req_end=min(final_end,asof)`); the thesis ledger; committee artifacts.
- **Outputs.** Per-agent multipliers in `(FLOOR=0.5, 1.0]` (`brain/calibration.py`), Brier/hit-rate (`brain/scorer.py`), readiness flags.
- **Decision rights.** None over trades. Applied at `committee.py:111-115` on raw_conf *before* NEXUS sees it; **never inflates**, inert below MIN_N=12.
- **Cadence.** Recomputed each build from latest resolved outcomes.
- **Success metric.** Coverage — every decision type (add/veto/withhold/size/trim/exit/watchlist) is gradable (Chapter 07 fills the MISSING graders).

---

## 2.3 The Authority Matrix

Actions: **propose-add · approve-add · veto-add · set-size · trim · exit · withhold-to-watchlist · override**. `Owns` = final authority; `Contributes` = input only; `None` = no rights.

| Role | propose-add | approve-add | veto-add | set-size | trim | exit | withhold→WL | override |
|---|---|---|---|---|---|---|---|---|
| **SCOUT** | Contributes | None | None | None | None | None | None | None |
| **MACRO STRATEGIST** | None | None | None | None | None | None | **Owns** | None |
| **ANALYST / FORGE** | Contributes | None | None | None | None | None | None | None |
| **TECHNICIAN** | None | None | None | Contributes | None | None | **Owns** | None |
| **ADVERSARY / SENTINEL** | None | None | Contributes | None | None | None | None | None |
| **PM — CONVICTION** | **Owns** | None | None | Contributes | None | None | None | None |
| **PM — GATE OFFICER** | None | **Owns** | **Owns** | Contributes¹ | None | None | **Owns** | None |
| **RISK OFFICER / EXIT MGR** | None | None | None | None² | **Owns** | **Owns** | None | None |
| **CIO / META-PM** | None | None | None | None | None | None | None | **Owns**³ |
| **NEXUS** | None | None⁴ | None⁴ | **Owns** | None⁴ | None⁴ | None | None |

¹ Gate Officer's `set-size` is **downsize-only** (never above PM-Conviction intent / NEXUS floor). ² Risk Officer's trim/exit *implies* size reduction but it cannot set entry size. ³ CIO `override` is over **role influence / mandate**, never a single trade. ⁴ NEXUS *enforces* the outcomes of approve/veto/trim/exit deterministically; it never *originates* them.

**Subtract-only ledger (who can only de-risk):** MACRO STRATEGIST, TECHNICIAN, ADVERSARY/SENTINEL, PM-GATE OFFICER (on entries), RISK OFFICER/EXIT MANAGER. The **only** component permitted to add size is **NEXUS**, and only up to the engine-derived floor. PM-Conviction expresses *intent*, which NEXUS may meet or any subtract-only seat may cut — but never exceed.

---

## 2.4 Quorum & Sign-Off Rules for an Addition

An addition fills **only if all of the following hold** (any failure → not a buy):

1. **FORGE confirmed** — `confirmed = (combined≥60 ∧ viability≠avoid ∧ recommend=True)`. Hard prerequisite; without it NEXUS drops (subtract-only invariant). *Required.*
2. **MACRO STRATEGIST backdrop ≠ HOSTILE.** SUPPORTIVE or NEUTRAL. HOSTILE is a hard withhold (§2.5). *Required positive sign-off.*
3. **TECHNICIAN verdict ∈ {ENTER_NOW, STAGED_STARTER}.** A WAIT routes to watchlist. STAGED_STARTER permits entry but caps the first tranche. *Required positive sign-off.*
4. **ADVERSARY/SENTINEL not a high-confidence OPPOSE.** OPPOSE@conf≥0.6 forces NEXUS drop; OPPOSE/CONDITIONAL only de-size. *Required: no decisive veto.*
5. **PM — CONVICTION proposes the add** with target sizing intent. *Required: a champion.*
6. **PM — GATE OFFICER decision = APPROVE.** *Required: final approval, and no veto.*

**Quorum definition.** The required positive sign-offs are FORGE-confirmed + Strategist(non-hostile) + Technician(enter/staged) + a PM-Conviction champion + Gate-Officer APPROVE, **with no Gate-Officer veto and no SENTINEL high-conviction OPPOSE.** This is a **conjunctive quorum** — every gate is necessary; none is sufficient alone. **Nobody can force an add**: even unanimous enthusiasm cannot override a Gate-Officer veto, and the Gate Officer cannot conjure a name PM-Conviction never championed. NEXUS verifies the quorum bit before any `rebalance()` call and records `quorum_met` in the artifact.

---

## 2.5 Deadlock & Tie-Break Rules

The default resolution of *any* unresolved disagreement is **WITHHOLD → WATCHLIST** (never force-buy, never blow to cash). Specific rules, in precedence order:

| # | Situation | Resolution |
|---|---|---|
| 1 | **MACRO STRATEGIST backdrop = HOSTILE** | **Hard withhold → watchlist.** Overrides all downstream enthusiasm. The name is re-reviewed daily until the backdrop turns. |
| 2 | **TECHNICIAN verdict = WAIT** | **Watchlist, not buy.** Re-reviewed daily and at the next anticipated catalyst (`get_anticipation` horizon). Never a fill. |
| 3 | **SENTINEL = OPPOSE @ conf ≥ 0.6** (post-calibration) | NEXUS **drops**; routes to watchlist with the bear case attached. Calibration shrink (`committee.py:111-115`) can demote a historically-wrong adversary below the 0.6 bar — then it only trims, per existing logic. |
| 4 | **PM-CONVICTION (ADD) vs GATE OFFICER (VETO/WITHHOLD)** | **Gate Officer wins → watchlist.** The champion cannot overrule the veto seat — separation of powers. Disagreement is logged as two gradable theses (Chapter 05) so the desk learns who was right. |
| 5 | **PM-CONVICTION (ADD) vs GATE OFFICER (APPROVE but downsize)** | Add fills at the **smaller** of {Conviction intent, Gate downsize, NEXUS floor}. Subtract-only: the lowest non-zero size wins. |
| 6 | **FORGE confirmed but quorum incomplete** (e.g., no champion) | **Withhold → watchlist.** Confirmation alone is *not* a buy (the core redefinition). |
| 7 | **All seats silent / no LLM available** | Degrade gracefully: FORGE+engine decision passes through, SENTINEL skipped (`committee.py` graceful-degrade). **But** with the funnel installed, a missing Strategist or Technician verdict = **missing required sign-off = withhold** (fail-safe to *not buying*, never fail-open to a buy). |
| 8 | **Risk Officer EXIT vs mechanical detector HOLD** | **Exit wins** (subtract-only always permits de-risking). |
| 9 | **Two seats tie on a size cut** | Apply the **largest** cut (most conservative). |

The invariant across every row: **ties and ambiguity resolve toward *less* risk** — withhold, downsize, or watchlist — never toward an unreviewed buy.

---

## 2.6 Flagship Desk vs the Autonomous Brains

The full desk is reserved for the **Flagship** (the disciplined, concentration-for-alpha alpha book). The regional **Brain** books (US / CN / HK) are deliberately *single discretionary Opus* mandates with a thin safety overlay — they are free-form by design and must not be over-engineered into a committee.

| Seat / Guardrail | **Flagship** | **US/CN/HK Brain** | **Self-Directed** |
|---|---|---|---|
| SCOUT | ✅ full | ❌ (Opus self-sources) | ❌ (manual) |
| MACRO STRATEGIST | ✅ | ↺ folded into the single Opus | ❌ |
| ANALYST / FORGE | ✅ | ↺ folded into the single Opus | ❌ |
| TECHNICIAN | ✅ | ↺ folded into the single Opus | ❌ |
| ADVERSARY / SENTINEL | ✅ | ❌ | ❌ |
| PM — CONVICTION | ✅ | ↺ single Opus is its own champion | 👤 human |
| PM — GATE OFFICER | ✅ full veto | ⚠️ **guardrails only** (ETF/concentration/max-names/min-conviction enforced deterministically) | ⚠️ deterministic caps |
| RISK OFFICER / EXIT MGR | ✅ | ✅ (risk/exit overlay) | ⚠️ mechanical only |
| CIO / META-PM | ✅ | ✅ (shared weekly oversight) | ✅ (read-only) |
| Self-mirror memory | ✅ per-role | ✅ single-agent | ❌ |
| NEXUS invariants | ✅ full | ✅ (caps/no-ETF/no-leverage) | ✅ (caps) |

**Reading.** A Brain book = **one discretionary Opus** that internalizes Strategist/Analyst/Technician judgment in a single free-form pass, **+** a Risk/Exit overlay (trim/exit only), **+** self-mirror memory, **+** Gate-style *deterministic* guardrails (no broad ETFs in the alpha sleeve, concentration/max-names/min-conviction). It has **no separate adversary and no multi-seat quorum** — its discipline is the NEXUS invariant spine, not a committee. The Flagship is the only book that runs the full separation-of-powers desk.

---

## 2.7 Mapping to NEXUS / committee.py (high level; detail → Chapter 07)

The existing `brain/committee.py` already encodes the spine this chapter generalizes: a blind adversary (`sentinel_assess`), a deterministic subtract-only synthesizer (`nexus`), graceful degradation, and durable per-agent artifacts for grading. The target desk **extends, never rewrites** this contract:

- **New seats follow the SENTINEL pattern**: each adds a `_<agent>_input(...)` (the *exact* context that seat may see — e.g., the Technician's input excludes `combined`/`viability`, mirroring SENTINEL's blindness invariant) and an `<agent>_assess(...)` returning a normalized, gradable verdict dict. *(Spec in Chapter 07.)*
- **`nexus()` is extended** from the 2-input (FORGE + SENTINEL) synthesis to a quorum check over the full funnel — still a **pure, exhaustively testable function**, still **subtract-only** (`forge_confirmed=False → drop`), now also asserting `quorum_met`, the Strategist hard-withhold, the Technician WAIT→watchlist route, and the smallest-size-wins tie-break (§2.5).
- **`assess()` orchestration** gains the upstream Strategist/Technician calls and the downstream PM-Conviction/Gate-Officer arbitration, each wrapped in the same `try/except → None` graceful-degrade so an additive seat can never break the gate.
- **Artifacts** (`_write_artifacts`) expand to one file per seat under `data/committee/<asof>/<ticker>/`, making every decision type self-describing and feeding the Chapter 05 graders (the watchlist, veto, timing, and exit graders that are *missing today*).
- **The deterministic floor is unchanged**: NEXUS remains the sole component that may *add* size; all new LLM seats are wired to *subtract* only. Caps (`name_cap=0.08`, `SECTOR_MAX_FRACTION=0.50`), `_MANUAL_EXCLUDE`, no-ETF, and no-leverage stay in the deterministic enforcer, untouched by any judgment seat.

This preserves every existing invariant (subtract-only, blindness, graceful degradation, full auditability) while converting the pipeline from **"score-passes → auto-buy"** into the **right-name-AND-right-time, quorum-gated funnel** specified above. The held-book overlay (Risk Officer) and the weekly oversight loop (CIO) attach to the same artifact/grader substrate, completing the org without violating the deterministic spine.
