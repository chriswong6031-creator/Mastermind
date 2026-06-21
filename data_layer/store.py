"""System-of-record store. Postgres in production (DATABASE_URL), SQLite locally.

The Postgres DDL lives in sql/0001_schema.sql + sql/0002_doctrine.sql. For zero-infra
local Phase 2 this uses SQLite with an equivalent minimal schema (JSONB->TEXT,
BIGSERIAL->INTEGER PK). Same API either way so the orchestrator is backend-agnostic.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

_DB = Path(__file__).resolve().parent.parent / "data" / "bot.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS theses (
  id TEXT PRIMARY KEY, logged_at TEXT, state_asof TEXT, subject TEXT, lean TEXT,
  conviction TEXT, prob_correct REAL, horizon_d INTEGER, falsifier TEXT, check_by TEXT,
  entry_levels TEXT, status TEXT DEFAULT 'open', sleeve TEXT, stage INTEGER,
  scorecard_dims TEXT, bottleneck TEXT, time_stop_by TEXT);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, asof TEXT, ticker TEXT, theme_id TEXT, sleeve TEXT,
  stage INTEGER, weight REAL, size_pct INTEGER, verdict TEXT, cycle_blocked INTEGER DEFAULT 0,
  dd_tail REAL, time_stop_by TEXT, thesis_id TEXT, reason TEXT, UNIQUE(asof, ticker));
CREATE TABLE IF NOT EXISTS track_record (
  asof TEXT PRIMARY KEY, n INTEGER, hits INTEGER, hit_rate REAL, brier REAL, skill REAL, status TEXT);
CREATE TABLE IF NOT EXISTS detectors (
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, mode TEXT, subject TEXT, fired_at TEXT,
  severity TEXT, payload TEXT, resolved INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS runs (
  asof TEXT PRIMARY KEY, ran INTEGER, triggers TEXT, state_sig TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS trials (
  id INTEGER PRIMARY KEY AUTOINCREMENT, spec_hash TEXT, scored_at TEXT, dsr REAL,
  verdict TEXT, sharpe REAL, maxdd REAL, beats_spy INTEGER, beats_6040 INTEGER,
  crisis_pass INTEGER, fold_robust INTEGER, p_value REAL, n_eff INTEGER);
CREATE TABLE IF NOT EXISTS holdout_touches (
  spec_hash TEXT PRIMARY KEY, touched_at TEXT, holdout_start TEXT, holdout_sharpe REAL);
CREATE TABLE IF NOT EXISTS promotions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, spec_hash TEXT, stage TEXT, promoted_at TEXT,
  dsr REAL, n_eff INTEGER, reason TEXT);
"""


def connect():
    if os.environ.get("DATABASE_URL"):
        raise NotImplementedError("Postgres path: psycopg.connect(DATABASE_URL) + sql/00*.sql")
    _DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(_DB)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def _dumps(v):
    return json.dumps(v, default=str) if isinstance(v, (dict, list)) else v


def insert_thesis(con, t: dict) -> bool:
    cur = con.execute("SELECT 1 FROM theses WHERE subject=? AND status='open'", (t["subject"],))
    if cur.fetchone():
        return False
    cols = ["id", "logged_at", "state_asof", "subject", "lean", "conviction", "prob_correct",
            "horizon_d", "falsifier", "check_by", "entry_levels", "status", "sleeve", "stage",
            "scorecard_dims", "bottleneck", "time_stop_by"]
    con.execute(f"INSERT OR IGNORE INTO theses ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
                [_dumps(t.get(c, "open" if c == "status" else None)) for c in cols])
    con.commit()
    return True


def upsert_position(con, asof: str, p: dict):
    cols = ["asof", "ticker", "theme_id", "sleeve", "stage", "weight", "size_pct", "verdict",
            "cycle_blocked", "dd_tail", "time_stop_by", "thesis_id", "reason"]
    row = {**p, "asof": asof}
    con.execute(
        f"INSERT INTO positions ({','.join(cols)}) VALUES ({','.join('?'*len(cols))}) "
        "ON CONFLICT(asof,ticker) DO UPDATE SET weight=excluded.weight, size_pct=excluded.size_pct, "
        "verdict=excluded.verdict, sleeve=excluded.sleeve, stage=excluded.stage, reason=excluded.reason",
        [_dumps(row.get(c)) for c in cols])
    con.commit()


def save_track_record(con, asof: str, tr: dict):
    con.execute("INSERT OR REPLACE INTO track_record (asof,n,hits,hit_rate,brier,skill,status) "
                "VALUES (?,?,?,?,?,?,?)",
                (asof, tr.get("n"), tr.get("hits"), tr.get("hit_rate"), tr.get("brier"),
                 tr.get("skill"), tr.get("status")))
    con.commit()


def record_run(con, asof: str, ran: bool, triggers: list, state_sig: str, at: str):
    # asof is the PK, so a same-day call would REPLACE the row. A carry-forward (ran=False) must
    # never clobber a successful (ran=True) run already recorded for that day — that corrupts the
    # audit trail / any "last actual rebuild" query. Use OR IGNORE for carry rows, OR REPLACE for runs.
    verb = "INSERT OR REPLACE" if ran else "INSERT OR IGNORE"
    con.execute(f"{verb} INTO runs (asof,ran,triggers,state_sig,at) VALUES (?,?,?,?,?)",
                (asof, int(ran), json.dumps(triggers), state_sig, at))
    con.commit()


def last_run(con) -> dict | None:
    r = con.execute("SELECT * FROM runs ORDER BY asof DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def positions(con, asof: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM positions WHERE asof=? ORDER BY weight DESC", (asof,))]


# ---------- self-improving loop state ----------
def record_trial(con, spec_hash: str, m: dict, n_eff: int, at: str):
    con.execute("INSERT INTO trials (spec_hash,scored_at,dsr,verdict,sharpe,maxdd,beats_spy,"
                "beats_6040,crisis_pass,fold_robust,p_value,n_eff) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (spec_hash, at, m["dsr"], m["dsr_verdict"], m["sharpe"], m["maxdd"],
                 int(m["beats_spy"]), int(m["beats_6040"]), int(m["crisis_pass"]),
                 int(m["fold_robust"]), m["p_value"], n_eff))
    con.commit()


def cumulative_trial_count(con) -> int:
    return con.execute("SELECT count(DISTINCT spec_hash) FROM trials").fetchone()[0]


def touched_set(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT spec_hash FROM holdout_touches")}


def add_holdout_touch(con, spec_hash: str, holdout_start: str, sharpe: float, at: str):
    con.execute("INSERT OR IGNORE INTO holdout_touches (spec_hash,touched_at,holdout_start,holdout_sharpe) "
                "VALUES (?,?,?,?)", (spec_hash, at, holdout_start, sharpe))
    con.commit()


def record_promotion(con, spec_hash: str, stage: str, dsr: float, n_eff: int, reason: str, at: str):
    con.execute("INSERT INTO promotions (spec_hash,stage,promoted_at,dsr,n_eff,reason) VALUES (?,?,?,?,?,?)",
                (spec_hash, stage, at, dsr, n_eff, reason))
    con.commit()
