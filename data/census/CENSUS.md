# Mastermind System Census

> GENERATED — do not hand-edit; architecture docs must cite this file. (R10)  
> Generated at: `2026-07-06T08:29:04.426114+00:00`  
> Git SHA: `0ef769f`

## A. Scheduled Jobs

**18 jobs registered** in `app/scheduler.py`

| id | cron_spec | hour | minute | day_of_week | timezone |
|---|---|---|---|---|---|
| `macro_refresh` | `30 */3 * * *` | */3 | 30 | * | UTC |
| `daily_mark` | `35 22 * * mon-fri` | 22 | 35 | mon-fri | UTC |
| `daily_loop` | `40 22 * * *` | 22 | 40 | * | UTC |
| `autonomous_daily` | `10 23 * * mon-fri` | 23 | 10 | mon-fri | UTC |
| `heavyweight_daily` | `25 23 * * mon-fri` | 23 | 25 | mon-fri | UTC |
| `china_daily` | `0 8 * * mon-fri` | 8 | 0 | mon-fri | UTC |
| `hk_daily` | `0 9 * * mon-fri` | 9 | 0 | mon-fri | UTC |
| `etf_daily` | `15 23 * * mon-fri` | 23 | 15 | mon-fri | UTC |
| `settle_pending` | `0 15 * * mon-fri` | 15 | 0 | mon-fri | UTC |
| `settle_brain_asia` | `35 1 * * mon-fri` | 1 | 35 | mon-fri | UTC |
| `watch_us_overnight` | `20 2,6,11 * * mon-fri` | 2,6,11 | 20 | mon-fri | UTC |
| `watch_asia_overnight` | `20 14,20,0 * * mon-fri` | 14,20,0 | 20 | mon-fri | UTC |
| `derisk_us_intraday` | `0 14-20 * * mon-fri` | 14-20 | 0 | mon-fri | UTC |
| `publish_macro_snapshot` | `25 12,22 * * *` | 12,22 | 25 | * | UTC |
| `cio_weekly` | `0 10 * * sun` | 10 | 0 | sun | UTC |
| `improvement_agenda_weekly` | `30 agenda_hour * * sun` | agenda_hour | 30 | sun | UTC |
| `loop_maintenance` | `45 23 * * mon-fri` | 23 | 45 | mon-fri | UTC |
| `experiment_maturity` | `50 23 * * mon-fri` | 23 | 50 | mon-fri | UTC |

## B. Portfolio Books

**7 books** in `portfolio/registry.py`

| id | kind | manager | benchmark | currency |
|---|---|---|---|---|
| `flagship` | gated | engine | SPY | USD |
| `heavyweight` | heavyweight | brain | SPY | USD |
| `autonomous` | autonomous | brain | SPY | USD |
| `etf` | etf_brain | brain | SPY | USD |
| `china` | china_brain | brain | FXI | CNY |
| `hk` | hk_brain | brain | FXI | HKD |
| `self_directed` | self_directed | you | SPY | USD |

## C. MASTERMIND_* Flags

**0 flags currently set** in environment:

**47 known flags NOT set:**  
`MASTERMIND_ALLOW_FRACTIONAL`, `MASTERMIND_AUTH_TOKEN`, `MASTERMIND_BRIDGE_PROGRAM`, `MASTERMIND_CAUTION_GROSS`, `MASTERMIND_CAUTION_SCORE`, `MASTERMIND_CHAIN_CAP`, `MASTERMIND_CHARTER_V`, `MASTERMIND_COMMITTEE`, `MASTERMIND_CONTROL_PLANE_MASTERPLAN`, `MASTERMIND_COOKIE_SECURE`, `MASTERMIND_DERISK_THEME_DROP`, `MASTERMIND_DWELL`, `MASTERMIND_EXPLORE_EPS`, `MASTERMIND_EXPLORE_WEIGHT`, `MASTERMIND_FAST_DERISK`, `MASTERMIND_FIRM_CAPS`, `MASTERMIND_FIRM_CLUSTER_CAP`, `MASTERMIND_FIRM_NAME_CAP`, `MASTERMIND_FIX_MASTERPLAN`, `MASTERMIND_FLAGSHIP_JUDGMENT`, `MASTERMIND_GATE_OFFICER`, `MASTERMIND_HW_FIRM_UNIVERSE`, `MASTERMIND_HW_MIN_FUNDABLE`, `MASTERMIND_LEARNING_DESIGN`, `MASTERMIND_MACRO_RISK`, `MASTERMIND_MIN_POSITION_FRAC`, `MASTERMIND_MIN_TRADE_FRAC`, `MASTERMIND_NIGHTLY_USD_CAP`, `MASTERMIND_NO_TRADE_BAND_FRAC`, `MASTERMIND_NW_CONTEXT`, `MASTERMIND_PASSWORD`, `MASTERMIND_POSTURE_ADAPT`, `MASTERMIND_POSTURE_DECIDER`, `MASTERMIND_REPUTATION_WEIGHTING`, `MASTERMIND_REQUIRE_AUTH`, `MASTERMIND_RESEARCH_LLM`, `MASTERMIND_RISKOFF_GROSS`, `MASTERMIND_RISKOFF_SCORE`, `MASTERMIND_RISK_GOVERNOR`, `MASTERMIND_RISK_OFFICER`, `MASTERMIND_SELECTION_EXPLORE`, `MASTERMIND_SELF_MIRROR`, `MASTERMIND_SELF_TUNE`, `MASTERMIND_SESSION_DAYS`, `MASTERMIND_STUDENT`, `MASTERMIND_TIMING_GATE`, `MASTERMIND_V`

