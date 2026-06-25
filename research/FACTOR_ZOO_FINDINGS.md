# Factor research program — findings (price + fundamentals + regime)

*Honest, multiple-testing-controlled cross-sectional factor research on the survivorship-safe
S&P-1500 panel. All backtests run through the FROZEN gauntlet: DSR re-deflated at effective-N over
the whole pool, PBO/CSCV, BH-FDR, one-shot 2022+ holdout. The historical backtest is a PRIOR/filter;
the forward paper Brier is the ultimate judge. Nothing here is wired into the live engine — research
only.*

Generators (on-demand, each burns a one-shot holdout per pool):
- `loop/factor_zoo.py` → `scripts/run_factor_zoo.py` → `data/backtest/factor_zoo.json` (price factors)
- `loop/fundamentals.py` → `scripts/run_fundamentals.py` → `data/backtest/fundamentals.json` (PIT value/quality)
- surfaced read-only on the dashboard ("Factor Alpha Lab" + "Fundamental Factors — PIT").

## 1. Price factors (closes-only, 2002–2026, 16 factors)

Library: multi-horizon momentum, residual/idiosyncratic momentum, reversal (1m/1w), low-vol (60/120d),
downside-vol, MAX-lottery, idiosyncratic skew, 52w/26w proximity, trend-consistency, acceleration +
a de-correlated composite.

**Verdict (latest run):** effective-N 4 (low-risk variants collapse as near-duplicates), PBO ≈ 0.18,
best DSR ≈ 0.99. **Holdout-confirmed survivors — ALL low-risk / anti-lottery:** lowvol_120d,
maxret_low_1m, lowvol_60d, skew_low_6m, consistency_6m.

- **Momentum did NOT confirm out-of-sample** (2022 was a momentum crash). Honest.
- **IC term-structure insight:** the low-vol factors have ~0 return-rank-IC but high Sharpe → their
  edge is **risk reduction, not return prediction**. They don't rank the biggest winners; they cut
  the variance/drawdown.
- The composite has the top DSR but FAILS the in-sample `beats_spy` leg (low-vol gives up raw upside
  in bull markets) → not confirmed as a return engine.

## 2. Fundamental factors — PIT (SEC EDGAR, 2010–2026, 15 factors)

Point-in-time correct: a fundamental is only used on/after its `asof_date` (filing date, ~1 quarter
after period end). Market cap = current price × last-reported shares. Value: E/P, B/P, CF/P, sales/P,
shareholder-yield. Quality: gross-profitability, ROE, ROA, accruals, asset-growth. Both long-only
(decile/quintile) and long-short (market-neutral) variants.

**Verdict (latest run):** effective-N 6, **PBO ≈ 0.66 (FAILS)**, best DSR ≈ 0.98 — but NOTHING passed
the in-sample gate, so the holdout was never burned on a passer. The decisive contrast:

| | long-only Sharpe | long-short (market-neutral) Sharpe |
|---|---|---|
| ROA / ROE / gross-prof | 0.95–1.01 | — |
| ep / bp / roe (L/S) | — | 0.11–0.40 (none FDR-significant) |

**Conclusion: the high long-only Sharpe is mostly MARKET BETA, not a factor premium.** Strip beta
(long-short) and the actual value/quality premia were weak in 2010–2022 (the "value winter"); none
were statistically significant after multiple-testing control. PBO failing confirms the in-sample
ranking among these (highly-correlated) portfolios does not hold out-of-sample.

**Regime-conditional IC (SPY vs 200d MA, leakage-free):** quality (ROA) predicts forward returns far
better in **risk-off** (IC ≈ +0.073) than risk-on (≈ +0.006) — a flight-to-quality tell. This is the
most actionable fundamental nuance: quality is a defensive/regime signal, not an all-weather premium.

## 3. Honest data gaps (not faked)

- **No historical volume panel** → volume/liquidity factors (Amihud illiquidity, turnover, dollar-
  volume) are NOT built. Acquiring a PIT volume panel (e.g., from the same source as the closes panel)
  is the cheapest next data add and would unlock a whole liquidity family.
- **EDGAR structured data starts ~2010** → the fundamental backtest window is shorter (and annual
  frequency) than the price factors. The 2010–2022 in-sample also coincides with the value winter, so
  the weak value result is partly era-specific.
- The backtest models 3bps one-way cost but **not shorting costs / borrow** for the long-short
  variants — their (already weak) net premia would be weaker still.
- **EDGAR-coverage subset (partial survivorship):** only ~60–70% of price-eligible S&P-1500 members
  have EDGAR structured data at a given rebalance (delisted / non-filing names are dropped from the
  fundamental cross-section, though they remain in the price panel). The fundamental factors thus run
  on a disclosed/alive-biased subset — quantified per run as `verdict.edgar_coverage_median`. The
  *price* factors are unaffected (full survivorship-safe panel). This was surfaced by the adversarial
  review and is now disclosed in the run's `data_gaps`.

## 4. What the whole program says

Across a wide, multiple-testing-honest search of price AND fundamental cross-sectional factors, the
**only edge that survives out-of-sample is the low-risk / anti-lottery anomaly** — and it is a
**risk-reduction (Sharpe/drawdown) lever, not a return-alpha**. Value/quality did not add a confirmed
standalone premium in this window net of beta; quality is best read as a **regime-conditional
defensive** signal.

**Implication for the live engine (when/if wired, subtract-only):** use the confirmed low-risk signal
as a **size haircut on lottery-like / high-vol names**, and optionally use **quality as a risk-off
defensive tilt** — never as a stock picker. Prove any of it FORWARD (shadow book + prediction log)
before it touches the live book; the historical verdict is a prior, not permission.
