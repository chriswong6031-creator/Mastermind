-- Mastermind — consolidated system-of-record schema (Phase 0/2)
-- Postgres owns mutable, accountable state. The vendored macro parquet store
-- stays append-only and read-only (queried via DuckDB), never written here.
--
-- Single sources of truth:
--   decision schema  = brain/decision.py (brain_decision.v1); `theses` is a projection
--   PIT corpus       = vendor/macro signal_archive (write-only, never scored = lookahead)
--   macro read       = data/regime/latest.json macro_risk (INVERTED for gross)

BEGIN;

-- ---------- the Claude brain ledger ----------
CREATE TABLE IF NOT EXISTS theses (
    id            TEXT PRIMARY KEY,                 -- e.g. '2026-06-19-1'
    logged_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    state_asof    DATE        NOT NULL,
    subject       TEXT        NOT NULL,
    lean          TEXT        NOT NULL,             -- overweight | avoid | hold | ...
    conviction    TEXT        NOT NULL,             -- high | medium | low
    prob_correct  REAL,                             -- [0.50, 0.85] after de-confidencing -> enables Brier
    horizon_d     INT         NOT NULL,
    falsifier     JSONB       NOT NULL,             -- engine-derived predicate {text, check{...}}
    check_by      DATE        NOT NULL,
    entry_levels  JSONB,
    status        TEXT        NOT NULL DEFAULT 'open' CHECK (status IN ('open','hit','miss','void'))
);

CREATE TABLE IF NOT EXISTS decision_journal (        -- = the scorer's graded rows
    id                    TEXT PRIMARY KEY REFERENCES theses(id),
    scored_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    outcome               TEXT NOT NULL,             -- hit | miss
    realized              REAL,                      -- realized rel-return at check_by
    directionally_correct BOOLEAN
);

CREATE TABLE IF NOT EXISTS track_record (            -- rolling Brier/skill = the paper->live gate
    asof          DATE PRIMARY KEY,
    n             INT  NOT NULL,
    hits          INT  NOT NULL,
    hit_rate      REAL,
    dir_accuracy  REAL,
    brier         REAL,
    skill         REAL
);

-- ---------- the self-improving loop ----------
CREATE TABLE IF NOT EXISTS candidates (
    spec_hash      TEXT PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    spec           JSONB NOT NULL,
    source         TEXT  NOT NULL CHECK (source IN ('algo','claude')),
    thesis_id      TEXT  REFERENCES theses(id),
    cluster_id     INT,
    n_eff_at_birth INT
);

CREATE TABLE IF NOT EXISTS trials (                  -- the PERSISTENT cumulative trial counter
    id          BIGSERIAL PRIMARY KEY,
    spec_hash   TEXT NOT NULL REFERENCES candidates(spec_hash),
    scored_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    dsr         NUMERIC,
    verdict     TEXT,
    sharpe      NUMERIC,
    maxdd       NUMERIC,
    beats_spy   BOOLEAN,
    beats_6040  BOOLEAN,
    crisis_pass BOOLEAN,
    bh_reject   BOOLEAN,
    fold_robust BOOLEAN,
    pbo         NUMERIC,
    cluster_id  INT,
    n_eff_used  INT
);

CREATE TABLE IF NOT EXISTS holdout_touches (         -- enforces touch <= once per spec_hash
    spec_hash      TEXT PRIMARY KEY REFERENCES candidates(spec_hash),
    touched_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    holdout_start  DATE NOT NULL,
    holdout_sharpe NUMERIC,
    holdout_maxdd  NUMERIC
);

CREATE TABLE IF NOT EXISTS promotions (
    id            BIGSERIAL PRIMARY KEY,
    spec_hash     TEXT NOT NULL REFERENCES candidates(spec_hash),
    stage         TEXT NOT NULL CHECK (stage IN ('paper','live-display','refused','demoted')),
    promoted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    dsr           NUMERIC,
    n_eff         INT,
    forward_brier NUMERIC,
    reason        TEXT
);

-- ---------- the paper portfolio ----------
CREATE TABLE IF NOT EXISTS positions (
    id                 BIGSERIAL PRIMARY KEY,
    asof               DATE NOT NULL,
    ticker             TEXT NOT NULL,
    theme_id           TEXT NOT NULL,
    state              TEXT NOT NULL CHECK (state IN ('candidate','open','trim','exit','blocked')),
    display_score      INT,                          -- 0..100, may be HIGH while w_target_pct=0
    composite_z        DOUBLE PRECISION,
    size_bucket        TEXT NOT NULL,                -- avoid/quarter/half/three-quarter/full
    w_target_pct       DOUBLE PRECISION NOT NULL,    -- post Kelly/ERC/gross; 0 allowed
    w_current_pct      DOUBLE PRECISION NOT NULL DEFAULT 0,
    gross_scalar       DOUBLE PRECISION,
    kelly_frac         DOUBLE PRECISION,             -- 0.50 or 0.25
    cycle_blocked      BOOLEAN NOT NULL DEFAULT FALSE,
    capped_by          TEXT,                         -- risk|entry|theme_cap|name_cap|erc
    ext_z              DOUBLE PRECISION,
    dd_tail            DOUBLE PRECISION,             -- p05 MAE, medium horizon
    buy_zone           DOUBLE PRECISION,
    stop               DOUBLE PRECISION,
    call_wall          DOUBLE PRECISION,
    entry_tag          TEXT,
    last_change_bd     INT NOT NULL DEFAULT 0,
    reason             JSONB,
    thesis_id          TEXT REFERENCES theses(id),
    strategy_spec_hash TEXT REFERENCES candidates(spec_hash),
    UNIQUE (asof, ticker)
);
CREATE INDEX IF NOT EXISTS positions_open_idx ON positions (state, asof)
    WHERE state IN ('open','trim');

COMMIT;
