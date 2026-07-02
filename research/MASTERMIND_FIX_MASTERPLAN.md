# MASTERMIND FIX MASTERPLAN — Fable program doc

**Date:** 2026-07-02 · **Owner:** Fable (this doc is the canonical program state; update the Status log every wave)
**Companion docs (same dir):** [MASTERMIND_PROBLEM_AUDIT_FOR_FABLE.md](MASTERMIND_PROBLEM_AUDIT_FOR_FABLE.md) (the 47-problem Opus audit) · [MASTERMIND_V2_ARCHITECTURE.md](MASTERMIND_V2_ARCHITECTURE.md) (the full target architecture — read it before building any wave) · `mastermind_problem_register.json` (machine-readable ranked register, 74 items) · `mastermind_reaudit_2026-07-02.json` (re-audit: 3 empirical resolutions, 20 verified novel problems, 14 unverified candidates, impact grades)

**Provenance:** Opus first-pass audit (60 agents, 47/51 confirmed) → Fable re-audit (38 agents: empirical resolvers + 8 novel hunters + impact graders + adversarial verifiers) → 5 judge-panel solution designs (3 angles each) → architecture synthesis (21 agents) → this plan. ~9M subagent tokens across the program.

---

## 0. The goal, stated as a falsifiable metric

> **A Brain book beats BOTH (a) the Self-Directed defensive book (XLU/XLV/XLF/XLP) and (b) SPY, on a rolling 60-day basis, on one price source from a common inception, with max drawdown no worse than 1.25× the defensive book's** — while every risk control demonstrably binds and every data outage degrades size instead of inflating confidence.

Until W5's benchmark ledger exists, this metric cannot even be computed honestly. That is itself finding #1 about the program.

### What we now know empirically (re-audit resolutions — full detail in the JSON)

1. **S3 (defensive book wins) is REAL but is an N-of-3-trading-day result.** Renormalized to $1.00 at the latest common inception (2026-06-23) on one price source (parquet ends 06-26): Self-Directed **+2.92%** (zero drawdown) vs heavyweight +0.75%, etf −0.02%, autonomous −0.47%, flagship −0.49%, SPY −0.63%. Decomposition: it is ~all **allocation** (XLV +5.4%, XLU +2.5% vs SMH −1.7%); churn drag is only ±0.1–0.8pp and *helped* two books. → The fix priority is the Brains' **allocation capability** (defensive rotation), not turnover throttling. Books are 8–12 days old; no durable-superiority claim yet.
2. **The sector-cycle VETO is refuted.** Walk-forward 2015–2026 on the bot's actual leadership rule: baseline CAGR 8.17%/Sharpe 0.61 → Peak-halve 6.54%/0.57 → Peak-exclude 4.77%/0.47. Topping leaders keep leading; the veto forfeits the premium the sleeve exists to harvest. The only defensible cycle use: **Trough/Recovery entry preference on NEW additions** + the Bottoming list as a **defensive candidate source**. Also note: SPY B&H (Sharpe 0.91) and the defensive 4-ETF book (0.76) both beat every leadership variant (≤0.61) — the top-RS rotation sleeve *itself* underperforms; brakes alone don't fix that.
3. **Judgment-echo risk is LOW.** The armed dry-run genuinely diverged (dropped IREN, trimmed SMH/XLK/MTUM ~2pts each, added 18 single names, held ~29% cash, gross 0.704). The persona already rewards cash and forbids ETF-core. But the seats are fed the confidence-blind funnel and can't see the defensive benchmark → arm it WITH the three prompt edits (W4).
4. **stockdata provenance:** macro-repo **publish gap** — 1,684 files tracked on local main, **0 on origin/main** (the deploy target the bot syncs). Dashboard-side fix owned by the dashboard sessions; bot side is fail-closed as of W0.
5. **Production env (dumped from the live PID):** `FAST_DERISK=1, MACRO_RISK=1, SELF_MIRROR=1, SELECTION_EXPLORE=1, EXPLORE_EPS=0.05, RESEARCH_LLM=0`; `FLAGSHIP_JUDGMENT` and `RISK_GOVERNOR` unset. The subtract-only teeth run; the additive mind is dark.

