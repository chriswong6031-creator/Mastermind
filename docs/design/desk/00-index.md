# Mastermind — The Desk: Multi-Role Organizational Architecture & Accountability/Learning Loop

**Version 1.0 · 2026-06-22**

---

## Abstract

The Mastermind Flagship paper book today auto-buys any name that clears two gates (engine confluence + a Conviction Index ≥ 60), with no seat judging *entry timing*, the *top-down backdrop*, or *judgment-based exits* — and no decision graded after the fact. This document redesigns the Flagship into an institutional **desk**: a right-name-AND-right-time funnel governed by **separation of powers** and **subtract-only safety**, where a passing score is mere CANDIDACY, an add requires a conjunctive **quorum** of positive sign-offs plus no Gate-Officer veto, good-but-not-now names are parked on a first-class **WATCHLIST**, and every decision type is logged as a falsifiable thesis, graded leakage-free vs SPY, and fed back through per-role calibration, KPIs, attribution, reputation, and **self-mirror** memory under a weekly **CIO** review. The deterministic engine (NEXUS) keeps the sole additive-sizing authority and enforces all hard invariants; judgment may only ever de-risk.

---

## Reading Guide

- **Start here, then read Ch01 → Ch02.** Ch01 is the thesis and the diagnosis; Ch02 is the constitutional org chart and authority model. Everything downstream cites them.
- **Ch01 §1.5.1 and Ch02 §2.3 are the canonical authority matrices.** Where any later chapter's table disagrees, those two win.
- **Ch07 is the build spec.** It is authoritative for all on-disk artifacts, paths, schemas, the `nexus()` extension, and the phased plan. When a path or parameter conflicts, Ch07 §7.1–7.3 governs.
- **The Canonical Brief §D is the single source of truth for role names and term definitions.** Use the exact names in the Glossary below.
- **Honesty over Alpha is binding.** No threshold is tuned to the one-day result or to IREN/KMT/FSS; nothing changes desk behavior until it clears effective-n / MIN_N significance.
- **Chapter file numbering does not match Ch01 §1.7's promised titles** — see Open Question #1. Use the Table of Contents below for the actual file → content mapping.

---

## Table of Contents

