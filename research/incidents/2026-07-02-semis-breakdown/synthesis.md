# MASTERMIND INCIDENT 2026-07-02 — DECISION-GRADE SYNTHESIS
*(Composed from F1 timeline, F2 counterfactual replay, F3 signal autopsy, F4 deployment forensics. All figures at today's marks; SMH @ $605.70; each book NAV ≈ $980–988k.)*

---

## 1) ROOT-CAUSE CHAIN

1. **Bad regime label held.** Q1 Goldilocks survived the rotation because the growth axis is a sign-vote democracy of clipped ±1 votes (`engine/axes.py:19-62`) padded by lagging monthlies (payrolls/indpro/gdpnow), sticky-CPI votes on the wrong axis, and `hysteresis_days=7` adds a week of stickiness. → **NOT-FIXED-ANYWHERE** (dashboard-side; see §4).
2. **Transition regressed WEAKENING→STABLE on the crash day.** `engine/transition.py:85-112` is a memoryless flag counter — two flags rolled off their boolean windows and the state silently reset to "all clear" (history: n=2 WEAKENING 06-25/26 → n=0 STABLE 07-01). No ratchet exists. → **NOT-FIXED-ANYWHERE** upstream; **FIXED-ON-MASTER** at the consumer only (W1 dwell state machine makes the *bot's* CAUTION→RISK_ON flip impossible, but it dwells on the feed it's given).
3. **"Expanding liquidity" was a stress artifact read as benign.** +68.9bn 20d RoC on 07-01 was one-day base-effect noise (net-liq flat at ~$5,858bn for 5 sessions); decomposition = −TGA drawdown against RRP drained to **$6.4bn**, not Fed easing. The overlay has no quality dimension. → **NOT-FIXED-ANYWHERE** (classifier spec exists, §3.3).
4. **Confidence-blind consumers.** Old stack hardwired lead_budget 0.50 regardless of conf 0.327/flip_margin 0.05. → **FIXED-ON-MASTER** (W2 `regime_frame.budget()` → 0.449 today; note the fragility damp F=0.75 does all the work — the transition term T=1.0 is neutered by link #2's STABLE regression: garbage-in survives the fix).
5. **Neutered tripwire.** Sev-2 fired every session 06-26→07-02 and cut nothing; BUG-A took `min` of state-cap only, and the state flip *raised* the gross cap 0.70→1.00 as SOXX printed −6.4%. → **FIXED-ON-MASTER** (W1 `eff_cap=min(state,sev)=0.70` + dwell). Honest caveat from F2: this lever is weak — it caps gross, not composition; it does nothing to autonomous (0.64) or flagship (0.25) and never targets SMH concentration.
6. **No exit machinery.** Production carried the literal `# market closed → queue buys ... No sells.` (`bot/phase2.py:976-978` on the old branch); flagship froze at the same NAV two sessions running. → **FIXED-ON-MASTER** (W0 sells-first queue, `paper_account.py:842-899`) **and now deployed** (F4).
7. **No concentration brakes.** SMH 16.2% single-name, firm-wide SMH 0.3316, heavyweight 89.8% gross in one cluster. → **FIXED-ON-MASTER** (W2 G4 extension clamp — SMH pct_vs_200d 46.2% > 40 → 0.08; W3 name/cluster/firm caps default-ON).
8. **No defensive capability.** Zero mechanism to own XLV/XLU/XLP even with the dashboard printing HC Trending / Utilities Bottoming-BUY. → **FIXED-IN-W4-PENDING** (defensive_candidates + DEF_SLEEVE) — but throttled to 7% of NAV by links #1–3 (signal 0.20 vs ~0.65 on a correct WEAKENING+caution read).
9. **Deployment lag.** Production ran a pre-W0 branch through the entire incident; W0–W3 only went live today 15:20 (`prod-deploy-w0w3`, PID 84236 — F4). The *lag* is remedied; the *class of failure* (nothing alarms when prod trails master by 4 merged waves during a sev-2 streak) → **NOT-FIXED-ANYWHERE** (§3.7).
10. **Blinded feed.** `data/risk_radar` was stripped from the vendored sparse set, so the bot never saw the dashboard's own "CAUTION: growth scare / defensive rotation (91/100)" radar. → **FIXED** at data level (eb9fd0b, today); the consumer (`_load_risk_radar` in risk_prior) is **FIXED-IN-W4-PENDING** (master's test marks it importorskip). `data/china_regime` remains sparse-missing → **NOT-FIXED-ANYWHERE**.

---

## 2) QUANTIFIED FIXED-vs-NOT MATRIX vs THE USER'S COMPLAINTS

Damage baseline (F1): every book lagged its own SPY bench by **−1.57 to −2.50 pts in one week** ≈ **−$15k to −$25k per book, ~−$82k firm-wide**; unrealized on the semis cluster: auto −$9.9k, etf −$7.5k, flagship −$19.8k. User's book over the same window: **XLV +7.8% / XLU +3.9% / XLP +2.4%** vs SMH −7.3%.

| Complaint | Status | Counterfactual under fixed stack (F2) |
|---|---|---|
| **Offensive posture into a breakdown** | PARTLY FIXED (master, deployed) | budget 0.50→0.449; eff_cap 0.70 forces heavyweight −$196k, etf −$10.6k; nothing for books already <0.70 — composition untouched by this lever |
| **No rotation** | W4-PENDING, throttled | DEF_SLEEVE = 7 legs incl. **XLV+XLP** at 7.1% of NAV (~$70k/book); would be ~23% (~$226k) on a correct WEAKENING read — rotation capability exists, rotation *fuel* (regime truth) does not |
| **No disposal of 99th-pctile semis** | FIXED-ON-MASTER | SMH ext-clamp 16.2%→8% (auto −$80.8k; hw SMH −$69k + XLK halved); book-cap trims: auto **−$104k**, etf **−$157k**, hw **−$255k** (Σ ≈ **$516k**); firm caps honestly bring firm-SMH 0.3316→0.10 ≈ **−$220k** (the $1.09M zero-out is a double-counting upper bound); **auto's +$24.8k SMH buy today is rejected outright** |
| **Capital protection** | FIXED-ON-MASTER (W0 sells + caps), weak on state | flagship's queued rotation now executes sells-first; ETF's SGOV→SPY re-risk **would not fire** (fixed stack only shrinks) — though note the SGOV-as-ballast false-positive: name cap wrongly trims T-bills −$39.2k (W5 flag) |
| **Bubble foresight (stress-liquidity read)** | NOT-FIXED-ANYWHERE | No liquidity-quality classifier; the 07-01 "expanding" buy-day was base-effect noise. Spec ready (§3.3) |
| **Unused dashboard signals** | PARTLY FIXED | sector_cycles now consumed (W2 `cycles()`: XLK late_cycle=True blocks all new semis legs; XLV/XLU entry_favored=True — exactly the user's book); risk_radar restored to sparse set today but has **no consumer on master**; regime feed itself still trusted uncritically |
| **Zero healthcare** | W4-PENDING | XLV and XLP both in today's candidate pool; sized ~1%/leg at current throttle |

---

## 3) NOT-FIXED-ANYWHERE — RANKED, WITH THE CONCRETE BUILD

**3.1 Regime-label quality (the root).** Dashboard-side (§4). Bot-side hedge until then: never let a single-plane label be load-bearing — see 3.4.

**3.2 Distribution/exit discipline — the honest reconciliation.** Two verdicts constrain this: the **cycle-phase veto/exit on held leaders was walk-forward REFUTED** (masterplan "will not do": entry-tilt only) and the macro repo's own **EXIT-rule NO-GO** (EMA8 tail-flag only). So no general "sell signal" gets built. What IS defensible, narrowly:
- **(a) Severity escalation from holdings-level distribution tells.** Don't predict exits — feed the *validated* lever. Holdings at >95th pctile 60d-return + 2W-StochRSI topped + defensive-RS crossover ⇒ severity +1 on the existing tripwire ladder (sev3→0.55 cap). This routes distribution evidence through machinery that already passed replay, instead of a new exit rule.
- **(b) MTF-confluence trim ladder — PENDING walk-forward, not shipped on faith.** Mirror of the validated buy gate (MACD-2D×StochRSI-3D) as a *trim-to-cap* trigger, ¼-position steps, held names only. Must clear the same walk-forward harness that killed the exit rule; pre-registered falsifier before any capital.
- **(c) Cap-forced structural trims (already the real teeth).** F2 showed caps, not signals, do today's work: extension clamp is not held-exempt, so a 46%-over-200d SMH gets cut to 8% mechanically, no forecast required. Extend: caps re-checked on every session, trims execute via the W0 sell path.

**3.3 Liquidity-quality classifier.** F3's spec, series all on disk: quantity RoC (existing) + RRP-buffer flag (`RRPONTSYD` < $100bn → today TRUE at $6.4bn) + composition share (dWALCL vs −dTGA/−dRRP → today mechanical) + stress overlay (HY OAS 20d/z, NFCI direction). `expanding` reclassifies to **STRESS-expansion** if buffer-exhausted OR mechanical OR credit-confirming. Today's honest label: "neutral, hollow." Feeds `regime_frame.budget()` as a shrink-only multiplier.

**3.4 Price-action regime nowcast, shrink-only.** Defensive-minus-offensive 20d RS differential + %sectors>20d/50d SMA (F3 script computes both from vendored yahoo). When the nowcast contradicts the feed (defense leading while label says Goldilocks), it can only *shrink* budget/tighten caps — never re-risk. This is the bot's insurance against link #1 forever.

**3.5 Rotation-evidence budget term.** The defensive-RS crossover date becomes an explicit input to `budget()` and to DEF_SLEEVE's fragility_signal, so the sleeve isn't throttled solely by the (corruptible) risk-state dwell + confidence terms. Would have moved today's def_budget toward the ~0.23 correct read.

**3.6 Incident/case-study replay harness.** Freeze 06-26→07-02 inputs (regime feeds both planes, cycles, macro_risk states, boards, fills) as a fixture; assert the full fixed stack's outputs (SMH buy rejected, eff_cap 0.70, DEF_SLEEVE ≥ X, sells execute). Every future wave must keep this incident un-repeatable, the same pattern as the 07-01 CAUTION→RISK_ON replay falsifier already in W1.

**3.7 Deploy-lag tripwire.** Daily job: running PID's branch/commit vs origin/master merged waves; if prod trails by ≥1 merged risk-wave while tripwire severity ≥2, page + badge the admin console. This incident's signature — three sev-2 days on pre-W0 code — becomes structurally impossible to miss.

**3.8 Housekeeping with teeth:** SGOV/cash-equivalent allowlist in `enforce_book_caps` (stop trimming ballast, F2 item 4); add `data/china_regime` to `_SPARSE_PATHS`; ship the risk_radar *consumer* (currently data with no reader on master).

---

## 4) DASHBOARD HANDOFF (Macro Dashboard repo)

*Note for the lead: F3's transmission truncated before its numbered §4; this list is reassembled from F3's body findings and verified file:line cites — treat as the handoff content, not a verbatim quote.*

1. **Transition state machine has no ratchet** (`engine/transition.py:85-112`): memoryless flag counter; WEAKENING→STABLE on flags rolling off windows, not on the rotation reversing. Build: hysteresis/decay on de-escalation (mirror of the bot's dwell), e.g. WEAKENING may only clear after N sessions with n=0 *and* the cyc/def ratio slope back positive.
2. **Growth axis sign-vote democracy** (`engine/axes.py:19-62`): clipped ±1 votes let shallow fresh crossings equal deep moves; lagging monthlies (payrolls/indpro/gdpnow) pad the axis during fast rotations. Build: magnitude-weighted votes + a fast/slow split so `growth_cyclical_defensive` can veto.
3. **Sticky-CPI misrouted**: `sticky_cpi_direction` votes on the inflation axis only; never touches the growth/stagflation call.
4. **Liquidity overlay is quantity-only** (`engine/regime.py:100-113`, `engine/inputs.py:279`): ship the quality classifier of §3.3 at the source (RRP-buffer, composition decomposition, OAS/NFCI co-check); the 07-01 "expanding" print was one-day base-effect noise on ffilled-flat WALCL.
5. **Two regime planes disagree and both publish**: `latest.json` (07-01: STABLE/expanding, flag_gex False) vs `regime_history.parquet` (06-26: WEAKENING/contracting) vs same-day `macro_risk state.json` rationale asserting "flag_gex and flag_confidence_decay both firing." One plane must be canonical; history must not end 4 sessions before latest.
6. **risk_radar green badge is stale/mislabeled** vs its own escalating internals: headline "risk receding" while `drawdown h21` rose 0.16→0.19 (lift 1.07x) and the CAUTION growth-scare/defensive-rotation read (91/100) was live from 06-26. Also `cap_leadership: False` at caution — threshold review: this incident is the argument that caution should cap leadership.
7. **Contract for consumers**: publish `regime_confidence`, flip_margin, transition flag count, and the liquidity-quality label in `latest.json` so `regime_frame` downstream never has to infer them.

---

## 5) HOW MASTERMIND LEARNS FROM THIS (the answer for the user)

**What W5 would have graded here.** Today, "every book lost to SPY while you made +7.8% in XLV" is a fact I computed by hand — the system cannot compute it about itself (masterplan §0: "Until W5's benchmark ledger exists, this metric cannot even be computed honestly. That is itself finding #1"). W5's ledger fixes exactly the blindspots this incident exposed: P1 grades seats on binary beat-SPY-in-21d so **holding cash or defensives generates zero grading rows** — defense is *defined* as failure in the one regime it wins (finding #17); all 11 shadow policies are the same momentum book, so the value of doing nothing — or of holding your XLV/XLU/XLP book — is **unpriceable** (P2, finding #18). Post-W5: a regime-conditional bogey, cash-credit in grading, and a defensive/do-nothing shadow book that would have printed, in the ledger, "the defensive shadow beat every live book by ~10pts this week." The transition-call itself gets a Brier score (`forward_brier()` currently has zero callers — finding #13): the 07-01 STABLE call would be graded a confident miss and would move the calibration weights that feed `budget()`.

**The case-study harness (§3.6)** turns this week into a permanent regression test: frozen inputs, asserted outputs. The 07-01 CAUTION→RISK_ON flip already lives as a W1 replay falsifier; this incident becomes the second, larger one. Learning here is literal — code that reproduces the failure cannot merge.

**What remains judgment, honestly.** The user's edge this week was a *thesis* — recognizing expanding liquidity as stress-driven, reading fund flows + defensive RS as a Goldilocks→stagflation turn — formed before any of the system's slow legs rolled. §3.3–3.5 mechanize the *detectable* parts (RRP exhaustion, composition, RS crossover), and would have shrunk risk and funded a real defensive sleeve. But no planned build originates that thesis. That is what the W4 judgment pipe is for: a bounded LLM seat (shadow A/B 2–4 weeks, promotion gated on beating the engine book in WEAKENING/CAUTION slices, pre-committed deterministic DEF_SLEEVE fallback if it echoes). The honest division of labor after the full program: **the machine's job is to make a wrong regime label survivable — caps, shrink-only nowcasts, sells that execute, a defensive sleeve that exists; the human's (or the judgment seat's) job is to be early. This week the human was early and the machine made earliness unactionable. Master + W4 fixes the second half; nothing yet fixes a feed that lies, which is why §4 goes to the dashboard and §3.4's shrink-only nowcast is the standing insurance.**

---

**Key artifacts:** `/Users/chriswong/Documents/Cluade/Mastermind/data/macro_risk/2026-07-01/state.json` (crash-day risk_on/1.00), `data/*/fills.jsonl` (auto +$24.8k SMH 07-02; ETF SGOV→SPY/RSP/XLI 07-02), `bot/phase2.py:976-978` (old no-sell branch), `paper_account.py:842-899` (W0 sells-first), `brain/regime_frame.py:475-476` (budget), `portfolio/sleeves.py:195-210` (G4/late_cycle), `portfolio/firm_exposure.py:499` (firm caps default-ON), commit `eb9fd0b` on `prod-deploy-w0w3` (risk_radar sparse restore; deployed 15:20, PID 84236), `Macro Dashboard/engine/transition.py:85-112` + `engine/axes.py:19-62` + `engine/regime.py:100-113` (handoff targets), `/tmp/mm_incident/*.py` (replay scripts).