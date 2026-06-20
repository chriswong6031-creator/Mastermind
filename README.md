# Mastermind

Autonomous, narrative-based, medium/long-term **US-equity** investment agent. A
live "breathing" web app that reuses the macro dashboard's engine as a library,
runs a gated Claude (Opus 4.8) analysis loop, keeps a Brier-scored accountability
ledger, and publishes its paper portfolio + decision journal back to the macro
dashboard as a static page.

> Paper-only / display-only. Accountability, not alpha. Never auto-executes.

## Architecture (one sentence)

Themes/regime → a Claude deliberation → a deterministic sizing pass → a paper
book → a static page, with a self-improving backtest loop feeding vetted
strategies in from the side. Three invariants: the macro repo is a **pinned,
read-only submodule**; **nothing executes**; **sizing is subtract-only**.

```
Mastermind/
  vendor/macro/      pinned submodule (symlink in local Phase 0) — engine.* lib
  app/               FastAPI service (deps bootstrap, /health, /regime)
  bot/               package root — bootstraps vendor/macro onto sys.path; smoke test
  brain/             B — Claude deliberation (client/gate/panel/adjudicator/decision/scorer)
  loop/              A — self-improving backtest loop (candidates/harness/holdout/promote)
  portfolio/         C — themes -> sized, timed positions
  data_layer/        D — read channel + Polygon ingest + Postgres/DuckDB
  bridge/            write-back: site/portfolio.json -> macro dashboard page
  sql/0001_schema.sql  the 9-table system-of-record
  config/brain.yml   model tiers, gate, de-confidencing, paper gate
  prototype/index.html  clickable static UI prototype (no backend)
```

## Phase 0 — prove the wiring

```bash
# vendor/macro is a symlink to the working macro checkout (data/ populated)
python -m bot.smoke          # imports engine, reproduces today's regime live
pytest                       # same as an acceptance test
```

Expected: the live `build_features() -> classify()` recompute matches
`data/regime/latest.json` (live == backtest by construction).

## Status

Phase 0 scaffold. Brain / loop / portfolio / data_layer / bridge packages are
empty placeholders to be filled per the design (see the design doc / memory).