| # | File | Title | One-line | Key contents |
|---|---|---|---|---|
| 00 | `00-index.md` | **Index & Front Matter** | This document. | Abstract · reading guide · TOC · glossary · decision log · open questions. |
| 01 | `01-summary-and-diagnosis.md` | **Executive Summary, Design Philosophy & Diagnosis** | Why the pipeline is "score-passes → auto-buy", and the redesign thesis. | The four-gates/one-loop mission · 7 design principles (separation of powers, subtract-only, confirmation-over-prediction, concentration, watchlist, accountability, honesty) · the diagnosis (n=1 is noise; the durable structural flaw) · the entry state machine · **§1.5.1 canonical authority matrix + tie-breaks** · scope per book. |
| 02 | `02-organization-structure.md` | **The Desk: Organizational Structure & Decision Rights** | Who sits at the desk and what each may do. | Org chart · per-seat role profiles (mandate/inputs/outputs/rights/tier/cadence/metric) · **§2.3 canonical authority matrix** · **§2.4 conjunctive quorum** · §2.5 deadlock/tie-breaks · Flagship-vs-Brain-books · NEXUS/`committee.py` mapping. |
| 03 | `03-buy-pipeline-and-watchlist.md` | **The Buy Pipeline & Watchlist Subsystem** | The seven-stage BUY funnel and the WATCHLIST. | Idea state machine · candidacy redefined (necessary, not sufficient) · `E_macro`/`E_tech` scores + decision table · concentration mandates (`MAX_NAMES=12`, caps) · entry staging (`_INITIAL_SIZE_FRACTION=0.7`) · watchlist sub-states / TTL / decay (`MAX_WATCH=40`) · quorum + per-stage decision artifact. |
| 04 | `04-sell-pipeline.md` | **The Sell Pipeline & Risk Officer / Exit Manager** | The judgment exit layer atop mechanical detectors. | Risk Officer mandate (trim/exit only) · the Exit Thesis written at entry · mechanical ⊕ judgment daily review · trim ladder vs full exit · re-entry rule (broken-thesis full funnel) · **never-blow-to-cash floor** (`max_exits_per_session`, `min_invested_fraction=0.20`) · every exit graded. |
| 05 | `05-accountability-learning-loop.md` | **The Accountability & Learning Loop** | How every decision becomes a graded prediction. | The core loop + invariants LL-1..LL-4 · per-decision-type grading specs + counterfactuals · mechanisms (calibration / shadow ablation / universe predictions / self-mirror) · CIO weekly review · statistical honesty · cross-book application. |
| 06 | `06-kpis-rewards-attribution.md` | **KPIs, Rewards / Reputation & Credit Assignment** | Per-seat measurement, bounded rewards, Brinson attribution. | Measurement substrate · per-role KPI tables · reward levers (influence/budget/reputation/memory/authority, all `≤1.0`) · Goodhart defenses · Brinson-adapted credit assignment (allocation/selection/timing/sizing/exit/veto). |
| 07 | `07-data-contracts-and-build-plan.md` | **Data Contracts, Module Mapping & Phased Build Plan** | The authoritative build spec. | Storage conventions · all artifact schemas (`data/committee/<asof>/<TICKER>/<seat>.json` etc.) · role→module map · **`nexus()` quorum/subtract-only extension** · scheduler order of operations · **phased plan P1–P6** anchored to 2026-07-17 · watchlist state machine · grader correctness table · open questions. |
| 08 | `08-failure-mode-register.md` | **Failure-Mode Register & Residual Risk** | The core invariants and the 40-hole register that stress-tests them. | The core invariants (separation of powers, subtract-only both sides, quorum, no-ETF, never-to-cash, everything gradable) · the red-team failure-mode register (categories A–D) · phase gates · defense-in-depth summary + honest residual risks. |

---

## Glossary (canonical role names & key terms — Brief §D)

### Roles (the Flagship desk)

| Role (canonical) | A.k.a. | One-line mandate |
|---|---|---|
| **SCOUT** | — | Haiku/Sonnet sourcing agent; daily candidate ideas + one-line thesis + why-now. Surfacing only. |
| **MACRO STRATEGIST** | Regime & Rotation Officer | Top-down read: is the regime/sector/rotation/breadth/crowding backdrop supportive **now**? Subtract-only (withhold/park). |
| **ANALYST / FORGE** | — | Bottom-up underwriter (existing); thesis, fair value, viability, falsifier, check-by. Sees everything. |
| **TECHNICIAN / TACTICIAN** | — | Price structure / RS / impulse / extension. Verdict: enter-now / staged-starter / wait. Subtract-only. |
| **ADVERSARY / SENTINEL** | — | Blind devil's-advocate bear (existing; to be broadened). Never sees the bull score. Contributes a stance; NEXUS enforces the drop. |
| **PM — CONVICTION** | Idea Champion | Proposes/champions adds, sets target sizing **intent**. Cannot self-approve. |
| **PM — GATE OFFICER** | Chief Investment Risk / Veto | FINAL APPROVE/VETO/WITHHOLD; subtract-only on entries; owns concentration / no-ETF / max-names / min-conviction. |
| **RISK OFFICER / EXIT MANAGER** | — | Owns held positions; trim + exit only; daily falsifier checks atop mechanical detectors. |
| **CIO / META-PM** | — | Weekly oversight; tunes role weights/mandates/reputation past significance. Does not trade. |
| **NEXUS** | — | Deterministic synthesis clerk + invariant enforcer + record-keeper. Sole additive sizer. Not a seat. |
| **GRADER / CALIBRATION** | — | Engine (not a seat): labels every decision leakage-free vs SPY, feeds per-role calibration. |

### Key terms

