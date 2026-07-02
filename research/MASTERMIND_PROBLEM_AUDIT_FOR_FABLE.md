# Mastermind Bot — Problem Audit for Fable

**Date:** 2026-07-01
**Repo:** `Mastermind/` (this repo). Macro engine consumed read-only via `vendor/macro` → `vendor/macro_src` (symlink).
**Method:** Adversarial multi-agent audit. 8 subsystem mappers → 51 candidate problems → one independent verifier per problem (instructed to *refute first*) → root-cause synthesis. **47 of 51 problems confirmed or partially confirmed; 4 refuted.** Every claim below carries `file:line` evidence that was independently re-read by a second agent, and the load-bearing facts (missing `stockdata/`, unused `sector_cycles.json`, 100%-invested Flagship, self-directed holdings, the 07-01 rebuild) were hand-verified by the lead.

> **This document is diagnosis, not prescription.** It states what is broken and *why*, links each defect to the symptoms you reported, and hands Fable a ranked, falsifiable test agenda. It does not implement fixes.

---

## 0. TL;DR — the one-paragraph through-line

**Mastermind is a momentum-following, subtract-only filter that trusts an over-optimistic regime label it is architecturally incapable of disagreeing with, has no reasoning seat that can originate a defensive or contrarian trade, and cannot see or learn from the one book that is beating it.** Every reported symptom falls out of that sentence. The offensive sleeve mechanically buys whatever already has the highest relative strength (SMH/semis, 99th percentile, +47% in 60 days) with **no** RSI/MACD/valuation/extension/cycle-phase brake. The concentration firebreaks that *should* stop this are defeated by their own keying. The one reasoning seat that *can* hold cash and rotate defensively (`STRATEGIST + PM-CONVICTION`) exists, works in dry-run, and is **switched off by a default flag**. And the winning defensive book is invisible to every learning loop, so nothing self-corrects.

---

## 1. The two "investigate this" mysteries — resolved (with corrections)

You flagged two things as "weird, investigate." Both resolved, and **both differ materially from the first-pass read** — including my own interim message. Stating the corrections plainly:

### 1.1 S4 — "Flagship closed like all its trades today due to the conviction sleeve"

**What actually happened on 2026-07-01: nothing was liquidated. It was a silent nightly *rebuild rotation* of the target book, and the names it dropped were your diversifiers, not your risk.**

- The nightly build ran the full path: `leadership=4, conviction=15, gross=0.7478, cash=0.2522`, and **queued 15 BUY orders for the 07-02 open** (`data/brain/runs/2026-07-01T22-40-00-741Z…jsonl`). It kept the AI/semis/MAG7 cohort and even opened TSM.
- The "closed trades" you saw were **16 conviction names dropped by the ordinary rebuild reconciliation** — `position_log.update()` closes any prior-book name absent from the freshly-built book with the single undifferentiated string `'exited (left book)'` and **no `reason` field at all** ([portfolio/position_log.py:141-149](portfolio/position_log.py), [bot/phase2.py:805-808](bot/phase2.py)). The dropped names were **ANET, APH, WAB, URI, ACIW, BRC, PH, CSL, VSEC, AIT, ULS, CI, INCY, UNP, AEIS, LII** — i.e. the *industrials / healthcare / cyclical diversifiers*. The extended semis/AI leaders were **retained**.
- It was **not** a risk-director directive: [data/macro_risk/2026-07-01/derisk_flagship.json](data/macro_risk/2026-07-01/derisk_flagship.json) shows `action='hold'`. The severity-2 SOXX tripwire that *did* fire (`"theme day: SOXX -6.4%"`) **cut nothing** — see §1.3.
- Nothing physically sold: with the market closed the paper account only *queues buys* ([paper_account.py:697](portfolio/paper_account.py) "the desk does not sell while shut"); sells happen only in the market-open `rebalance()` path. So the paper account still holds the old names; the *published target book* rotated.

> **Correction to my own interim read:** I initially hypothesized the SOXX tripwire cascaded through a `hard_exit` sweep and closed the book. The verifiers **refuted** that (`held-hysteresis-and-hard-exit-sweep-S4`, `flagship-conviction-closed-soxx-tripwire-not-pm-directive`): 2026-07-01 was a full rebuild, not a carried-forward day, so the hard-exit sweep branch never executed, and the tripwire was a no-op.

