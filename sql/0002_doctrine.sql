-- Mastermind — doctrine integration schema delta (applies after 0001)
-- Adds the thematic-rotation doctrine's per-lot fields + the detector feed.
-- All additive; nothing here is a scored alpha column.

BEGIN;

-- ---- per-position doctrine fields ----
ALTER TABLE positions ADD COLUMN IF NOT EXISTS sleeve TEXT
    CHECK (sleeve IN ('leadership','conviction','cash'));           -- 3-sleeve placement
ALTER TABLE positions ADD COLUMN IF NOT EXISTS stage SMALLINT       -- lifecycle Stage 0-4
    CHECK (stage BETWEEN 0 AND 4);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS order_layer SMALLINT  -- 1/2/3/4 derivative layer
    CHECK (order_layer BETWEEN 1 AND 4);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS scorecard_dims JSONB; -- {rs,breadth,flow,volume,catalyst,regime: confirmed|absent|unverified}
ALTER TABLE positions ADD COLUMN IF NOT EXISTS bottleneck JSONB;     -- {current_constraint, next_constraint, order_layer}
ALTER TABLE positions ADD COLUMN IF NOT EXISTS thesis_invalidation JSONB;  -- == the falsifier predicate (engine-derived)
ALTER TABLE positions ADD COLUMN IF NOT EXISTS time_stop_by DATE;    -- entry + window_td; the dead-capital clock
ALTER TABLE positions ADD COLUMN IF NOT EXISTS entry_asof DATE;      -- anchors the time-stop window

-- ---- the failure-mode detector feed (D1-D6) ----
CREATE TABLE IF NOT EXISTS detectors (
    id          BIGSERIAL PRIMARY KEY,
    code        TEXT NOT NULL CHECK (code IN ('D1','D2','D3','D4','D5','D6')),
    mode        TEXT NOT NULL CHECK (mode IN ('self','operator')),
    subject     TEXT NOT NULL,                  -- ticker or theme_id
    lot_id      BIGINT REFERENCES positions(id),-- null in operator mode (user's book)
    fired_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    severity    TEXT NOT NULL DEFAULT 'flag' CHECK (severity IN ('flag','veto')),
    unverified  BOOLEAN NOT NULL DEFAULT FALSE, -- A7: inferred vs observed input
    payload     JSONB,                          -- the evidence + the blunt advisory text
    resolved    BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS detectors_open_idx ON detectors (code, mode) WHERE resolved = FALSE;

COMMIT;
