# Case study — the AVGO/NVDA forced-override post-mortem (2026-06-22)

**Type:** governance / process lesson (NOT a graded P&L outcome).
**Status:** the theses are unresolved; the lesson here is about *process*, not about being proven right by one day of price action.

## What happened

On 2026-06-20→21 the engine and the Opus research desk were, by design, **cautious** on NVDA and AVGO at large size:

- The conviction engine and the brain disagreed with a large position; the combined "Conviction Index" was muted.
- The operator (PM) pushed back, and changes were made across **both** the Macro analyzer engine and the Mastermind conviction/brain stack that lifted the two names, and the Opus brain **re-rated** AVGO's research leg upward.
- Result in the Flagship paper book:
  - **AVGO** — engine 86 + research **100** → combined **93**, `size_stage: full`, the 1.3× size boost. 2.69% of NAV.
  - **NVDA** — engine 67 + research 70 → combined 68, `size_stage: initial` (half size). 0.47% of NAV.

On 2026-06-22, NVDA was −1.3% and AVGO −4.6% (the book's largest single-name loser) while most of the rest of the book (industrials / AI-buildout / semis-momentum / small-caps) was green.

## The honest lesson (the crux)

**One down day is one observation. It does not prove the engine's caution was "right," any more than a green day would have proven the override was right.** Treating a single day's P&L as vindication is exactly the outcome-bias trap this system's validation machinery (DSR/PBO/FDR, the signal-sanity tripwire, the outcome ledger) exists to resist. The AVGO/NVDA theses do not *resolve* until their time-stop (~2026-09-15); the `outcome_ledger` will grade them then, on realized rel-return, like every other bet.

What *is* day-independent, and what this post-mortem actually indicts, is the **process**:

1. **A risk control was removed to fit two names.** The sector-concentration firebreak was disabled (`SECTOR_MAX_NAMES=None`) so a homogeneous AI-semis cohort could load up. Removing a concentration cap is a fragility-increasing change regardless of which way the names then moved.
2. **An unvalidated signal was wired into the score.** A forward-cone "risk shape" tilt — built on a signal the repo's own research labels *NO-GO for size / coin-flip direction* — was added to the analyzer's scored entry axis specifically to surface the two names' favourable cone.
3. **Weights/bars were tuned to a two-name target.** The selection-axis EDGE weights (SUE↓/revisions↑) were set by rebuilding until AVGO/NVDA cleared a band, and the held keep-bar was loosened citing NVDA churn.

The common thread is **single-name motivated changes to general machinery** — the failure mode, not the price move.

## What we did (and deliberately did NOT do)

We did **not** hard-code an NVDA/AVGO penalty or a permanent ban. That would overfit the engine to one day and corrupt it — the same error in the opposite direction. Instead:

**Reverted the unprincipled changes (kept the defensible ones):**
- Restored a concentration firebreak — now **percentage-based: ≤ 50% of the conviction-sleeve budget per sector** (`SECTOR_MAX_FRACTION = 0.50`, `portfolio/conviction.py`), an improvement on the old count-based cap: it de-grosses a correlated cohort as a *group* and never churns held names (it scales them down).
- Pulled the forward-cone tilt **out of the scored axis** (Macro `engine/stock_score.py`); kept it as a display-only honesty note.
- Re-derived the EDGE weights **from the IC evidence, not a two-name target** (SUE's deep-panel IC ≈ 0 → demoted to a confirmer floor; weight concentrated on the lone FDR survivor, insider; revisions kept at their prior level, *not* re-raised). Macro [PR #459].
- Tightened the held keep-bar back toward entry parity (exit floor 0.15→0.25; combined-gate hysteresis 8→4).

**Kept the genuinely defensible fixes** (reverting them would make the engine chase expensive growth *more*): the growth-adjusted (PEG) valuation read, the 13F min-sample gate, the leadership/falling-knife rewrite, and the subtract-only forward-valuation haircut.

**Unwound the positions cleanly:** NVDA and AVGO were reversed out of Flagship **at cost basis ("as if never bought")** — the buy fills were deleted from the order book and the cost was refunded to cash, so the unrealized loss was restored rather than realized at the lower price. A `_MANUAL_EXCLUDE` hold-out keeps the daily rebalance from silently re-buying them (a removable operational guard, not a scoring penalty).

## How this *reinforces* the system (the principled channel)

The way to "reward" the system's correct instinct is to **strengthen the general risk controls that the override had weakened**, and to **let the calibration loop learn** — not to bolt on a special case:

- The ≤50%-per-sector firebreak is now a permanent, general crowding control.
- When the AVGO/NVDA theses mature, `brain/outcome_ledger.py` grades them on realized rel-return and feeds `lens_edge` / `lens_weights`; if the lenses that over-rated AVGO were genuinely miscalibrated, the **self-calibrating gate** down-weights them *empirically* — the honest, sample-driven channel, immune to one-day narrative.

## Pointers
- Macro analyzer revert: [PR #459](https://github.com/chriswong6031-creator/macro/pull/459)
- Mastermind engine: `portfolio/conviction.py` (`SECTOR_MAX_FRACTION`, `_MANUAL_EXCLUDE`, `_apply_sector_cap`, `_EXIT_CONFLUENCE_FLOOR`), `brain/research_paper.py` (`_HELD_HYSTERESIS`)
- Governance row: `data/brain/outcome_ledger.jsonl` (`kind: "governance"`)
- Ledger backup of the pre-reversal book: `data/portfolio/_backup_pre_nvda_avgo_exit_*`