**The real problems S4 exposes** are two, and neither is the one you feared:
1. **Observability gap** (`s4-conviction-close-is-silent-rebuild`, medium): routine rotation is indistinguishable from a deliberate risk exit because the position ledger records no reason. You *cannot tell* a rebuild from a stop.
2. **The rotation went the wrong way** (`s4-conviction-sleeve-wholesale-rotation-trigger`, high): the 15 *new* conviction names were all sized at `confluence=+1.00` with **`price=None`** off a *single alt-data lens* — see the critical bug in §3 / RC3. The book churned out real cyclical diversifiers and replaced them with a MAG7/semis basket selected on one political-flow signal with no price data.

### 1.2 S5 — "the macro dashboard is stale/wrong (says Goldilocks Q1; Hedgeye says Quad 4)"

**Correction: the vendored feed is *not* provably stale-on-disk.** `macro_src` HEAD is current and `site/` is re-synced from `origin/main` every 3 hours ([app/scheduler.py:407-408](app/scheduler.py)), and the regime file carries today's date. So "our systems are lagging" is only *partly* the story. The deeper, verified defects are:

- **Confidence-blindness (`intake-regime-nuance-dropped`, `leadership-sleeve-blind-to-regime-quality`, high).** `data/regime/latest.json` *does* carry the danger: `confidence=0.327` (low), `transition_state=WEAKENING`, `flag_confidence_decay=true`, and 3 `contradicting` legs (`growth_cyclical_defensive`, `growth_wei_trend`, `inflation_sticky_cpi_direction`). **Every regime reader drops all of it** and keeps only `{quad, quad_name, liquidity_overlay}` — there are **five byte-identical 3-field `_regime_dict()` copies** ([bot/autonomous.py:449](bot/autonomous.py), [bot/etf.py:650](bot/etf.py), [bot/heavyweight.py:525](bot/heavyweight.py), [bot/hk.py:571](bot/hk.py), [bot/china.py:563](bot/china.py)) plus the strategist and SENTINEL whitelists. The run-gate signature is `quad|band|liquidity|top_sector` ([brain/gate.py:12-14](brain/gate.py)) — so a WEAKENING, low-confidence regime **can neither shrink the offensive sleeve nor wake the book.** The bot is told "Goldilocks" and nothing else.
- **The deterministic risk officer *agrees* it's risk-on** (`macro-risk-state-flip-on-day-of-soxx-crash`, high) — see §1.3.
- **The cycle engine's verdict is published and ignored** (S6, §1.4).

So the honest framing is not "the file is old" — it is **"the bot consumes a wrong-but-fresh top-line label and is blind to the same file's own uncertainty and to the cycle engine that disagrees."** A perfect freshness-tripwire would not catch a wrong-but-fresh Goldilocks.

### 1.3 The risk engine saw the danger — and stood down anyway

The single most damning sequence in the audit (`macro-risk-state-flip-on-day-of-soxx-crash`, high, independently verified):

- On **2026-07-01, the same day SOXX fell −6.4%**, the deterministic `macro_risk` state **flipped CAUTION → RISK_ON**, and `gross_cap` went **0.7 → 1.0** — the hard cash floor was removed on the crash day.
- Cause: the fragility scorer is **stateless, with no hysteresis** ([brain/macro_risk.py:444-464](brain/macro_risk.py)). One benign GEX reading (`dist_to_flip +0.16%`) collapsed the dealer-gamma axis 0.80 → 0.15, dropping total fragility to `0.121` → `risk_on`. It cleared a 4-day caution in a single run.
- The severity-2 tripwire fired correctly (`SOXX -6.4%`), but the fast-derisk cut is **sized to the state's own gross_cap** — now 1.0 — so `if gross(0.215) <= cap(1.0): action='hold'` ([bot/derisk.py:268-271](bot/derisk.py)). **The correctly-fired tripwire de-risked nothing.**
- Meanwhile Heavyweight, reading the same "Goldilocks/neutral-liquidity" label, *bought more semis* ([_pending_decision.json](data/portfolios/flagship_judgment/_pending_decision.json): "pressing the confirmed leadership ETF… the indirect NVDA/AVGO exposure I cannot own directly").

### 1.4 S6 — the cycle engine's conclusions are published, machine-readable, and read by zero bot code

`vendor/macro/site/sectordata/sector_cycles.json` **exists** (1.9 MB, rendered today) and says exactly what you said it says:

| Sector | phase | signal | pos |
|---|---|---|---|
| Technology (XLK) | Peak / **Topping** | **SELL** | 81.6 |
| Health Care (XLV) | Trending | TAKE PROFITS | — |
| Industrials (XLI) | Trending / **FRESH BUY** | BUY | — |
| Financials (XLF) | Trending | — | — |
| Comm/Discretionary/Utilities | **Bottoming** | BUY | 15.5 (XLU) |

