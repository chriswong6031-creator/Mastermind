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
    con.execute("INSERT OR REPLACE INTO runs (asof,ran,triggers,state_sig,at) VALUES (?,?,?,?,?)",
                (asof, int(ran), json.dumps(triggers), state_sig, at))
    con.commit()


def last_run(con) -> dict | None:
    r = con.execute("SELECT * FROM runs ORDER BY asof DESC LIMIT 1").fetchone()
    return dict(r) if r else None


def positions(con, asof: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM positions WHERE asof=? ORDER BY weight DESC", (asof,))]