---

## 1. Ranked problem register (top 30 of 74 — full list in `mastermind_problem_register.json`)

Score = 2·pnl_impact + urgency (0–30). Sources: `audit-47` (first pass, verified), `novel` (re-audit, adversarially verified), `UNVERIFIED` (re-audit hunters, verification pending — treat as candidate until checked).

| # | Score | Problem | Src | Wave |
|---|---|---|---|---|
| 1 | 28 | **flagship-sell-asymmetry-zero-sells-ever** — only sell-capable call is inside `if _market_open:`; cron always fires closed; account has never sold (41 pos, $14.72 cash, fictional vs 29-name target) | novel ⚠CRIT | **W0** |
| 2 | 28 | **missing-stockdata-degenerate-confluence** — data outage → confluence=1.0, sa='up', vetoes=[] | audit ⚠CRIT | **W0** ✅ |
| 3 | 25 | sector-cycles-ignored — *rescoped by walk-forward: entry-tilt + defensive source ONLY, veto refuted* | audit | W2/W4 |
| 4 | 25 | flagship-positions-pre-rotation-state — account ≠ published book (same family as #1) | audit | **W0** |
| 5 | 24 | flagship-all-flags-off — judgment/contrarian seats dark in prod (PID-confirmed) | audit | W4 |
| 6 | 24 | leadership-sleeve-no-extension-veto — 40–60% NAV ungated; SMH 12.5% = top risk line | audit | W2 |
| 7 | 24 | self-directed-outperformance-gap — no defensive rotation capability anywhere | audit | W4 |
| 8 | 23 | theme-cap-defeated-by-per-name-theme-id — correlated SMH/XLK/MTUM never sum; 42.6% IT, breaches:[] | audit | W3 |
| 9 | 23 | macro-risk-state-flip-on-day-of-soxx-crash — stateless scorer; cap 0.7→1.0 on crash day; tripwire neutered | audit | W1 |
| 10 | 22 | no-defensive-hypothesis-generator — favor list advisory-by-design, never bought | audit | W4 |
| 11 | 22 | all-conviction-1-of-1-altdata — companion of #2 | audit ⚠CRIT | **W0** ✅ |
| 12 | 21 | P1 — seats graded on binary beat-SPY-in-21d; cash generates zero grading rows | novel | W5 |
| 13 | 21 | NEW-A — loop/ discovery+promotion is write-only; `forward_brier()` returns None, zero callers | novel | W5 |
| 14 | 21 | NEW-B — desk_levers claims "SURVIVES multiple-testing (DSR≥0.95)" while its own effective_n=1 → the haircut was a no-op | novel | W5 |
| 15 | 21 | CN-HK-1 — HK book runs on HKEX-closed holidays (mainland calendar + stale-Yahoo blindspot) | novel | W1 |
| 16 | 21 | brain-books-settle-at-mid-session — Brain fills at ~11am intraday marks; flagship at true day-open (two price semantics in one settle job) | novel | W1 |
| 17 | 21 | P1-fixed-spy-benchmark — defense is *defined* as failure in exactly the regime it wins | novel | W5 |
| 18 | 21 | P2-no-donothing-or-defensive-shadow — all 11 shadow policies are the same momentum book; value of inactivity unpriceable | novel | W5 |
| 19 | 21 | extension-trim-in-etf-not-flagship — G4/G5 proven, never ported | audit | W2 |
| 20 | 21 | leadership-blind-to-regime-quality — budget hardwired 0.50 midpoint | audit | W2 |
| 21 | 21 | extension-hard-veto-parabolic-only — the +30%-vs-200dma withhold is CODED (phase2.py:416) behind an unset flag | audit | W2 |
| 22 | 21 | caution-gross-cap-conviction-only — leadership (52.5% NAV) exempt from the risk cut | audit | W1 |
| 23 | 20 | flagship-no-independent-mind (umbrella of 5/10) | audit | W4 |
| 24 | 20 | s4-rotation-trigger — binary confluence defeats hysteresis (companion of #2) | audit | **W0** ✅ |
| 25 | 19 | judgment-book-diverges-but-not-executed — the dry-run evidence for W4 | audit | W4 |
| 26 | 18 | autonomous-sold-SMH-while-flagship-couldn't — overnight off-ramp excludes Flagship/Heavyweight | audit | W4 |
| 27 | 18 | P6-four-us-books-cannot-buy-defense (universe restriction) | UNVERIFIED | W4-verify |
| 28 | 18 | P-NEW-1 — risk_prior consumes shadow fused_risk while ignoring non-shadow risk_radar (caution) | UNVERIFIED | W1-verify |
| 29 | 18 | P-NEW-2 — bot ignores `us_standouts.gate_go`; buys names its own dashboard flagged NO-GO | UNVERIFIED | W1-verify |
| 30 | 18 | NEW-SIZE-1..4 — sizing pipeline: renorm erases initial-size haircut; vol-sizing inert (universe-key mismatch); equal-weight collapse; risk weights overwritten by research size_mult | UNVERIFIED | W2-verify |

Also W0-adjacent: **SBI-1 — the test suite deletes production `bot.db`** (root cause of the "empty runs table"/daily-full-rebuild mystery; conftest isolation shipped in W0), SBI-3 nav_history insertion-order dupes (W5 marks.py), `{_DB}` root file (solved: dev-REPL artifact, code correct — delete the file at will).

### Do NOT build (refuted/decided — stop re-proposing these)
- **Cycle-phase veto/exit on held leaders** (walk-forward refuted; entry-tilt only).
- **A bull-adversary counterpart to SENTINEL** (the PM with defensive candidates IS the additive counterweight).
- **New books** (orthogonality via mandates + cluster caps + benchmark ledger, not proliferation).
- **Rewiring every subtract-only seat to bidirectional** (weakens the load-bearing discipline; exactly ONE additive seat).
- **Un-vendoring / floating the macro read** (pin to published contract stays; freshness gates + fail-closed instead).
- **Heavyweight hard-gate removal without replacement** (it becomes firm best-ideas union with cluster caps, W6).

---

## 2. Target architecture (1-paragraph summary — full spec in MASTERMIND_V2_ARCHITECTURE.md)

Every regime bit flows through **one reader** (`brain/regime_frame.py`) into **one budget equation** and **one composable brake stack** that can only shrink on bad/missing data; every book is bounded by **name→cluster→firm caps** keyed on real correlation structure; **one marking layer** (`portfolio/marks.py`) prices all 7 books; a **benchmark ledger** makes "held defensives and did nothing" creditable and promotable; and **exactly one LLM seat is additive** (PM-CONVICTION+STRATEGIST) — bounded, shadow-graded, with a pre-committed deterministic DEF_SLEEVE fallback if it echoes. The doctrine's asymmetric law, equal-weight leadership, ½-Kelly subtract-only conviction sizing, and Brier falsifiability culture survive intact. **v2 is the doctrine finally binding, plus the additive mind the doctrine always implied but never armed.**

Invariant to memorize (governs every wave): **missing/stale/wrong data may coarsen identity, freeze the book, or shrink size — it may NEVER un-cap, raise authority, or flip direction.**

---

## 3. The waves

Model-tier routing per house policy: **Sonnet** for well-specified code, **Opus** for design-sensitive implementation/judgment, **Fable** for orchestration, adversarial review, and anything touching the sizing spine. Every wave ends with: full pytest, a replay/ablation falsifier where stated, adversarial review of the diff, squash-merge to master same-day.

### W0 — Stop the bleeding (SHIPPED this session, branch `fable/w0-critical-fixes`)
| Item | Status |
|---|---|
| Fail-closed coverage gating: `data_degraded`, `size_authority='insufficient_data'`, **freeze semantics** (held+degraded → hold, never liquidate), >80%-degraded circuit breaker with loud `data_health` | ✅ + tests |
| Close-reason taxonomy: `position_log.update(close_reasons=…)` + phase2 3-bucket classifier (rotation vs floor vs hard-exit) — S4 can never recur as a mystery | ✅ + 15 tests |
| Staleness tripwire: 3 real anchors (us_standouts `as_of` / regime `date` / sector_cycles `meta.asOf`), min-date semantics, `anchors_report()`, `data_gaps` incl. permanent stockdata-gap surfacing | ✅ + 22 tests |
| hk.py docstrings; record_run regression test (wiring was correct — tests were wiping prod bot.db) | ✅ |
| **Sell-path fix**: queue sells while closed, fill sells-before-buys at day-open, real-NAV sizing (kills #1 + #4; account converges to target over subsequent builds) + conftest `store._DB` isolation (kills SBI-1) | 🔄 in flight this session |

### W1 — Risk spine + execution integrity (next session; mostly Sonnet, Opus on the state machine)
1. **`brain/regime_frame.py`** — the single reader replacing 13 slice sites; golden-output test (`lens_row()` byte-identical to today's 3-field dict). *(Sonnet, mechanical-with-golden-test)*
2. **Fragility dwell state machine** — escalate instantly, de-escalate needs 3 sessions below deadband + cooldown; HARD CLAMP if severity≥2 tripwire within 2 sessions; degrade-to-stateless on corrupt state. Replay falsifier: 07-01 CAUTION→RISK_ON flip becomes impossible. *(Opus)*
3. **Severity-decoupled tripwire**: `eff_cap = min(state_cap, severity_cap)` (sev2→0.70, sev3→0.55) + derisk held-filter → `{conviction, leadership}`. Replay: 07-01 tripwire now cuts. *(Sonnet)*
4. **Gate wake triggers**: confidence bucket, transition bucket, new contradicting leg, severity tripwire appended to `state_signature`. *(Sonnet)*
5. Execution integrity: Brain-book settle at true day-open (unify with flagship polygon semantics); HK calendar fix (HKEX holidays); `{_DB}` file deletion. *(Sonnet)*
6. **Verify-first batch** (adversarial verification then fix if confirmed): P-NEW-1 (risk_radar vs fused_risk), P-NEW-2 (`gate_go` respect — likely a 5-line fix), P-NEW-3 (entry_signal/stops ignored — wire stops into the position ledger at minimum). *(Opus verify, Sonnet fix)*
7. **stockdata source decision**: keep fail-closed + surface the gap (default), and hand the publish-gap to the dashboard sessions (chip already spawned). Optional flag-gated fallback: read the local sibling macro repo with provenance stamps — only if the publish gap persists >1 week. *(decision recorded here; no code unless triggered)*

### W2 — Brakes on offense (Sonnet builds, Opus reviews thresholds, Fable signs off sizing spine)
1. Shared **`apply_leadership_caps()`**: G4 overextension (pct_vs_200d>40 → clamp to 0.08) + G5 cluster pre-cap, ported from the proven `bot/etf.py`, config-driven from `doctrine.yml`, applied to leadership legs after phase2:271. Replay: SMH-week drawdown delta.
2. **Graded extension schedule** on conviction: ≥30% vs 200dma → initial-size only; ≥45% → no adds; parabolic → hard veto (unchanged). Arm the already-coded timing withhold (phase2.py:416) after confirming its flag & behavior.
3. **Kill `_SHORTLIST`** → regime seed: basket order-layer leaders filtered to cycle phase ∈ {Trough, Recovery, Expansion} (the walk-forward-defensible entry tilt), liquidity floor preserved.
4. **ONE budget equation** (architecture §Stage-2): `lead_budget = clamp(0.40 + 0.20·conf·T·F, 0.40, 0.60)`; constants tagged `(unverified-prior)`; E5 regression to size the haircut; **calm-tape invariance test** (conf>0.55 & STABLE → byte-identical budgets) + offensive-gross floor ≥0.5·lead_budget + over-de-gross tripwire.
5. Verify-then-fix the **NEW-SIZE quartet** (sizing pipeline order: vol-sizing → sector cap → no silent renorm past caps; fix universe-key mismatch; preserve initial-size haircut through renormalization).

### W3 — Firebreaks that bind (Opus design, Sonnet build)
1. **`cluster_id()`** in `fragility_chain`: 3-tier (chain membership → GICS sector → singleton); stability falsifier (0 relabels over 20 historical builds absent config edit).
2. **Cluster pass in `enforce_book_caps`**: 0.40 book / 0.35 semis, pro-rata scale-down, `kind:'cluster'` breaches → D6. `theme_id` becomes display-only.
3. **Leadership name-cap exemption DIES**: broad-index allowlist (SPY/QQQ/VTI/RSP/IWM/DIA @0.15); **SMH → 0.08, no carve-out**.
4. **Shared cluster config** + `bot/etf.py` G5 refactor to read it + drift-check test (one definition firm-wide; ship in one PR).
5. **`firm_exposure.headroom()`** binding at each book's finalize: firm cluster 0.30 / name 0.10, never-raise, sequential 22:40 order (Flagship claims first by design).

### W4 — The additive mind (Opus/Fable; the judgment-critical wave)
*Precondition: W2 brakes landed (E1 must measure judgment value, not brake value).*
1. **Pipe leadership legs through `judgment_book`** (today it sees only conviction — the placebo hole). PM authority over leadership: **DROP + reallocate-to-cash/defensive only** (never re-weight survivors).
2. **`portfolio/defensive_candidates.py`** — one canonical generator: `defensive_playbook.favor` ∪ cycle-Bottoming/FreshBuy ∪ us_standouts bottoming board; equal-weight prior frozen until ≥12 resolved theses; three consumers (PM champion pool, contingent DEF_SLEEVE, benchmark basket).
3. **Three prompt edits** (from the echo analysis): (a) full `regime_frame` fields into pm_conviction/strategist/get_regime slices; (b) the Self-Directed defensive basket as an explicit named benchmark the PM must beat; (c) de-anchor: engine weights move to the end of the payload.
4. **Three-questions duty**: required `own_more / own_less / not_holding_should` fields on submit_book; each `not_holding_should` emits a shadow entry + 21d falsifier → Brier-graded rotation calls without trades.
5. **Arm `MASTERMIND_FLAGSHIP_JUDGMENT=1` as a shadow A/B, 2–4 weeks.** Promotion rule: Phase-1 replay passes AND divergence (>2% gross AND ≥1 defensive/contrarian take) AND rolling 4-wk shadow NAV ≥ engine book in WEAKENING/CAUTION slices. **Pre-committed echo branch:** ship deterministic `portfolio/rotation.py` DEF_SLEEVE (max 0.35, fragility-sized, `DEF_SLEEVE_MAX=0` = today's book byte-for-byte).
6. **Overnight off-ramp**: `_RUNNERS += {flagship, heavyweight}` + `directive=` threading into phase2.run (requires W1 severity ladder so the directive has teeth).

### W5 — Measure & learn (Opus; the honesty wave)
1. **`portfolio/marks.py`** — single marking layer: Polygon-EOD → Yahoo-parquet → last-good-carry with `stale_days`, NEVER avg_cost; marks all 7 books incl. Self-Directed; kills SBI-3/nav-dupes.
2. **`brain/benchmark_ledger.py`** — renorm battery (common inception, one source): SPY + **regime-conditional bogey** (`max(SPY, defensive basket)` only when risk_state≠risk_on OR WEAKENING — plain SPY in risk_on so cash-hoarding still loses) + Self-Directed + carry shadows. **Kill-switch: if the SD lead vanishes under renorm, cancel the governor/conditional-bogey downstream (keep W2/W3 — justified independently).**
3. **Attribution**: allocation + cash-timing terms beside per-name Brinson (builds on in-flight #17); reconcile-asserted.
4. **Shadow arms**: do-nothing/carry-forward + defensive-basket policies in shadow_books/desk_ab (prices the value of inactivity).
5. **Grading fixes**: P1 (binary 21d beat-SPY → add risk-adjusted + cash-credit terms; regime-conditional bogey into calibration); extend `cio._NAV_BOOKS` to all books.
6. **Research integrity**: NEW-B (desk_levers honest effective_n — cross-family correlation on RETURN series, or stop printing "SURVIVES"); NEW-A decision — wire `forward_brier` promotion or explicitly retire the paper→live promise (no zombie promises).
7. **`brain/posture_governor.py`** — default OFF, ±0.05 steps clamped in doctrine band, effective_n≥8 + HAC significance + 3-review hysteresis; noise-injection tests pass before arming.

### W6 — Book portfolio (Opus design + Fable judgment)
1. **Heavyweight mandate**: universe → union of published books' `latest.json`, one-name-per-cluster, firm headroom, mirror as fallback; kill the flag if cluster-overlap doesn't fall vs the mirror shadow in 2–4wk.
2. **Registry lifecycle**: active → probation (2 consecutive HAC-significant losing reviews OR corr-to-Flagship >0.8 for Autonomous) → retired; CIO recommends, human executes.
3. **CN/HK**: CHINA_FUNNEL_PROFILE=edge-led decision (the validated reversal edge is dark in prod); HK freshness residuals; regional benchmark wiring (CSI300/2800.HK).
4. Self-Directed becomes a **first-class measured benchmark book** (published exposure, in every battery; never killed — it is the yardstick).

### W7 — Acceptance gate (Fable; continuous from W5, decision at +4 weeks of shadow data)
- The §0 metric, computed by the benchmark ledger, per book.
- Per-lever ablation falsifiers with pre-committed kill criteria (architecture §5.1): cycle entry-tilt spread ≤0 → demote to display; no confidence→forward-drawdown power → flatten the budget term; brake-week drawdown saving < ⅓ of trend-week give-up → loosen schedule.
- Judgment promotion / echo-branch decision per W4 rule.
- Program verdict for the user: which books earn capital, which die, what the next program is.

---

## 4. Coordination & guardrails

- **In-flight branch `fix/bot-orphans-arming`** (another session, same repo): predictions revival (#8), attribution counterfactual credit (#17), D1/D2/D4 wiring (#31), armory (#2), same-model-adversary label (#11), fused_risk prior; `brain/panel.py` DELETED. Do not duplicate; expect a small phase2.py merge (their hunk ~:1043, ours ~:799-840 + :946-984).
- **Dashboard sessions own**: regime correctness (Goldilocks-vs-Quad4), signal quality, and the **stockdata publish gap to origin/main** (chip spawned). The bot never blocks on them: every design degrades to today's behavior when a wishlist item doesn't ship. Wishlist = architecture §6 (10 items: frozen regime schema, sector_cycles hysteresis + signal/action precedence, stockdata guarantee, per-instrument pct_vs_200dma, consistent asOf, fused_risk consistency, down-day alpha, cluster-map contracts, defensive-ETF adjusted closes on origin/main, fresh driver→favor map).
- **Test hygiene**: the suite is stateful and order-dependent (pre-existing failures catalogued in the W0 PR body); W1 should add the conftest isolation pattern to every prod-state file the tests touch. Never run the suite in the production checkout until then (it wipes `bot.db`).
- **Restart discipline**: uvicorn runs without `--reload` — merged changes reach production only after `systemctl`/manual restart of the app process, on the checkout it runs from. Coordinate with the other session before restarting.

## 5. Status log

- **2026-07-02 (this session, Fable):** Program created. Re-audit done (20 novel problems verified, 3 empirical resolutions, 47 impact-graded). Architecture + 5 judged solution designs done. **W0 shipped** on `fable/w0-critical-fixes`: fail-closed+freeze, close reasons, tripwire anchors, hk/record_run, phase2 test guards; sell-path fix + conftest DB isolation in flight. Docs: audit, architecture, register, re-audit bundle, this masterplan.