`grep -rn 'sector_cycles|phaseLabel|Topping' brain/ bot/ portfolio/` → **zero hits** (`sector-cycles-completely-ignored`, `cycle-signals-not-consumed`, high). The leadership selector consumes `regime.sector_rs` **rank** ([bot/phase2.py:256](bot/phase2.py)) — it ingests *"semis lead"* and is told nothing about *"semis are topping."* A Topping sector (XLK, signal SELL) and a Fresh-Buy sector (XLI) are **indistinguishable** to the selector when both rank high on RS. And the momentum board it *does* read (`us_standouts.json`) is consumed by `intake`, `conviction`, and `risk_sizing` — so the bot reads "what's leading" but not "what's rolling over."

---

## 2. Symptom → root-cause map

| Your symptom | Primary root cause(s) | Confirmed? |
|---|---|---|
| **S1** perma-buys extended semis/AI/MAG7, no asymmetry | RC2 (momentum-follow, no brake), RC3 (firebreaks defeated + fail-open) | ✅ |
| **S2** "no independent mind"; feed-and-filter | RC1 (subtract-only; contrarian seat OFF) | ✅ |
| **S3** defensive 3–4-ETF book beats every Brain | RC5 (can only de-gross to cash, never rotate), RC6 (can't see/learn from it) | ✅ |
| **S4** "closed all trades / conviction sleeve" | Silent rebuild rotation + observability gap (**not** a risk exit; nothing liquidated) | ✅ resolved |
| **S5** stale/wrong regime, can't pre-empt | RC4 (confidence-blind to a wrong-but-fresh label; risk officer stands down) | ✅ (framing corrected) |
| **S6** ignores cycle engine / standouts / signals | RC4 (`sector_cycles.json` read by zero code) | ✅ |

---

## 3. The critical live bugs — fix-now candidates (not just backtest questions)

Two **critical** findings, independently reproduced. These convert a *data outage into a full-conviction buy book* and are live right now.

### 🔴 C1 — Missing `stockdata/` makes the 6-dim confluence gate fail *open*
`missing-stockdata-degenerate-confluence` (critical) + `all-conviction-names-1-of-1-altdata-only` (critical).

- **`vendor/macro/site/stockdata/` does not exist** (0 files; hand-verified `find … -name stockdata` → nothing). The per-name lenses (extension, valuation, trend, solvency, asymmetry) all read `site/stockdata/{t}.json` ([lenses.py:497](portfolio/lenses.py)).
- When it's absent, `lenses.full()` takes the "Trump-linked entity" degenerate branch ([lenses.py:504-507](portfolio/lenses.py)) and returns **only** an alt-data row. Result for *every* name: `n_scored=1, confluence=1.0, size_authority='up', vetoes=[]`.
- All the vetoes live on the absent stockdata rows, so `_hard_vetoes` ([lenses.py:868-878](portfolio/lenses.py)) can never fire; `size_authority='up'` guards ([lenses.py:1022-1030](portfolio/lenses.py)) all **degrade-to-pass** when the direction is missing.
- **Live proof:** the 2026-07-01 build sized **24 names at identical `confluence=1.00`, equal weight, `price=None`, `size_mult=1.3`** ([data/shadow/inputs/2026-07-01.json](data/shadow/inputs/2026-07-01.json): price/extension/rs/pct_vs_200dma all `null` for every name). The engine's own AMD log calls it out: *"confluence 1.0 is a mirage… a single lens registered… no stockdata."*

> **A data-feed outage is silently converted into a full-conviction, equal-weight, cap-exempt buy book.** This is the highest-priority production issue in the audit. It is also *why* the S4 rotation picked a MAG7 basket on one political-flow signal.

**Open question that gates the fix (see §7):** is `stockdata/` a *stale-symlink deployment* problem or a *never-produced engine-output* problem? Repoint `vendor/macro` to a fresh render and re-run `lenses.full()`. If vetoes re-arm, it's a deploy bug; if not, the macro engine isn't publishing the contract the bot depends on.

---

## 4. The six systemic root causes

### RC1 — No additive mind; every reasoning seat is subtract-only, and the one that isn't is switched off
*(S2 primary; enables S1, S3)*

The entire decision architecture can only **confirm, trim, veto, or drop** names the momentum funnel already surfaced:
- Panel adjudicator clamps LLM lean **down, never up** ([panel.py:41](brain/panel.py), sys-prompt "you may only CONFIRM or DE-ESCALATE… never escalate").
- Committee is "**subtract-only… never escalate or rescue**" ([committee.py:143-146](brain/committee.py)).
- Gate Officer "**can never inject a name**" ([gate_officer.py:178](brain/gate_officer.py)).
- Research paper "can confirm, veto, or size a buy, but **can never rescue** a hard-vetoed name" ([research_paper.py:12-13](brain/research_paper.py)).
- SENTINEL is a **bear-only adversary** with no bull/contrarian counterpart.

The **only** seat with additive authority — `STRATEGIST + PM-CONVICTION`, which *can* hold cash and propose a name the feed didn't surface ([pm_conviction.py:109-115](brain/pm_conviction.py): "holding cash when you lack conviction… a smaller book of your best ideas plus paid cash beats…") — sits behind `MASTERMIND_FLAGSHIP_JUDGMENT`, **default OFF** ([phase2.py:109](bot/phase2.py)), set nowhere in `.env`. With it off, the docstring is explicit: *"the engine path below is BYTE-IDENTICAL"* ([phase2.py:490-496](bot/phase2.py)). **The live Flagship is provably a pure engine filter.** No code path anywhere asks "should we be defensive?" or "what should we own to diversify?" (`no-defensive-or-diversification-hypothesis-generator`, `flagship-has-no-independent-contrarian-mind`).

> **Smoking gun (`flagship-judgment-book-diverges-correctly-but-not-executed`):** the contrarian seat, when armed in a dry-run, **correctly trims semis and raises cash to ~29%**. The capability exists and works — it's disabled by a default flag. Highest-leverage single finding in the audit.

**Rolls up:** `flagship-all-flags-off`, `judgment-layer-dark-in-production`, `subtract-only-gate-confirmation-bias`, `no-defensive-or-diversification-hypothesis-generator`, `flagship-has-no-independent-contrarian-mind`, `all-defense-flags-off-by-default`.

### RC2 — The offensive sleeve is a pure top-RS momentum follower with no valuation / extension / cycle brake
*(S1 primary)*

- The **Leadership sleeve (40–60% of NAV)** is literally `leaders = [s for s in secrs[:6] if above_200d_trend][:4]`, equal-weighted ~12.5% each ([phase2.py:256](bot/phase2.py)). Its only filter selects sectors **that already went up**. It **bypasses `lenses.full()` entirely** — no RSI/MACD/pct-vs-200dma/parabolic/valuation/crowding check ever runs on the sleeve holding the bulk of the tech exposure (`leadership-sleeve-no-extension-veto`, `no-forward-pe-valuation-on-leadership`). Live: SMH sits at 12.5% with **vol 39.3, beta 2.03, risk_contribution 0.206 — the largest single-name risk in the book — entered through this ungated path** ([data/portfolio/safety.json](data/portfolio/safety.json)).
- The conviction pool is force-fed a **hardcoded 20-name AI/MAG7 `_SHORTLIST`** ([conviction.py:19](portfolio/conviction.py)) unioned into *every* build regardless of regime; **~10 of 25 live conviction names exist only because of this hardcode**, several of them momentum laggards the feed would never surface (NVDA −10.7%, ORCL −40.9%, PLTR −27.4% trailing 20d) (`hardcoded-shortlist-ai-bias`).
- Where a brake exists it doesn't bind: extension hard-vetoes **only the boolean `parabolic` flag**, so merely-stretched (+30–50% vs 200dma) names clear the 0.30 gate ([lenses.py:872-873](portfolio/lenses.py), `extension-hard-veto-parabolic-flag-only`); valuation is diluted into one growth-muted bloc vote and can never veto ([lenses.py:190-194](portfolio/lenses.py)).
- **The fix already exists in one book.** The ETF book's G4 overextension trim + G5 `megacap_growth_semis` factor-cluster cap (0.40 gross) — built after the 06-22 SMH blow-off — hold that book to **~15% semis** ([bot/etf.py:272-296](bot/etf.py), [config/etf_strategy.yml:38-47](config/etf_strategy.yml)). It was **never ported to Flagship** (`extension-trim-in-etf-book-not-flagship`).

### RC3 — The concentration firebreaks are structurally defeated, and the integrity check fails open
*(S1, S2)*

The three DOCTRINE firebreaks don't bind on the exposure that matters:
1. **0.25 theme cap keys on per-instrument `theme_id`** ([sleeves.py:38-40](portfolio/sleeves.py)); leadership legs set `theme_id = ticker` ([phase2.py:266](bot/phase2.py)), so the 0.79–0.94-correlated SMH/XLK/MTUM cohort is 4 separate "themes" and never sums. Live: **Information Technology = 42.6% of book across 10 names, `safety.json breaches: []`** (`theme-cap-defeated-by-per-name-theme-id`).
2. **Leadership sleeve is exempt from the 0.08 name cap by design** ([sleeves.py:30-31](portfolio/sleeves.py)).
3. **The 6-dim confluence gate fails open** on missing stockdata — see §3 / C1.

And there is **no binding cross-book constraint**: `firm_exposure.py` is "**deliberately TOOTHLESS… never changes an allocation**" ([firm_exposure.py:9-13](portfolio/firm_exposure.py)), wired into zero buy paths. Heavyweight is *hard-gated to Flagship's universe* ([heavyweight.py:50-95](bot/heavyweight.py)) — it **structurally amplifies** rather than diversifies (`heavyweight-downstream-amplifier`): on 07-01 it liquidated $733k of cyclicals and bought $760k of tech/MAG7 to mirror Flagship, landing at **~40% semis, ~68% tech, no theme cap in its path** (`heavyweight-book-mirrors-flagship-after-rotation`). Four US books can independently max-conviction the same SMH with nothing trimming the aggregate.

### RC4 — Trusts an over-optimistic regime label it is architecturally blind to and cannot disagree with
*(S5 mechanism, S6, S1 justification)*

Two layers, both in §1.2–1.4: **(a)** every regime reader drops `confidence / transition_state / contradicting`; the run-gate is blind to them, so a WEAKENING low-confidence flip neither shrinks the sleeve nor wakes the book. **(b)** the one armed defensive tooth (`macro_risk` gross cap) self-neutralizes because the stateless, hysteresis-free fragility scorer prints `risk_on` — *and flipped CAUTION→RISK_ON on the −6.4% SOXX day*. **(c)** the `sector_cycles.json` contract (Technology=Topping/SELL) sits in the tree the bot already reads and is referenced by zero code.

**Rolls up:** `intake-regime-nuance-dropped`, `sector-cycles-completely-ignored`, `cycle-signals-not-consumed`, `regime-stale-propagates-to-all`, `macro-risk-state-flip-on-day-of-soxx-crash`, `caution-gross-cap-not-enforced-on-flagship`, `macro-risk-gross-cap-off`, `intake-regime-stale-anchor-gap`, `fused-risk-shadow-not-wired`.

### RC5 — Every response mechanism is subtract-only and lagged; the book can de-gross to cash but never rotate
*(S3, S1, S5)*

RC1's mirror on the risk side. `macro_risk` cap, `safety.gross_overlay`, conviction sector-cap, `defensive_playbook` — **all architecturally subtract-only.** `defensive_playbook` states it verbatim ([defensive_playbook.py:165-166](portfolio/defensive_playbook.py)): the `favor=[XLP,XLV,USMV,SGOV,TLT]` list is *"ADVISORY… the desk NEVER auto-buys a defensive."* So "should we be defensive?" collapses to "how much cash?"; "what should we own to diversify?" is never answered. Timing compounds it: reconfiguration happens **only in the once-daily 22:40 UTC build**; the sole intraday reflex is subtract-only; and the overnight re-decide watch **excludes Flagship and Heavyweight entirely** (`_RUNNERS = {etf, autonomous, china, hk}`, [overnight.py:22](bot/overnight.py)). On 07-01 the **Autonomous** book *sold* SMH into the down-tape while Flagship couldn't lean out and Heavyweight *bought more* (`autonomous-book-sold-smh-on-soxx-crash-day`, `regime-intraday-lag`).

### RC6 — The system cannot see, attribute, or learn from being beaten by "buy defensives and do nothing"
*(S3 directly; amplifies S1/S2)*

The winning book is architecturally invisible. **Self-Directed produces neither `decisions.jsonl` nor `nav_history.jsonl`**, is *explicitly excluded* from `firm_exposure` ([firm_exposure.py:88](portfolio/firm_exposure.py)) and the calibration ledger ([calibration.py:296](brain/calibration.py)), and is compared to the Brains on an incompatible basis — different inception (06-22 vs 06-19), `vs_spy=None` while Brains get SPY-relative, live-recomputed mark vs once-daily snapshot, and a different price source (Polygon vs Yahoo) (`unfair-inception-and-benchmark-basis`, `mark-timing-and-stale-nav-rows`). **No calibration multiplier, reputation weight, or posture parameter is a function of the Brains losing to it.** Worse, the attribution engine is a per-name Brinson split ([attribution.py:216-232](brain/attribution.py)) — a book whose edge was *holding defensives and not trading* generates no attributable name and **cannot be credited**. And the empirical loop is dead: the 6,677-row prediction ledger **never resolves** because the labeler reads absent `_closes_deep`/`_closes_delisted` parquet and swallows the exception ([predictions.py:102-114](portfolio/predictions.py)) — a permanently-stuck resolver looks identical to a healthy "building" state, so the Brains run on a flat 0.70 prior with **zero empirical correction** (`shadow-leaderboard-zero-resolved-theses`, `no-learning-loop-from-benchmark-book`, `overtrading-turnover-drag-unmeasured`).

> **Is S3 real or a measurement artifact?** The *sign* is real: recomputing self-directed from the local Yahoo parquet closes gives **~+1.45%** (XLV +4.26%, XLU +0.60%, XLF −0.50%, XLP −0.78%) vs Brains **−0.23% … −3.34%**. But inception/benchmark/mark-timing all differ, so the *magnitude* is not trustworthy until renormalized (see §7).

---

## 5. Confirmed problem inventory (47)

Grouped by root cause. Severity = final (post-verification). Full evidence in the audit artifact; representative `file:line` in §3–4.

**RC1 — no additive mind**
- `flagship-all-flags-off` (high) · `judgment-layer-dark-in-production` (high) · `subtract-only-gate-confirmation-bias` (high) · `no-defensive-or-diversification-hypothesis-generator` (high) · `flagship-has-no-independent-contrarian-mind` (high) · `all-defense-flags-off-by-default` (high, partial) · `flagship-judgment-book-diverges-correctly-but-not-executed` (medium, partial)

**RC2 — momentum-follow, no brake**
- `hardcoded-shortlist-ai-bias` (high) · `leadership-sleeve-no-extension-veto` (high) · `extension-hard-veto-parabolic-flag-only` (high) · `extension-trim-in-etf-book-not-flagship` (high) · `leadership-sleeve-blind-to-regime-quality` (high) · `no-forward-pe-valuation-on-leadership` (medium) · `momentum-only-candidate-sourcing` (medium, partial) · `static-seed-tech-bias` (low, partial)

**RC3 — firebreaks defeated / fail-open**
- `missing-stockdata-degenerate-confluence` (**critical**) · `all-conviction-names-1-of-1-altdata-only` (**critical**) · `theme-cap-defeated-by-per-name-theme-id` (high) · `no-cross-book-diversification-mandate` (high) · `heavyweight-downstream-amplifier` (high) · `heavyweight-book-mirrors-flagship-after-rotation` (high) · `illusory-diversification-one-factor` (high, partial)

**RC4 — regime blindness**
- `intake-regime-nuance-dropped` (high) · `sector-cycles-completely-ignored` (high) · `cycle-signals-not-consumed` (high) · `leadership-sleeve-blind-to-regime-quality` (high) · `macro-risk-state-flip-on-day-of-soxx-crash` (high) · `regime-stale-propagates-to-all` (high, partial) · `caution-gross-cap-not-enforced-on-flagship` (high, partial) · `macro-risk-gross-cap-off` (high, partial) · `intake-regime-stale-anchor-gap` (medium) · `regime-staleness-soft-block` (medium, partial) · `fused-risk-shadow-not-wired` (medium, partial)

**RC5 — subtract-only / lagged response**
- `regime-intraday-lag` (high) · `regime-gate-blind-to-name-breakdown` (high) · `autonomous-book-sold-smh-on-soxx-crash-day` (medium)

**RC6 — no learning from the benchmark**
- `no-learning-loop-from-benchmark-book` (high) · `unfair-inception-and-benchmark-basis` (high) · `self-directed-outperformance-gap` (high) · `mark-timing-and-stale-nav-rows` (medium, partial) · `overtrading-turnover-drag-unmeasured` (medium, partial) · `shadow-leaderboard-zero-resolved-theses` (medium, partial) · `self-directed-book-no-current-prices` (medium, partial)

**S4 observability / state**
- `s4-conviction-close-is-silent-rebuild` (medium) · `s4-conviction-sleeve-wholesale-rotation-trigger` (high, partial) · `flagship-positions-pre-rotation-state` (medium, partial) · `conviction-rebuild-daily-churn` (medium, partial)

**Housekeeping**
- `hk-file-wrong-comment-allowed-venues` (low) — `bot/hk.py` docstring/comments are copy-pasted from the China book (says "CNY"/"A-share"; runtime is correctly HKD/HK). Cosmetic, but a real maintenance-hazard trap.

---

## 6. What was refuted (kept for honesty)

The adversarial pass killed 4 candidate problems — worth recording so Fable doesn't chase them:

1. **`flagship-conviction-closed-soxx-tripwire-not-pm-directive`** — REFUTED. Flagship was **not** liquidated (99.8% invested, $14.72 cash). The derisk artifact `action='hold'` is a *no-op*, not evidence of prior de-grossing. (This was my interim hypothesis.)
2. **`held-hysteresis-and-hard-exit-sweep-S4`** — REFUTED. 07-01 was a full rebuild, not a carried-forward day, so the hard-exit sweep never fired; the 16 closes came from ordinary rebuild reconciliation, and the dropped names were the *diversifiers*, not a correlated AI stop-out.
3. **`conviction-daily-full-churn`** — REFUTED/overstated. `build()` is **not** stateless; hysteresis is wired ([conviction.py:136-174](portfolio/conviction.py)); live held-name turnover was 0/15, not >50%. (A genuine but *different, lower-severity* issue surfaced: a `positions_ledger` ↔ `account.json` reconciliation gap where ANET/APH/AME show closed in the ledger but still hold shares in the account — a bookkeeping bug, not orchestration churn.)
4. **`etf-brain-factor-cluster-cap-not-enforcing-current-book`** — REFUTED. The ETF book's caps *are* enforcing (cluster ~15% vs 40% cap; SMH 2%); the book is heavily de-risked (~53% cash+SGOV). One narrow survivor: `_apply_guardrails` is gated behind `if decided:` ([etf.py:136](bot/etf.py)), so a carried book on a no-submission day isn't re-checked — robustness note only.

---

## 7. Research & test agenda for Fable

Ranked by (expected impact × testability on the existing paper + backtest harness). Tier 1 first. Each carries a **falsifier** so a null result is informative.

### TIER 1 — highest leverage, cheapest to test

**E1 — Arm the contrarian seat and A/B it** *(RC1, RC5; S2/S1/S3)*
Run `MASTERMIND_FLAGSHIP_JUDGMENT=1` (+ `RISK_GOVERNOR=1`, `RISK_OFFICER=1`) as a shadow book 2–4 weeks against the live deterministic book. Measure forward NAV, semis concentration, cash%. **We already have a dry-run showing the seat trims semis and raises cash to ~29%.** *Falsifier:* if the judgment book never diverges from the engine book, the fix is deeper than a flag (the personas merely echo the engine → rewrite persona incentives, which are graded on NAV-vs-SPY and told to "press your winners").

**E2 — Fail *closed* on missing stockdata + require multi-lens confluence** *(RC3 / C1; critical)*
A **live safety bug**, not a backtest question. Two guards: (a) `size_authority='up'` only if a real `extension`/`trend` row exists **and** `n_scored ≥ 2`; (b) if `site/stockdata/` is empty at build time, refuse to open new conviction positions. Re-run the 07-01 build: the 24-name equal-weight book should collapse to the handful with real confirmation. **First resolve the open question:** is `stockdata/` a stale-symlink deploy bug or a never-produced engine gap? Repoint `vendor/macro` to a fresh render and re-run `lenses.full()`.

**E3 — Wire the sector-cycle contract as a leadership veto** *(RC2/RC4; S1/S6)*
Add `sectordata/sector_cycles.json` to `lenses._load`. Walk-forward the leadership sleeve two ways: (a) RS-only; (b) RS + a cycle veto forcing any Peak/Topping (or `signal=SELL`) sector to *initial* size instead of full equal-weight. Compare forward 20/60d return and max drawdown. Run three variants keying on `now.signal` vs `now.action` vs `now.phase` (resolves the SELL-but-action=HOLD ambiguity). *Falsifier:* if the cycle veto doesn't improve risk-adjusted return, the signal is cosmetic → downgrade S6.

**E4 — Port the ETF book's G4/G5 caps to Flagship** *(RC2; S1)*
The overextension trim + `megacap_growth_semis` factor-cluster cap already **work** in `etf.py`. Shadow-build Flagship with G4/G5 on the leadership legs; measure weight removed (each extended leg clamped 0.125→0.08) and cluster de-gross. Proven in one book → low-risk, high-confidence.

### TIER 2 — high impact, moderate cost

**E5 — Confidence/transition haircut on the leadership budget** *(RC4; S5/S1)*
Make `lead_budget = interp(low..mid)` a function of `confidence`/`transition_state`; add both to the gate signature so a WEAKENING flip wakes the book. Replay across historical regime snapshots; regress next-20d leadership drawdown on the low-confidence/WEAKENING flag to size the haircut.

**E6 — Make Self-Directed visible and close the learning loop** *(RC6; S3)*
Write `nav_history.jsonl` for self-directed (add a Yahoo-parquet fallback to `_current_price` — the ETF closes exist locally but `_current_price` never reads them); add `self_directed_nav` as a benchmark column alongside `spy_nav`; extend `cio._NAV_BOOKS` to all books; compute trailing Brain-minus-SelfDirected excess; add a turnover-cost/cash-drag term and a frozen-holdings buy-and-hold shadow so "the edge was inactivity" becomes representable. *Falsifier:* grep every adaptation artifact for any input derived from the cross-book gap — currently zero.

**E7 — Hysteresis on the fragility scorer + de-couple the tripwire cut from the state cap** *(RC4/RC5; S5)*
Replay `risk_state()` over 60 days; count CAUTION→RISK_ON transitions where the same day's tripwire fired severity≥2. Add a 3-session cooldown / EMA decay before any de-escalation to `risk_on`, and size the tripwire cut to a **severity-derived** cap independent of state. Re-run 07-01: assert `derisk_flagship` would then see `gcap=0.7` and actually cut.

**E8 — Cluster-aware cross-sleeve cap + binding firm-exposure** *(RC3; S1)*
Re-run `enforce_book_caps` with `theme_id` replaced by (a) GICS sector and (b) a corr≥0.7 cluster label; measure the tech-cluster haircut the per-instrument cap never applied (~0.12–0.18 expected today). Separately, make `firm_exposure` binding on the buy path and back-test a firm-wide semis cap.

### TIER 3 — observability / correctness, lower P&L leverage

**E9 —** Close-reason taxonomy in `position_log` (S4): mirror the 5 thesis-ledger strings into the position ledger so rotation vs risk-exit is distinguishable. Pure observability.
**E10 —** Fix the prediction-ledger labeler ([predictions.py:102](portfolio/predictions.py) absent-parquet hardcode) + a data-health alert when the panel fails to load, so a stuck resolver stops masquerading as "building." Prerequisite for E6's empirical loop.
**E11 —** Overnight/intraday re-decide for Flagship + Heavyweight (RC5): add them to `overnight.py._RUNNERS` and give `phase2.run` a `directive=` param. Measure the open-to-open drawdown the mechanical books ate on material-tape days the Autonomous book dodged.

---

## 8. Where the audit is still uncertain — data that would resolve it

1. **Is `stockdata/` missing from a stale symlink or never produced?** *(gates C1/E2 severity.)* Repoint `vendor/macro` to a fresh render, re-run `lenses.full()`. Vetoes re-arm → deploy bug; still empty → engine-output gap.
2. **Which env actually runs production?** `.claude/launch.json` arms `MACRO_RISK` + `FAST_DERISK` but is a VS Code-style launcher, not a confirmed cron/systemd unit (`.env` sets **none** of the `MASTERMIND_*` flags). Dump the running scheduler process env (`ps eww` on the `app.main` PID). This decides whether the subtract-only teeth even run.
3. **Is S3 outperformance real or an artifact?** Sign is real (~+1.45% vs −0.23%…−3.34%), but inception/benchmark-basis/mark-timing/price-source all differ. Renormalize every book to $1.00 at the common max-inception date, force one price source/timestamp. If the lead survives, RC2–RC5 are confirmed as the cause.
4. **Would the contrarian seat actually rotate, or echo the engine?** *(E1's falsifier.)* Only a shadow run resolves it.
5. **Does the cycle/confidence signal carry forward-return power, or is it cosmetic?** *(E3/E5 falsifiers.)* Only the walk-forward resolves whether Peak-sector names underperform Trough-sector names over the window.

---

## 9. Bottom line for Fable

The four money-losing clusters are **RC2** (momentum-follow with no brake), **RC3** (firebreaks defeated + fail-open on a data outage), **RC4** (trusts an over-optimistic label it can't disagree with), and **RC5** (can only cut to cash, never rotate). **RC1 is *why*** — no additive mind — and its fix (E1) is one flag plus a shadow A/B. **RC6 is *why nothing self-corrects*.** Start with **E1, E2, E3, E4**: three of the four are already-proven-elsewhere-in-the-codebase or already-observed-in-dry-run, so they are high-confidence and cheap to validate on the existing harness. And **C1 (fail-open on missing stockdata) should be treated as a live incident, not a research item** — right now a feed outage is silently rewriting the book into an equal-weight MAG7 basket picked on one political-flow signal with no price data.

---

### Appendix — provenance
Adversarial multi-agent audit run 2026-07-01 (60 agents, ~4.2M tokens, 8 subsystem mappers + 51 verifiers + synthesis). 47/51 problems confirmed; 4 refuted (§6). Raw artifact retained in the session workflow output. Load-bearing facts (absent `stockdata/`, unused `sector_cycles.json`, 100%-invested Flagship, self-directed holdings, the 07-01 rebuild, the risk-state flip) hand-verified by the lead against live state files.