## D. API Endpoints

**68 routes** across `app/main.py` + `app/web.py`

| method | path | open | LLM |
|---|---|---|---|
| GET | `/health` | open |  |
| GET | `/regime` | auth |  |
| POST | `/reason` | auth | LLM |
| POST | `/daily` | auth | LLM |
| POST | `/api/autonomous/run` | auth | LLM |
| POST | `/api/heavyweight/run` | auth | LLM |
| POST | `/api/china/run` | auth | LLM |
| POST | `/api/hk/run` | auth | LLM |
| POST | `/api/etf/run` | auth | LLM |
| POST | `/research` | auth | LLM |
| POST | `/chat` | auth | LLM |
| GET | `/chat/history` | auth |  |
| GET | `/chat/paper` | auth |  |
| GET | `/` | auth |  |
| GET | `/research` | auth |  |
| GET | `/self` | auth |  |
| GET | `/desk` | auth |  |
| GET | `/market_view` | auth |  |
| GET | `/agenda` | auth |  |
| GET | `/theme.css` | auth |  |
| GET | `/theme.js` | auth |  |
| GET | `/chat.js` | auth |  |
| GET | `/research_paper.pdf` | auth |  |
| GET | `/api/performance` | auth |  |
| GET | `/api/risk` | auth |  |
| GET | `/api/portfolio` | auth |  |
| GET | `/api/portfolios` | auth |  |
| GET | `/api/decisions` | auth |  |
| GET | `/api/posture` | auth |  |
| GET | `/api/market_view` | auth |  |
| GET | `/api/agenda` | auth |  |
| GET | `/api/etf/outcomes` | auth |  |
| GET | `/api/overnight-tape` | auth |  |
| GET | `/api/research` | auth |  |
| GET | `/api/research_papers` | auth |  |
| GET | `/api/outcome_ledger` | auth |  |
| GET | `/api/trades` | auth |  |
| GET | `/api/self_directed` | auth |  |
| GET | `/api/self_directed/history` | auth |  |
| GET | `/api/self_directed/search` | auth |  |
| GET | `/api/self_directed/quote` | auth |  |
| POST | `/api/self_directed/order` | auth |  |
| POST | `/api/self_directed/thesis` | auth |  |
| POST | `/api/self_directed/cancel` | auth |  |
| GET | `/api/outcomes` | auth |  |
| GET | `/api/shadow_books` | auth |  |
| GET | `/api/predictions` | auth |  |
| GET | `/api/rejections` | auth |  |
| GET | `/api/shadow_bandit` | auth |  |
| GET | `/api/student` | auth |  |
| GET | `/api/distill` | auth |  |
| GET | `/api/interim_marks` | auth |  |
| GET | `/api/engine_backtest` | auth |  |
| GET | `/api/factor_zoo` | auth |  |
| GET | `/api/fundamentals` | auth |  |
| GET | `/api/readiness` | auth |  |
| GET | `/api/macro` | auth |  |
| GET | `/api/activity` | auth |  |
| GET | `/api/runs` | auth |  |
| GET | `/api/runlog` | auth |  |
| GET | `/api/competitors` | auth |  |
| GET | `/api/desk/strategist` | auth |  |
| GET | `/api/desk/decisions` | auth |  |
| GET | `/api/desk/watchlist` | auth |  |
| GET | `/api/desk/scorecard` | auth |  |
| GET | `/api/desk/macro-risk` | auth |  |
| GET | `/api/desk/firm-exposure` | auth |  |
| GET | `/api/desk/experiments` | auth |  |

## E. External Artifact Read Paths

### `portfolio.lenses` (17 paths)
- `data/altdata/by_ticker.json`
- `data/altdata/mastermind.json`
- `data/flow/mastermind.json`
- `data/intelligence/by_ticker.json`
- `data/news/by_ticker.json`
- `data/policy/intel.json`
- `data/regime/latest.json`
- `data/vol/mastermind.json`
- `site/allocationdata/allocation.json`
- `site/altdata/by_ticker.json`
- `site/altdata/mastermind.json`
- `site/basketdata/baskets.json`
- `site/flow/mastermind.json`
- `site/intelligence/by_ticker.json`
- `site/news/by_ticker.json`
- `site/stockdata/fund_flows.json`
- `site/vol/mastermind.json`

### `brain.intake` (5 paths)
- `altdata/mastermind.json`
- `basketdata/radar_ticker.json`
- `factordata/us_standouts.json`
- `intelligence/briefing.json`
- `news/by_ticker.json`

### `brain.china_intake` (6 paths)
- `china_regime/latest.json`
- `factordata/china_alpha.json`
- `factordata/china_reversal.json`
- `factordata/china_setups.json`
- `factordata/china_standouts.json`
- `factordata/hk_standouts.json`

### `brain.etf_board` (2 paths)
- `data/regime/latest.json`
- `data/transmission/latest.json`

### `brain.gate_officer` (0 paths)
_(no artifact paths detected)_

### `brain.regime_frame` (4 paths)
- `data/china_regime/latest.json`
- `data/regime/latest.json`
- `data/risk_radar/forward_log.jsonl`
- `site/sectordata/sector_cycles.json`

### `data_layer.macro_refresh` (4 paths)
- `data/regime/latest.json`
- `site/factordata/us_standouts.json`
- `site/sectordata/sector_cycles.json`
- `site/stockdata/SPY.json`

### `brain.neural_web_context` (0 paths)
_(no artifact paths detected)_

## F. GuardrailResult Construction Sites

_(none found — GuardrailResult is defined but not yet called outside tests and its own module. L2 lane will add call sites.)_
