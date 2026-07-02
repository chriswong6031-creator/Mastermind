# MASTERMIND v2 — Target Architecture

**Composed from the five judged cluster syntheses, grounded against the audit (research/MASTERMIND_PROBLEM_AUDIT_FOR_FABLE.md), DOCTRINE.md, and /tmp/mm_audit/synthesis.md. Where clusters overlapped or judges hedged, decisions are made and marked DECIDED.**

The one-sentence destination: **a portfolio of seven mandate-orthogonal books driven off a single regime frame, where deterministic engines own sizing through one composable brake stack that can only shrink on bad/missing data, one LLM seat holds bounded additive authority earned through shadow grading, and a benchmark ledger makes "held defensives and did nothing" a creditable, promotable strategy.**

---

## 1. The v2 daily decision spine (22:40 UTC build, end to end)

### Stage 0 — Data gates (owner: Wave 0, in flight)
`lenses.py` fail-closed with freeze semantics; `macro_refresh` multi-anchor; stockdata publish-gap surfacing; `record_run` wiring. **The v2 invariant established here governs every later stage: missing or stale data may coarsen identity, freeze the book, or shrink size — it may NEVER un-cap, raise authority, or flip direction.** Specifically: no stockdata → no new conviction opens; `size_authority='up'` requires a real extension/trend row AND `n_scored ≥ 2`; stale `sector_cycles.json` (>3–5 trading days by `meta.asOf`) → cycle levers inert; missing regime confidence fields → budget midpoint (today's behavior); peer-book firm data absent → per-book caps only (never uncapped); peer data stale-but-present → used as-is (last-known ≈ fail-closed).

### Stage 1 — Regime/risk frame (NEW: `brain/regime_frame.py` — the single reader)
Replaces all 13 regime slice sites (8 brain-side field-slices + the 5 byte-identical `_regime_dict()` copies at `bot/autonomous.py:449`, `bot/etf.py:650`, `bot/heavyweight.py:545`, `bot/hk.py:576`, `bot/china.py:568`), guarded by a golden-output test asserting `lens_row()` is byte-identical to today's 3-field dict so LLM prompts don't drift. It exposes:
- `frame()`: quad, quad_name, liquidity_overlay **plus** confidence, transition_state, contradicting legs, flip_margin, flag_confidence_decay.
- `cycles()`: reads `vendor/macro/site/sectordata/sector_cycles.json` with the freshness gate. **DECIDED (merging the additive-mind and regime-spine clusters' different keying):** the sizing key is the derived `late_cycle` boolean — `phase ∈ {Peak, Downturn} AND osc_slope < 0 AND pos ≥ 70` — because `now.signal`/`now.action` are verifiably inconsistent (XLK SELL/HOLD; XLV None/'TAKE PROFITS'). `action ∈ {SELL, REDUCE}` survives only as an **exit-path feed**, never a sizing input. The additive-mind cluster's phaseLabel-keyed clamp is subsumed by this: `regime_frame.cycles()` is the ONLY consumer of the file; the leadership brakes read its output, they do not load the JSON themselves.
- `cycle_defensive_candidates`: published for the additive seat (clean ownership; no synthetic-corroboration intake hacks).
- Nowcast: z-scored MAX-aggregated, **advisory lens row only** until it passes the AUC>0.55 gate vs realized SPY 5d forward drawdown on 2011–2026; killed to lens-only if it disagrees on <10% of days or never during drawdowns.
- Fragility: `brain/macro_risk.risk_state` gains the dwell state machine — escalate instantly; de-escalate needs 3 sessions below the lower deadband (caution exit 0.28, risk_off exit 0.50) + 2–5 session cooldown; HARD CLAMP blocking de-escalation if a severity≥2 tripwire fired within 2 sessions; max-dwell auto-release; degrade-to-stateless on corrupt state file. The 07-01 CAUTION→RISK_ON-on-the-crash-day flip becomes structurally impossible.
- `fused_risk` consumed as risk prior (in-flight; do not duplicate).

### Stage 2 — Sleeve budgets (ONE equation — the anti-double-count decision)
**DECIDED:** the offense-brakes cluster's `lead_budget` flex and the regime-spine cluster's `budget_scale` are the **same signal** (confidence × transition) applied to the same budget; shipping both double-counts. v2 has exactly one budget function, owned by `regime_frame`:

```
lead_budget = clamp( 0.40 + 0.20 · clamp(confidence,0,1) · T · F ,  0.40, 0.60 )
  T = {WEAKENING: 0.6, ROLLING/DETERIORATING: 0.4, else 1.0}
  F = 0.75 if flip_margin < 0.15 else 1.0
  missing fields → midpoint 0.50
```
Then `lead_budget ×= posture_mult` (posture governor, hard-clamped inside [0.40,0.60], dark until armed — §measure-learn). Conviction budget takes the regime-spine multiplicative damps (lone-source ×0.75, late_cycle-sector ×0.8, queue-wide ×0.9 when the budget term is <0.75). Freed weight is always **sized cash** (doctrine A6), never redistributed. All constants live in `config/doctrine.yml` tagged `(unverified-prior)`, sized/re-sized by the E5 regression; if confidence×WEAKENING shows no forward-drawdown power, flatten the confidence term to a small constant and keep only the transition term.

### Stage 3 — Selection
- **Leadership:** `_SHORTLIST` in `portfolio/conviction.py:19` DIES. Regime seed = basket order-layer leaders restricted to sectors whose cycle phase ∈ {Trough, Recovery, Expansion}, with the liquidity floor preserving the shortlist's data-coverage intent. Legs tagged `sleeve='leadership'`.
- **Conviction:** standouts funnel through fail-closed `lenses.full()` multi-lens confluence (Wave 0). Graded `ext_mult` enters conviction sizing.
- **Defensive candidates:** **DECIDED — one canonical generator, three consumers.** NEW `portfolio/defensive_candidates.py` = union of `defensive_playbook.favor` + `regime_frame.cycle_defensive_candidates` (Bottoming/FreshBuy) + `us_standouts` bottoming board; equal-weight prior, weights frozen until predictions-ledger revival (#8) resolves ≥12 theses; `get_quote`-priced so `price=None` hits the existing w≤0 drop guard. Consumed by (a) the judgment PM's champion pool, (b) the contingent DEF_SLEEVE rotation floor, (c) the benchmark ledger's regime-conditional basket. One map — no drift between what the PM sees, what the floor buys, and what the bogey measures.

### Stage 4 — Judgment layer (the additive mind, when armed)
`brain/judgment_book.py` + `brain/pm_conviction.py`: PM sees the full frame, sleeve-tagged legs **including leadership**, defensive candidates, and its own regime-conditional `self_mirror` track record. Authority over leadership legs is strictly **DROP + reallocate-to-cash/defensive** (never conviction-weight survivors — preserves the equal-weight rank-IC≈0 invariant). Required `submit_book` fields `own_more / own_less / not_holding_should`, each `not_holding_should` emitting a `_shadow_entry` + 21d falsifier so rotation calls are Brier-graded without trades. **Pre-committed E1-failure branch:** if the shadow PM echoes the engine, ship `portfolio/rotation.py` — deterministic DEF_SLEEVE (max 0.35, sized off fragility magnitude + confidence/transition, budget drawn from the SAME de-gross that shrinks leadership — one budget equation, no double-claim), `theme_id='DEFENSIVE_<archetype>'`, `DEF_SLEEVE_MAX=0` restoring today's book byte-for-byte; an armed PM then picks WHICH defensives but never undercuts the floor.

### Stage 5 — Gates (all subtract-only; unchanged in spirit)
Gate Officer, Committee, Research Paper, SENTINEL, hard vetoes, D1–D6 detectors (in-flight #31). None gain additive authority.

### Stage 6 — Brakes and caps (the composition order — memorize it)
1. **Per-leg:** `apply_leadership_caps()` (shared, config-driven from `doctrine.yml`) = MIN(graded extension schedule off `etf_board.etf_trend` pct_vs_200d — outage-proof — , cycle multiplier); `late_cycle` halves legs to 0.5·lw (never zero, never a flip; unmapped tickers un-shrunk — fail-open on mapping only removes a shrink). Parabolic hard veto retained.
2. **Book:** `portfolio/sleeves.enforce_book_caps` — leadership name-cap exemption (lines 30–31) DIES, replaced by broad-index allowlist (SPY/QQQ/VTI/RSP/IWM/DIA at 0.15) vs 0.08 for everything else — **SMH goes to 0.08, no carve-out (DECIDED, architectural over surgical)**; cluster pass aggregating by `fragility_chain.cluster_id()` (3-tier: chain membership → GICS sector from stockdata → singleton) at 0.40 book / 0.35 semis, pro-rata scale-down, breaches emitted `kind:'cluster'` so D6 fires. `theme_id` becomes display-only. `bot/etf.py` G5 refactors to read the same shared cluster config (one definition firm-wide; drift-check test; do NOT restructure the working ETF book).
3. **Firm:** `portfolio/firm_exposure.headroom(cluster_id, book_id)` from other books' published `latest.json`; each US book's finalize clamps its own contribution to firm cluster 0.30 / name 0.10 under the deterministic 22:40 sequential order. Never-raise. (Note: sequential order means earlier books claim headroom first — acceptable because Flagship builds first by design and per-book caps already bound any single book; revisit only if the ledger shows systematic starvation of later books.)
4. **Gross:** `eff_cap = min(state_cap, severity_cap)` — severity ladder sev2→0.70, sev3→0.55 — with `bot/derisk.py`'s held filter extended to `{conviction, leadership}` so a cut actually cuts the 40–60% sleeve. This is the ONLY complete fix of the verified 07-01 no-op.
5. **Guard rails against the stack itself:** calm-tape invariance test (conf>0.55, STABLE, low-z nowcast → byte-identical full budgets); post-all-levers offensive gross ≥ 0.5·lead_budget unless the parabolic hard veto fired; over-de-gross tripwire alerting if offensive gross <0.30 in a high-confidence non-extended tape.

### Stage 7 — Execution
`paper_account` unchanged (buy-queue-while-closed semantics kept). Close-reason taxonomy (Wave 0) makes rotation vs risk-exit distinguishable. Overnight: flagship + heavyweight added to `overnight.py._RUNNERS` with `directive=` threading (the deterministic derisk hook at overnight.py:85 already exists). Wake triggers appended to `gate.state_signature`: quantized confidence bucket, transition bucket, NEW contradicting leg, confidence drop ≥0.15, severity tripwire — a WEAKENING flip wakes a same-day rebuild.

### Stage 8 — Marking (NEW: `portfolio/marks.py` — the single marking layer)
Logged source precedence Polygon-EOD → Yahoo-parquet → last-good-carry with `stale_days`, NEVER avg_cost; ONE pinned adjusted series per name per renorm window (kills the total-return-vs-price bias). One union prices dict marks ALL 7 books including Self-Directed (`self_directed.mark_nav` via the existing `_daily_mark_job` seam). Carry shadows use frozen-targets (yesterday's positions) — the only variant that survives the `shadow_books` `targets or _has_inputs` guard.

### Stage 9 — Attribution
`brain/attribution.py` gains allocation + cash_timing terms alongside the per-name Brinson selection term, reconcile-asserted to active return (no 'inactivity' inside the identity; do-nothing carry delta reported as an adjacent line). `_seat_polarity` extended (builds on in-flight #17) to credit `own_less` / no-add-to-extended.

### Stage 10 — Adaptation
NEW `brain/benchmark_ledger.py` (single owner of renorm + benchmark battery: SPY, regime-conditional defensive basket, self-directed, carried shadows; all books renormed to $1.00 at common max-inception on one price source). Brier grading of the PM's three-question fields. Calibration extended to all books. NEW `brain/posture_governor.py` (name avoids the existing `brain/posture.py`): leadership_mult step 0.05, hysteresis = 3 consecutive same-sign weekly reviews AND HAC-significant with effective_n ≥ 8 (below n: mult=1.0), EMA decay to 1.0 on sign flip, shrink-fast/restore-slow dead-band, `MASTERMIND_POSTURE_ADAPT` default OFF. Registry lifecycle → CIO display-only kill/promote recommendations; KILL never automated at paper-n.

### Module disposition summary
- **DIE:** `brain/panel.py` (already deleted in-flight — do not resurrect), `_SHORTLIST`, the five `_regime_dict` copies, `theme_id` as cap key, leadership name-cap exemption, `'exited (left book)'` undifferentiated close, the stateless risk-state flip path, Heavyweight's `_flagship_universe` hard gate (demoted to fallback + soft tie-break).
- **NEW:** `brain/regime_frame.py`, `portfolio/marks.py`, `portfolio/defensive_candidates.py`, `brain/benchmark_ledger.py`, `brain/posture_governor.py`, `portfolio/rotation.py` (contingent), `cluster_id()` in `portfolio/fragility_chain.py`, shared `apply_leadership_caps()`.
- **CHANGE:** `phase2.py`, `sleeves.py`, `derisk.py`, `macro_risk.py`, `gate.py`, `overnight.py`, `firm_exposure.py`, `heavyweight.py`, `attribution.py`, `cio.py`, `conviction.py`, `judgment_book.py`, `pm_conviction.py`, `self_mirror.py`, `lenses.py` + `position_log.py` (Wave 0).
- **SURVIVE unchanged:** `paper_account.py`, `phase1.py`, thesis/Brier ledgers, `gate_officer.py`, `committee.py`, `research_paper.py`, ETF book G-cap semantics, the asymmetric law, equal-weight leadership, ½-Kelly subtract-only conviction sizing.

---

## 2. The book portfolio — mandates, benchmarks, kill/promote

**DECIDED: no new books.** v2 is the existing seven plus shadow books (judgment shadow, carry shadows, benchmark baskets under `data/shadow/benchmarks/`). Orthogonality is enforced by mandate + cluster caps + the benchmark ledger's cross-book active-return correlation, not by proliferation.

| Book | v2 mandate | Benchmark | Kill/promote hook |
|---|---|---|---|
| **Flagship** | Engine-gated core: leadership (regime seed, braked, capped) + conviction (fail-closed confluence) + sized cash; contingent DEF_SLEEVE. The reference expression of doctrine. | SPY in risk_on; `max(SPY, defensive basket)` when risk_state≠risk_on OR transition=WEAKENING | Judgment layer promotes INTO it per §promotion rule; posture governor trims within band |
| **flagship_judgment (shadow)** | The one additive mind: drop/reallocate + defensive picks + three-question calls | Same conditional bogey as Flagship | Promote after (1) Phase-1 replay passes, (2) E1 divergence (>2% gross AND ≥1 defensive/contrarian take), (3) rolling 4-wk shadow NAV beats live engine in WEAKENING/CAUTION slices. Echo → Phase-4 deterministic floor ships instead |
| **ETF** | Capped systematic rotation; the proven-brakes control book. Its G-caps are the firm's reference implementation (shared cluster config) | SPY + its own carried shadow | Control book — killed last; if Flagship-with-brakes can't beat it, that IS the finding |
| **Autonomous US** | Free-form LLM exploration; only book allowed high discretionary turnover + overnight re-decides; graded on residual vs Flagship (it must earn its keep by being DIFFERENT — it sold SMH on 07-01 when Flagship couldn't) | SPY + active-return correlation vs Flagship | Registry probation on 2 consecutive HAC-significant losing reviews OR correlation-to-Flagship persistently >0.8 (it's then a noisy mirror) |
| **Heavyweight** | Firm best-ideas concentration: universe = union of published books' `latest.json` (Flagship/ETF/Autonomous; Self-Directed only after RC6 makes it publish), one-name-per-cluster_id, firm headroom, 5–50%/8-name rails, LLM proposes only; mirror as fallback when <4 fundable names | SPY AND Flagship (concentration must beat the thing it concentrates) | Kill the firm-universe flag if cluster-overlap-with-Flagship doesn't fall vs the mirror shadow in the 2–4wk A/B |
| **China / HK Brains** | Regional mandates unchanged; consume `regime_frame.lens_row()` (golden-identical), gain book cluster caps | CSI300 ETF / 2800.HK Tracker | Same registry lifecycle |
| **Self-Directed** | User's manual book — becomes a **first-class measured benchmark book**: marks.py NAV, published exposure into firm_exposure, in `cio._NAV_BOOKS` and the calibration battery. The bot never trades it | SPY | Never killed — it is the yardstick. Its regime-conditional alpha feeds the posture governor |

The set is a portfolio of strategies because: mandates differ on axis (systematic-braked / free-form / concentrated / regional / manual-defensive), the firm cluster cap makes correlated max-conviction structurally impossible, Heavyweight is re-pointed from amplifier to cross-book concentrator, and the benchmark ledger's registry gives each book a lifecycle (active → probation → retired=display-only) with human-executed kills.

---

## 3. The LLM seat map

Deterministic engines own sizing everywhere; this separation is load-bearing and unchanged. Seats:

| Seat | Inputs | Incentive/grading | Authority |
|---|---|---|---|
| **PM-CONVICTION + STRATEGIST** (judgment book) | Full `regime_frame` (confidence/transition/contradicting/cycles), sleeve-tagged legs incl. leadership, `defensive_candidates`, regime-conditional self_mirror track record | Regime-conditional bogey: `max(SPY, XLP/XLV/USMV/SGOV)` ONLY when risk_state≠risk_on OR WEAKENING (plain SPY in risk_on so cash-hoarding still loses); Brier on own_more/own_less/not_holding_should; #17 polarity credits own_less. Falsifier: if the conditional bogey changes no calibration multiplier vs raw-SPY, the incentive leg is inert — rework before promoting | **ADDITIVE (bounded):** drop any leg incl. leadership; reallocate to cash/defensive; propose names (shadow-graded first); pick defensives above the rotation floor. Never conviction-weights leadership survivors, never undercuts the floor, never exceeds engine caps |
| **Gate Officer** | Candidate book + gates | Existing | Subtract-only; never injects |
| **Committee** | Adjudication rounds | Existing (+ #11 same-model-adversary label) | Subtract-only; never rescues |
| **Research Paper** | Per-buy thesis | Brier-ledgered falsifiers | Confirm/veto/size; never rescues a hard veto |
| **SENTINEL** | Book + tape | Brier | Bear-only adversary — kept; **DECIDED: no new bull-adversary seat.** The PM with defensive candidates IS the additive counterweight; a second additive seat doubles cost and blurs accountability |
| **Autonomous / China / HK Brains** | `lens_row()` + own mandate | NAV vs book benchmark + calibration | Free-form within book caps + firm headroom |
| **Heavyweight proposer** | Firm best-ideas union + frame | NAV vs SPY and vs Flagship | Proposes only; one-per-cluster + rails enforced deterministically |
| **CIO** | benchmark_ledger battery + registry | — | **Display-only** meta-allocation + kill/promote recommendations; humans execute |
| **Risk Officer / Governor** | Frame + book | Existing | Subtract-only |
| ~~Panel adjudicator~~ | — | — | DEAD (deleted in fix/bot-orphans-arming); not resurrected |

Exactly one seat is additive, its additive calls are Brier-graded via shadow entries before they ever trade, and its authority is bracketed above by engine caps and below by the deterministic rotation floor. That is RC1's fix without RC1's failure mode inverted (an LLM that can pump the book).

---

## 4. Interdependency / build-order constraints the masterplan must respect

1. **Wave 0 (fail-closed data gating) precedes everything.** Every downstream lever assumes inputs can't silently fail open; the E1 shadow graded against a fail-open engine measures noise. Also resolve audit §8 open questions first: stockdata deploy-vs-engine (gates C1 severity) and production env confirmation via `ps eww` (decides whether the subtract-only teeth even run — everything assumes FAST_DERISK/MACRO_RISK are live).
2. **Phase 0 risk spine ships first among new work** (dwell state machine + severity ladder + held-filter→{conviction,leadership}): pure risk reduction, no forward-edge proof needed, and the budget/brake work is meaningless while the state cap can print 1.0 on a crash day.
3. **`regime_frame` mechanical refactor (golden-test, zero behavior change) precedes all new consumers** — budget equation, cycles wiring, wake triggers, nowcast lens. One reader before new fields, or the 13-site drift recurs.
4. **Deterministic brakes precede arming the judgment shadow.** G4/G5 port + shortlist kill + cluster caps must land first so E1 divergence measures *judgment* value, not brake value; and the PM must be graded against the braked engine it would replace.
5. **Cap layers build inward-out:** per-book cluster caps (Phase 1) → firm-wide headroom (Phase 2, needs books' published exposure to be bounded and trustworthy) → Heavyweight firm-universe (Phase 3, consumes the Phase-2 headroom function). Each layer's replay proof gates the next.
6. **marks.py + renorm precede attribution precede posture governor** — and the Phase-1 renorm is the cheapest kill-switch in the plan: **if the ~+1.45% Self-Directed lead vanishes under one-price-source renorm, cancel the measure-learn downstream (governor, conditional bogeys) — but NOT the brake/firebreak work, which is justified independently by the 07-01 replay evidence.**
7. **Predictions-ledger revival (#8, in-flight) precedes any learned weights** in `defensive_candidates` (frozen equal-weight until ≥12 resolved theses) and any calibration-driven seat multipliers.
8. **Overnight inclusion of Flagship/Heavyweight requires the severity ladder first** — a directive with no teeth is churn; the derisk hook at overnight.py:85 exists but must cut both sleeves.
9. **Shared cluster config lands before (or with) the sleeves cluster pass and the etf.py G5 refactor** in one PR, with the drift-check test — never two cluster definitions live simultaneously.
10. **Do not duplicate in-flight work:** #8, #17, #31, #2, #11, fused_risk prior, Wave 0 items. `brain/panel.py` is deleted on fix/bot-orphans-arming — no v2 design may depend on it.
11. **Dashboard wishlist items are wants, not blockers.** Every design degrades to today's behavior (or better) when a wishlist item never ships; nothing in the build order waits on the dashboard sessions.

---

## 5. Top 5 architecture risks and mitigations

1. **The compounding-shrink stack under-invests permanently** (budget flex × ext_mult × late_cycle × cluster caps × severity cap × posture mult) — the bot beats the drawdown but loses every risk-on tape and the user's goal fails. *Mitigations:* one budget equation (confidence/transition consumed exactly once — the double-count is designed out, not tested out); strict composition order (budget→leg→book→firm→gross); calm-tape byte-identical invariance test; offensive-gross floor ≥0.5·lead_budget; over-de-gross tripwire; per-lever ablation falsifiers with pre-committed kill criteria (cycle spread ≤0 → demote to display; no confidence forward-drawdown power → flatten T; SOXX-week drawdown saving costs >⅓ of trend-week upside → loosen schedule, keep architecture).
2. **The additive mind never materializes** — the PM echoes the engine (E1 fails) and v2 is just better brakes, leaving RC5's rotate-gap open. *Mitigations:* the pre-committed Phase-4 deterministic DEF_SLEEVE branch makes E1 failure a cheap, strong outcome (rotation ships anyway, evidence-gated); incentive falsifier (inert conditional bogey → rework incentives before promotion); required three-question fields force rotation *calls* to exist and be graded even when trades don't.
3. **Wrong dashboard signals steer the bot** — a wrong-but-fresh Goldilocks, a mislabeled cycle phase, noisy confidence. *Mitigations:* the degrade-never-raise invariant everywhere (wrong signals can shrink size, never flip direction or un-cap); freshness gates render stale artifacts inert; extension brake reads etf_board (outage-independent of stockdata); nowcast and cycles enter as advisory lenses and are promoted to caps only through backtest gates; the offline-only correlation validator audits cluster config without a noisy corr matrix ever keying a live cap.
4. **Learning from noise at paper-n** — the governor or calibration adapts to a measurement artifact (the SD lead itself is unproven pending renorm). *Mitigations:* the Phase-1 renorm kill-switch; effective_n≥8 + HAC significance + 3-review hysteresis + bootstrap noise/wrong-benchmark injections passed BEFORE arming; governor default-OFF, clamped inside the doctrine band, shrink-only in the defensive direction; honest stated expectation that the loop is correctly INERT for weeks — the near-term deliverable is seeing and crediting the gap, with the smallest safe corrective armed and waiting.
5. **Single-owner-module sprawl and config drift** — regime_frame, cluster config, defensive map, marks each become new single points of failure; constants drift between doctrine.yml, etf_strategy.yml, and code. *Mitigations:* golden-output tests on every refactor (lens_row byte-identity; DEF_SLEEVE_MAX=0 and flags-off byte-identity); drift-check tests comparing shared constants across consumers; all thresholds in doctrine.yml tagged `(unverified-prior)` per the falsifiability culture; cluster_id stability falsifier (0 relabels over 20 historical build dates absent a config edit); degrade-to-stateless/degrade-to-midpoint on corrupt state so no new module can hard-fail a build.

---

## 6. Consolidated dashboard wishlist (union of all five clusters, deduped; bot degrades gracefully without every item)

1. **Regime contract:** frozen schema + `schema_version` for `latest.json` — confidence (documented scale), transition_state, contradicting legs, flip_condition.
2. **sector_cycles contract** at `site/sectordata/`: guaranteed `now.{phase, phaseLabel, pos, osc_slope, above200d}` for all 11 sectors; fix or deprecate the inconsistent signal/action pair with documented precedence; add hysteresis / `n_days_in_phase`; extend coverage (or a documented ETF→sector rollup, e.g. `factors_cycles.json`) to basket-block instruments SMH/QQQ/IGV/MTUM.
3. **stockdata:** guaranteed publish with publish-gap surfacing; `factors.sector` presence-or-explicit-absent sentinel; coverage extended to leadership tickers (the G4/G5 brake silently no-ops on an empty price store).
4. **Per-instrument `pct_vs_200dma` published independent of site/stockdata** (etf_board-adjacent), plus a true crowding percentile field (pctile_252d is selection, not crowding).
5. **One consistent `asOf`/freshness field** across all artifacts — true data timestamp, not build time.
6. **`fused_risk` emitted consistently** with its degraded flag and confidence.
7. **Down-day alpha + 60d leadership-cluster correlation per ETF** (feeds the extension/orthogonality lenses and the offline cluster validator).
8. **Canonical ticker→cluster_id and defensive-sector→ETF map contracts** shared with `firm_exposure._sector_of`; coarse per-ETF sector weights so the validator can audit thematic-ETF cluster placement.
9. **Adjusted EOD closes for SPY + the defensive ETF set** (XLU/XLV/XLP/XLF/USMV/SGOV/TLT) with a documented adjustment convention, on origin/main.
10. **Keep the driver→favor mapping fresh** in the defensive playbook feed.

---

**The destination in one paragraph:** every regime bit the dashboard publishes flows through one reader into one budget equation and one brake stack that can only shrink; every book is bounded by name→cluster→firm caps keyed on real correlation structure, marked by one pricing layer, and graded against a bogey that a defensive do-nothing book can win; exactly one LLM seat can add — bounded, shadow-graded, and pre-committed to a deterministic fallback if it turns out to be an echo; and the adaptation loop finally sees the book that's winning, credits inactivity, and adjusts posture only when the statistics earn it. Wrong dashboard data degrades size. Nothing flips direction. The doctrine's asymmetric law, equal-weight leadership, ½-Kelly subtract-only sizing, and Brier falsifiability culture survive intact — v2 is the doctrine finally *binding*, plus the one additive mind the doctrine always implied but never armed.