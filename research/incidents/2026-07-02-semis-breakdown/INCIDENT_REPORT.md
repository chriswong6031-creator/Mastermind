# INCIDENT: Semis/Memory/AI-Infrastructure Breakdown — 2026-07-02

**Severity:** Critical (capital loss + systemic decision failure across all US books)
**Status:** Post-mortem complete · W0–W3 deployed to production 2026-07-02 22:33 UTC (during incident) · W4 merged same day · Incident Wave (W-I) opened
**Appendix (this dir):** `timeline.md` (signal-vs-action ledger), `counterfactual.md` (fixed-stack replay), `signals.md` (signal autopsy + specs), `ops.md` (deployment forensics + runbook), `synthesis.md` (full causal analysis)

---

## 1. What happened

Semiconductors, memory, neoclouds and AI-buildout infrastructure broke down while every Mastermind US book was 60–90% offensive in exactly that cluster, with **$0.00 in healthcare/staples/utilities** across all four books. Over the window each book lagged its own SPY benchmark by **−1.6 to −2.5 points in one week** (~−$82k firm-wide vs bench). The user's self-directed defensive book (XLV/XLU/XLP/XLF) — positioned on fund flows, defensive RS, the Goldilocks→stagflation turn, and reading "expanding liquidity" as stress-driven — returned **XLV +7.8% / XLU +3.9% / XLP +2.4%** over the same window while SMH did −7.3%.

The three most damning behaviors, all documented in the appendix with verbatim Brain reasoning:

1. **The severity-2 tripwire fired EVERY session 06-26→07-02 and cut nothing.** Worse, the stateless risk scorer *raised* the gross cap 0.70→1.00 on 07-01 — the exact day SOXX printed −6.4% — so the correctly-firing alarm was sized against a cap that no longer bound anything.
2. **Autonomous whipsawed the epicenter:** sold 41.8 SMH @ $626.54 on 07-01, then **bought back 41 SMH @ $605.70 on 07-02 into the breakdown**, reasoning from "Goldilocks + SMH #1 RS." Its intake queue "corroborated" more semis because the funnel samples what's already leading.
3. **The ETF book sold $57.6k SGOV and re-risked into equities on 07-02** while its own fragility panel read `"CONCENTRATED — 3 of 6 markets are one bet … do NOT spend cash into strength."` It spent cash into strength.

Also: production Flagship structurally could not sell (pre-W0 code) — its NAV literally logs the same number two sessions running; Heavyweight force-rebuilt into a 89.8%-gross megacap-tech book at the top because its universe is hard-gated to Flagship's rotation.

## 2. Why — the causal chain (with fix status)

