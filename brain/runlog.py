"""Run-scoped logger — DEEP, granular trace of every bot session.

Every call, result, reasoning chunk, and trade step is written as a JSON-lines
row to data/brain/runs/<run_id>.jsonl.  An index file (index.jsonl) is kept
separately so list_runs() is O(n_runs) not O(n_steps).

Thread/process safety: each step append is a single write()+flush(); the index
update renames an atomic temp file.  Crashes between lines leave an incomplete
.jsonl but never corrupt existing data.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_RUNS_DIR = Path(__file__).resolve().parent.parent / "data" / "brain" / "runs"
_INDEX = _RUNS_DIR / "index.jsonl"
_LOCK = threading.Lock()          # index writes are rare; single lock is fine


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _run_path(run_id: str) -> Path:
    return _RUNS_DIR / f"{run_id}.jsonl"


def _ensure_dir():
    _RUNS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def start_run(kind: str, title: str = "") -> str:
    """Open a new run; return its run_id (timestamp-based, sortable)."""
    _ensure_dir()
    ts = _now()
    # run_id = UTC timestamp without colons/dots so it's a safe filename
    run_id = ts.replace(":", "-").replace(".", "-").replace("+", "Z")
    meta = {"run_id": run_id, "ts": ts, "kind": kind, "title": title or kind,
            "n_steps": 0, "cost_usd": None, "summary": ""}
    # write seed row to the run file (makes the file exist even if no steps follow)
    _run_path(run_id).write_text(json.dumps({"_meta": meta}) + "\n", encoding="utf-8")
    return run_id


def log_step(run_id: str, type: str, title: str, detail: str, **extra) -> None:  # noqa: A002
    """Append one step to the run file.  Never raises — errors are swallowed."""
    try:
        step: dict[str, Any] = {"ts": _now(), "type": type, "title": title,
                                 "detail": detail[:4000], **extra}
        line = json.dumps(step, default=str) + "\n"
        path = _run_path(run_id)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
    except Exception:
        pass   # logging must never break the session


def end_run(run_id: str, summary: str = "", cost_usd: float | None = None) -> None:
    """Finalise a run: count its steps, write a summary, update the index."""
    try:
        _ensure_dir()
        path = _run_path(run_id)
        n_steps = 0
        ts = _now()
        kind = "daily"
        title = run_id

        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
            for raw in lines:
                try:
                    obj = json.loads(raw)
                    if "_meta" in obj:
                        ts = obj["_meta"].get("ts", ts)
                        kind = obj["_meta"].get("kind", kind)
                        title = obj["_meta"].get("title", title)
                    else:
                        n_steps += 1
                except Exception:
                    pass

        entry = {"run_id": run_id, "ts": ts, "kind": kind, "title": title,
                 "n_steps": n_steps, "cost_usd": cost_usd, "summary": summary}

        with _LOCK:
            # read existing index
            rows: list[dict] = []
            if _INDEX.exists():
                for raw in _INDEX.read_text(encoding="utf-8").splitlines():
                    try:
                        obj = json.loads(raw)
                        if obj.get("run_id") != run_id:   # dedupe
                            rows.append(obj)
                    except Exception:
                        pass
            rows.append(entry)
            # atomic write via rename
            fd, tmp = tempfile.mkstemp(dir=_RUNS_DIR, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    for r in rows:
                        fh.write(json.dumps(r, default=str) + "\n")
                os.replace(tmp, _INDEX)
            except Exception:
                try:
                    os.unlink(tmp)
                except Exception:
                    pass
                raise
    except Exception:
        pass   # never breaks the caller


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------

def list_runs() -> list[dict]:
    """Return all run index entries, newest first."""
    if not _INDEX.exists():
        return []
    try:
        rows = []
        for raw in _INDEX.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(raw))
            except Exception:
                pass
        # newest first by ts
        rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return rows
    except Exception:
        return []


def read_run(run_id: str | None = None) -> dict:
    """Return {run_id, ts, kind, steps:[...]} for the given run (or the latest)."""
    try:
        if run_id is None:
            idx = list_runs()
            if not idx:
                return {"run_id": None, "ts": None, "kind": None, "steps": []}
            run_id = idx[0]["run_id"]

        path = _run_path(run_id)
        if not path.exists():
            return {"run_id": run_id, "ts": None, "kind": None, "steps": [],
                    "error": "run file not found"}

        ts, kind, title = None, None, run_id
        steps = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            try:
                obj = json.loads(raw)
                if "_meta" in obj:
                    ts = obj["_meta"].get("ts")
                    kind = obj["_meta"].get("kind")
                    title = obj["_meta"].get("title", run_id)
                else:
                    steps.append(obj)
            except Exception:
                pass
        return {"run_id": run_id, "ts": ts, "kind": kind, "title": title, "steps": steps}
    except Exception as exc:
        return {"run_id": run_id, "ts": None, "kind": None, "steps": [], "error": str(exc)}
