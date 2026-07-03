# Incident Replay Harness

Each sub-directory is one incident. The test file reads the fixtures, reconstructs
the stack inputs, and asserts the CURRENT code handles them correctly.

## Adding a new incident

1. Create `fixtures/<YYYY-MM-DD-slug>/` with the skeleton below.
2. Add a `test_incident_<slug>.py` in this directory importing the fixtures.
3. Register assertions for at minimum the four canonical shapes:
   - Dwell: a CAUTION->RISK_ON flip on the crash day is blocked.
   - Severity cap: sev-2 eff_cap cuts a gross > 0.70 book.
   - Firm cap: a high-conviction rebuy is rejected by peer pile-up.
   - Cycle gate: late_cycle blocks a new offensive seed; entry_favored admits defensive.

## Fixture skeleton

```
fixtures/<incident-slug>/
  state.json               # macro_risk state.json per date sub-dir OR a single day file
  <YYYY-MM-DD>/
    state.json             # brain/macro_risk output for that day (real or synthetic)
    derisk_autonomous.json # derisk artifact for autonomous book that day
  regime_latest.json       # vendor/macro/data/regime/latest.json snapshot (the label the bot trusted)
  sector_cycles.json       # vendor/macro/site/sectordata/sector_cycles.json snapshot
  etf_closes.json          # {ticker: {date: close}} for the incident window — trimmed from parquet
  peer_books.json          # synthetic peer book weights {pid: {positions, nav, currency}}
```

## 2026-07-02-semis-breakdown (the founding incident)

Semis/memory/AI broke down while every US book was 60-90% offensive on a
Goldilocks/STABLE/expanding-liquidity label. The dashboard's own radar already read
CAUTION/growth-scare/91 but the bot never saw it (risk_radar/ stripped from sparse set).

Key dates:
- 06-24: def-RS crossover first fires (soft nowcast).
- 06-26: risk_radar caution 91/100, dashboard-side CAUTION state (frag 0.552).
- 07-01: SMH 3D-MACD bearish cross; SOXX -6.4% theme day; raw scorer flips
  risk_on (frag 0.121) but dwell holds CAUTION. Autonomous sold SMH $626.
- 07-02: Autonomous rebuys SMH $605.70 into the breakdown. Firm caps reject it.

Test assertions (see test_incident_2026_07_02.py):
1. 07-01 CAUTION->RISK_ON flip is blocked by dwell (stays CAUTION for 3 sessions).
2. sev-2 + 0.90 gross book => eff_cap 0.70 cuts to 0.70 (heavyweight replay).
3. Autonomous SMH rebuy rejected: peer pile-up > firm name cap.
4. XLK late_cycle blocks a new semis seed; XLV/XLU are entry_favored.
5. budget() < 0.50 on the 07-01 regime file (conf=0.327 STABLE -> 0.449).