| # | Link | Status |
|---|---|---|
| 1 | **Bad regime label held.** Q1 Goldilocks survived because the growth axis is a sign-vote democracy of clipped ±1 votes padded by lagging monthlies; sticky-CPI votes sit on the wrong axis; 7-day hysteresis adds stickiness. Regime confidence had collapsed 0.68→0.18 into 06-25 — the label didn't move | **NOT FIXED** (dashboard-side; handoff §6) |
| 2 | **Transition flag regressed WEAKENING→STABLE on the eve of the break.** The transition classifier is a memoryless flag counter — two flags rolled off their windows and it silently reset to "all clear." No ratchet | **NOT FIXED upstream**; consumer-side dwell machine (W1) is deployed |
| 3 | **"Expanding liquidity" was hollow.** Net-liquidity was flat for 5 sessions; the +68.9bn RoC print was 20-day base-effect noise; decomposition = TGA drawdown against RRP drained to **$6.4bn** — mechanical/stress plumbing, not Fed easing. The overlay has no quality dimension | **NOT FIXED** (classifier spec ready, §5.2) |
| 4 | **Confidence-blind consumers.** Old stack hardwired lead_budget 0.50 at confidence 0.327 | **FIXED + DEPLOYED** (budget() = 0.449 today; note T-term is neutered by link #2 — garbage-in partially survives) |
| 5 | **Neutered tripwire** (see §1.1) | **FIXED + DEPLOYED** (eff_cap=min(state, sev)=0.70 covering leadership; dwell machine forbids the crash-day flip) |
| 6 | **No exit machinery / Flagship can't sell** | **FIXED + DEPLOYED** (W0 sells-first queue) |
| 7 | **No concentration brakes** (SMH 16.2% single name; firm-wide SMH 0.33; heavyweight 89.8% one cluster) | **FIXED + DEPLOYED** (G4 extension clamp — SMH at +46% vs 200d → 0.08; cluster 0.35; firm caps 0.10/0.30 default-ON) |
| 8 | **No defensive capability** (favor list advisory-by-design; zero healthcare ever) | **FIXED IN W4** (merged; DEF_SLEEVE default-0 pending arming decision; throttled by links 1–3 — see §4) |
| 9 | **Deployment lag.** Production ran a pre-W0 branch through the entire incident; four merged waves sat undeployed | **Remedied today; the CLASS is not** (deploy-lag tripwire, §5.5) |
| 10 | **Blinded feed.** `data/risk_radar` — the dashboard's own validated "CAUTION: growth scare / defensive rotation (91/100)" — was stripped from the bot's sparse vendored set by the R2 migration. `data/china_regime` is still missing | **FIXED at data level** (eb9fd0b); consumer lands with W4; china_regime still open |

**The one-paragraph verdict:** the dashboard published *both* stories — the wrong one (Goldilocks/expanding/risk-receding) on the label plane the bot trusts, and the right one (Tech Topping/SELL at RS-rank-1, Utilities Bottoming/BUY, radar CAUTION growth-scare 91/100, defensive RS crossover) on planes the bot either didn't read or couldn't act on. The bot had no disagreement machinery, no exit machinery, no defensive machinery, and its one working alarm was sized against a cap that evaporated at the worst moment. Meanwhile every fix for the response side existed on master, undeployed.

## 3. The counterfactual — what the fixed stack does with the same inputs (appendix `counterfactual.md`)

- **Autonomous's +$24.8k SMH rebuy today is REJECTED** (firm SMH pile-up 0.33 vs 0.10 cap).
- SMH clamps 16.2%→8% (−$80.8k autonomous alone); book-cap trims: autonomous **−$104k**, etf **−$157k**, heavyweight **−$255k**; severity eff_cap 0.70 cuts heavyweight another −$196k of gross. Honest firm-level SMH reduction ≈ −$220k.
- **No new semis leg can even seed**: XLK reads `late_cycle=True` (Topping, pos 80.8, osc_slope −18.4); XLV and XLU read `entry_favored=True` — the machine now sees exactly what the user saw.
- The ETF book's SGOV→SPY re-risk **would not fire** (the fixed stack only shrinks on these inputs).
- **Honest caveats:** the severity cap does nothing for books already under 0.70; a name-cap false-positive would trim SGOV (T-bill ballast) −$39.2k — ballast must be cap-exempt (W-I item); and W4's DEF_SLEEVE, fed today's wrong risk_on/STABLE labels, throttles to ~7% of NAV when a truthful caution/WEAKENING read would size ~23%. **Rotation capability is now built; rotation *fuel* (regime truth or a second opinion) is the remaining gap.**

## 4. Would Mastermind make the right decision even with accurate data?

Split honestly:
- **Deterministic stack (deployed):** yes for *response* — with a truthful caution/WEAKENING read, budget→0.40 floor, DEF_SLEEVE→~23%, severity 0.70/0.55, caps force semis disposal. This is mechanical and now replay-tested (the 07-01 no-op is an executable regression test).
- **Detection:** partially. The tripwires detected stress every day (short gamma, theme-day, vol-hole). What was missing is *synthesis into posture* — that is exactly the W-I nowcast + liquidity-quality + rotation-evidence work (§5).
- **Judgment:** the armed dry-run PM independently trimmed semis and held 29% cash; W4 gives it the whole book, the defensive pool, your book as its named benchmark, and Brier-graded three-questions duties. The shadow A/B decides whether it earns live authority. Foresight of the kind the user exercised — reading liquidity expansion as forced, weeks early — is now partially mechanized (§5.2), partially the judgment seat's job, and the honest residual is: some of it is human edge the system should *follow* faster (the user's self-directed book becomes a first-class benchmark input in W5).