- **Conviction Index** — `round(0.5·engine_score + 0.5·research_score)`, 0–100; ≥60 confirms a new buy (56 held, 4-pt hysteresis). A pass makes a name a **CANDIDATE**, not an auto-buy (target-state redefinition).
- **Falsifier** — the specific condition + check-by date that proves a thesis wrong; `falsifier.check.kind=="rel_return"` makes it gradable.
- **Subtract-only** — LLM judgment may only de-risk (veto/trim/exit/withhold/downsize/park); the deterministic engine owns additive sizing. No seat both pumps and approves size.
- **Quorum (conjunctive)** — an add requires **all** named positive sign-offs (Strategist non-hostile, Technician enter/staged, Sentinel not high-confidence-OPPOSE, PM-Conviction champion, Gate-Officer APPROVE) **AND** no Gate-Officer veto. Not a count — every arm is individually necessary.
- **NEXUS additive floor** — NEXUS provides the *minimum* authorized size (engine-derived, up to `name_cap`); PM-Conviction intent above it is advisory; realized size is capped at `name_cap=0.08` and `SECTOR_MAX_FRACTION=0.50`; subtract-only seats may only cut below.
- **Watchlist** — first-class state for good-but-not-now names; re-reviewed daily / at catalyst; never dropped, never force-bought. TTL 20 td (WATCH) / 10 td (ARMED); `MAX_WATCH=40`. Promotion re-enters at MACRO STRATEGIST (skips SCOUT/FORGE) and re-clears quorum.
- **Self-mirror** — injecting each role's own graded track record into its daily prompt (system-prompt prefix block; quality-gated, contradiction-checked).
- **Calibration multiplier** — `max(0.5, min(1.0, reliability/mean_conf))`; shrinks confidence by track record; inert below `MIN_N=12`; never inflates.
- **Effective-n** — count of independent (date-clustered, non-overlapping ~21-bday) observations; the honest sample size. Behavior changes only past significance thresholds.
- **Attribution** — Brinson-style split of active return into allocation (sector call) / selection (name pick) / interaction, extended with timing / sizing / exit / veto legs; credit per signal, not total P&L.
- **Never-blow-to-cash** — the sell-side floor: ≤ `max_exits_per_session` exits and ≥ `min_invested_fraction=0.20` of NAV per session; throttled exits are deferred, not lost.

---

## Decision Log (the load-bearing design decisions)

| # | Decision | One-line rationale |
|---|---|---|
| 1 | **Candidacy, not auto-buy** | A high Conviction Index certifies the *thesis*, not the *entry*; a pass is a CANDIDATE that must clear the funnel. |
| 2 | **Separation of powers** | No seat may both pump conviction and approve the size it implies — generalizes the existing SENTINEL blindness invariant across the whole desk. |
| 3 | **Subtract-only (both sides)** | LLM judgment may only de-risk; NEXUS alone adds size and enforces caps — bounds reward-hacking and keeps sizing deterministic. |
| 4 | **Two PMs + Gate-Officer veto** | A champion (PM-Conviction, proposes intent) split from an approver (Gate-Officer, final APPROVE/VETO/WITHHOLD) so enthusiasm cannot self-authorize. |
| 5 | **Strategist + Technician timing seats** | Install the absent top-down (backdrop-now) and entry-timing (chart-now) judgments using signals already in the surface but read by no seat at entry today. |
| 6 | **Watchlist as first-class state** | Good-but-not-now names are parked and re-reviewed, never silently dropped or force-bought — closes the "non-buy vanishes" hole. |
| 7 | **Judgment exit layer** | A Risk Officer lays daily falsifier checks atop the mechanical detectors so a quietly-broken thesis is not held to a mechanical trigger. |
| 8 | **Self-mirror memory** | Inject each role's own graded track record into its prompt: cheapest, highest-ROI, reversible, per-role, falsifiable — vs the cost/opacity of fine-tuning. |
| 9 | **Weekly CIO review** | One non-trading meta-seat re-weights role influence — only past effective-n/MIN_N significance, bounded, reversible, never self-promoting. |
| 10 | **Attribution-based reward** | Credit by Brinson component (allocation/selection/timing/sizing/exit), counterfactual-anchored, risk-adjusted — never raw P&L; idea generation credited separately from sizing/timing. |
| 11 | **Everything gradable, leakage-free** | Every decision type is a falsifiable thesis graded vs SPY (21-bday, leakage-free anchoring); no ungraded decisions enter the book. |
| 12 | **Honesty over Alpha / significance gating** | n=1 is noise; the one-day result and IREN/KMT/FSS tune nothing; grades move the desk only past MIN_N=12 / `_MIN_DATES=8` / shrinkage. |