## 5. The Incident Wave (W-I) — new builds this incident mandates

1. **Distribution Escalator** *(the exit answer, reconciled with both negative results — the refuted cycle-phase veto and the macro repo's exit-rule NO-GO)*: (a) holdings-level distribution tells (>95th pctile crowding + 2W-StochRSI topped + defensive-RS crossover) escalate the **severity ladder** (+1 ⇒ sev-3 cap 0.55) — evidence routed through already-validated machinery, no new prediction claim; (b) an MTF-confluence trim ladder (mirror of the validated buy gate) runs in **shadow** until it clears the same walk-forward that killed the exit rule; (c) cap-forced structural trims re-checked every session (already deployed — F2 showed caps, not signals, do most of today's work).
2. **Liquidity-Quality classifier**: quantity RoC + composition share (dWALCL vs −dTGA/−dRRP) + RRP-buffer flag (<$100bn ⇒ TRUE today at $6.4bn) + credit/NFCI overlay → `expanding` reclassifies as **STRESS-expansion** (today's honest label: "neutral, hollow"). Shrink-only input to `budget()`.
3. **Price-action regime nowcast** (the second opinion): defensive-vs-offensive RS differential + SMH MTF-MACD + breadth. Back-dated: soft signal **06-24**, hard composite **07-01** — the day before the SMH rebuy. Shrink-only; can never add risk. This also unthrottles the DEF_SLEEVE when the label lies.
4. **Rotation-evidence budget term**: when defensive RS + cycles + radar agree (all three did this week), shift the offense/defense mix — raise the DEF_SLEEVE floor, don't just filter entries.
5. **Deploy-lag tripwire + incident replay harness**: alert when production trails master >24h; this incident's inputs become permanent executable fixtures (the 07-01 cap-flip and 07-02 SMH-rebuy-rejection tests already exist; extend to the full week).
6. **Small sharp fixes:** ballast assets (SGOV/BIL) cap-exempt; `data/china_regime` restored to the sparse set; radar consumer wired on master.

## 6. Dashboard handoff (owned by the macro-dashboard sessions — bot degrades gracefully meanwhile)

1. Regime growth axis: magnitude-weighted votes (not ±1 democracy); sticky-CPI on the correct axis; confidence should gate the label's display prominence.
2. Transition classifier needs a **ratchet** (WEAKENING cannot reset to STABLE while confidence is falling and flags re-armed within N sessions).
3. `liquidity_overlay` needs the quality dimension (§5.2 spec; series all on disk) — "expanding" from TGA-drawdown-at-empty-RRP must not render as benign, on macro.html or in the contract.
4. The "risk receding" badge + pullback-odds: reconcile with the risk_radar's own CAUTION growth-scare (91/100) — two products on one page telling opposite stories.
5. sector_cycles: this incident validated it (Topping/SELL on the RS leader, continuously through the window) — protect its freshness and keep `signal`/`action` consistent.

## 7. How Mastermind learns from this (the standing answer)

- **This incident is now executable memory**: replay fixtures assert the stack cuts/rotates correctly on these exact inputs, forever.
- **W5 (next)** closes the grading loop: every book marked against SPY *and the user's defensive basket* on one price source; regime-conditional calibration so a seat that rotated defensive into this week scores *well*; allocation-aware attribution so "held XLV, avoided semis" is creditable; the posture governor adapts the offense budget only when statistics earn it.
- **The three-questions duty (W4)** forces every future build to answer *what should we own that we don't* — with each answer Brier-graded against a 21-day falsifier whether or not it trades.
- The organizational fix: one regime frame, one cluster identity, one defensive pool, one benchmark ledger — and a deploy-lag alarm so a fixed system is never again watching from the repo while production bleeds.