---

## Open Questions for the User

Items #2–#3 are **structural gaps** in the draft set (per-seat specs not yet drafted); the rest carry concrete defaults (Ch07 §7.8) that ship unless overridden.

1. **Chapter numbering — RESOLVED.** Ch01 §1.7 and every cross-reference in Ch02–Ch08 have been rewritten to point at the **actual** files (Ch02 = Organization, Ch03 = Buy Pipeline, Ch04 = Sell Pipeline, Ch05 = Accountability, Ch06 = KPIs, Ch07 = Data Contracts/Build Plan, Ch08 = Failure-Mode Register). This index's TOC is authoritative; no further action needed.
2. **Per-seat reasoning specs are not yet drafted** (the SCOUT/STRATEGIST/TECHNICIAN/PM/GATE-OFFICER seat-input schemas). Missing: MACRO STRATEGIST `_strategist_input(...)` and TECHNICIAN `_technician_input(...)` context schemas with the technician blindness invariant (no `combined`/`viability`/`fair_value`); PM-CONVICTION `_pm_conviction_input(...)` and GATE-OFFICER `_gate_officer_input(...)` schemas (incl. whether Gate-Officer should see PM-Conviction's confidence — anchoring risk); and the SENTINEL broadening spec (new chart/thesis-fragility lenses mapped to signals while preserving the blindness invariant). **Required before Phase 3** (Ch07 §7.5).
3. **`committee.py` orchestration extension** — the `assess()` API change adding the Strategist/Technician/PM-Conviction/Gate-Officer stages and the extended `nexus()` signature (Ch07 §7.3) needs a dedicated IO/wiring spec.
4. **(A) Opus cost budget per night** — `MAX_OPUS_SEATS_PER_NIGHT` cap; SCOUT=Haiku, STRATEGIST/TECHNICIAN=Sonnet (Opus on regime `state_signature` change), PM-CONVICTION/GATE-OFFICER/SENTINEL=Opus. Confirm the nightly dollar ceiling.
5. **(B) No-broad-ETF hardness** — keep scoped to the conviction/alpha sleeve only (Leadership sleeve unchanged)? Confirm the hard wall.
6. **(C) Concentration target** — `MAX_NAMES=12`, `MIN_CONVICTION=60`. (Quorum is conjunctive — no `QUORUM_MIN` to tune.) Confirm 12.
7. **(D) PM-Conviction mandatory** — confirm a champion is a *required* arm of the quorum, not one vote of N.
8. **(E) Brain-book guardrails** — Brain books get self-mirror (P1) + Risk-Officer exits (P4) only; full funnel stays Flagship-first. Confirm.
9. **(F) Significance method for CIO authority changes** — reuse `predictions.py` Newey-West HAC + clustering, `MIN_N=12`. Confirm the effective-n bar for *authority* (vs confidence) changes.
10. **(G) Watchlist size cap — DECIDED** `MAX_WATCH=40`, TTL 20/10 td, lowest-scored eviction. Confirm.
11. **(H) Self-mirror Phase-1 seat set — DECIDED (default)** inject into FORGE + SENTINEL + Brain Opus prompts in P1; extend to Strategist/Technician/PM-Conviction in P3+. Confirm the P1 set.
