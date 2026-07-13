"""brain/mastermind_ai.py — the Mastermind AI self-improvement loop coordinator (W-AI).

One module owns the loop the operator sees in the admin panel: each nightly tick runs the
observational self-improvement cycle (journal drafting + pin recompute + NW reflection), writes a
loop-log row, and every N loops writes a review with a deterministic progress assessment. It also
owns the operator-adjustable settings and the operator→orchestrator directive queue.

AUTHORITY: none. The cycle writes files only — it never trades, never flips a flag, never mutates
a seat, a prompt, or a book. The only actors with any authority remain self_tune (through the Lab
harness gates, MASTERMIND_SELF_TUNE) and the operator.

FLAGS
  MASTERMIND_AI_LOOP        default ON  — the observational cycle (files only). '0' disables.
  MASTERMIND_AI_REVIEW_LLM  default OFF — allows an LLM prose assessment on review rows
                            (capability gate; the runtime setting llm_review must ALSO be on).

DATA LAYOUT (data/mastermind_ai/ — rsynced to the public mirror: counts/codes only, no prompts,
no sizes, no secrets; directive text is operator-authored and scrubbed on intake)
  settings.json    — operator overrides for the doctrine `mastermind_ai:` block (bounded keys).
  loop_log.jsonl   — one row per cycle {ts, asof, run_id, trigger, loop_n, steps, nudges_open,
                     summary, review?}.
  reviews.jsonl    — one row per N-loop review {ts, asof, loop_n, window, completed, assessment}.
  directives.jsonl — operator directives {id, ts, text, status: queued|published|acknowledged}.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_DIR = _ROOT / "data" / "mastermind_ai"
_SETTINGS = _DIR / "settings.json"
_LOOP_LOG = _DIR / "loop_log.jsonl"
_REVIEWS = _DIR / "reviews.jsonl"
_DIRECTIVES = _DIR / "directives.jsonl"

# ── settings (doctrine defaults + bounded operator overrides) ────────────────────────────────
_DEFAULTS: dict[str, Any] = {
    "loop_enabled": True,          # soft switch inside the MASTERMIND_AI_LOOP env gate
    "review_every_n_loops": 5,     # the user's "every five loops" review cadence
    "nudges_max": 10,              # cap on nudges emitted to the orchestrator
    "attribution_min_n": 12,       # cold-start guard for NW attribution
    "llm_review": False,           # runtime half of the LLM-review double gate
    "directives_max_open": 10,     # queued+published directives the publisher may carry
}
# bounds enforced on operator writes — a settings API must never widen its own surface
_BOUNDS: dict[str, tuple] = {
    "loop_enabled": (bool,),
    "review_every_n_loops": (int, 2, 50),
    "nudges_max": (int, 1, 10),
    "attribution_min_n": (int, 6, 100),
    "llm_review": (bool,),
    "directives_max_open": (int, 1, 10),
}

_DIRECTIVE_MAX_CHARS = 280
# intake refusal patterns (public artifact downstream): secrets, env names, $ amounts, key-shaped
_DIRECTIVE_DENY = [
    re.compile(r"MASTERMIND_[A-Z_]+"),
    re.compile(r"\$[\d,]+"),
    re.compile(r"(?i)\b[A-Za-z0-9+/]{40,}\b"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]"),
]


def loop_flag_on() -> bool:
    return os.environ.get("MASTERMIND_AI_LOOP", "1").strip().lower() in ("1", "true", "yes", "on")


def llm_review_flag_on() -> bool:
    return os.environ.get("MASTERMIND_AI_REVIEW_LLM", "0").strip().lower() in ("1", "true", "yes", "on")


def _doctrine_block() -> dict:
    try:
        from bot.doctrine_config import load_doctrine
        return load_doctrine().get("mastermind_ai") or {}
    except Exception:  # noqa: BLE001
        return {}


def _overrides() -> dict:
    try:
        if _SETTINGS.exists():
            v = json.loads(_SETTINGS.read_text())
            return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _coerce(key: str, value: Any) -> Any | None:
    """Validate one settings value against _BOUNDS; None = rejected."""
    spec = _BOUNDS.get(key)
    if not spec:
        return None
    if spec[0] is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, str)):
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        return None
    if spec[0] is int:
        if isinstance(value, bool):
            return None   # int(True)==1 would silently pass bounds — reject bools outright
        try:
            iv = int(value)
        except Exception:  # noqa: BLE001
            return None
        lo, hi = spec[1], spec[2]
        return iv if lo <= iv <= hi else None
    return None


def settings() -> dict:
    """Effective settings: defaults ← doctrine block ← operator overrides (all bounded)."""
    out = dict(_DEFAULTS)
    for src in (_doctrine_block(), _overrides()):
        for k in _DEFAULTS:
            if k in src:
                v = _coerce(k, src[k])
                if v is not None:
                    out[k] = v
    return out


def update_settings(patch: dict) -> dict:
    """Apply a bounded operator patch to settings.json. Returns {ok, settings, rejected}."""
    rejected: list[str] = []
    try:
        cur = _overrides()
        for k, v in (patch or {}).items():
            if k not in _DEFAULTS:
                rejected.append(str(k))
                continue
            cv = _coerce(k, v)
            if cv is None:
                rejected.append(str(k))
                continue
            cur[k] = cv
        _DIR.mkdir(parents=True, exist_ok=True)
        _SETTINGS.write_text(json.dumps(cur, indent=2))
        return {"ok": True, "settings": settings(), "rejected": rejected}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}", "settings": settings(),
                "rejected": rejected}


# ── jsonl helpers ─────────────────────────────────────────────────────────────────────────────

def _read_jsonl(p: Path, limit: int | None = None) -> list[dict]:
    try:
        if not p.exists():
            return []
        rows = []
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(r, dict):
                rows.append(r)
        return rows[-limit:] if limit else rows
    except Exception:  # noqa: BLE001
        return []


def _append_jsonl(p: Path, row: dict) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── directives (operator → orchestrator) ─────────────────────────────────────────────────────

def add_directive(text: str) -> dict:
    """Queue a scrubbed operator directive for the next feedback publish. Returns {ok,...}."""
    try:
        t = str(text or "").strip()
        if not t:
            return {"ok": False, "error": "empty"}
        if len(t) > _DIRECTIVE_MAX_CHARS:
            return {"ok": False, "error": f"too long (max {_DIRECTIVE_MAX_CHARS} chars)"}
        for pat in _DIRECTIVE_DENY:
            if pat.search(t):
                return {"ok": False,
                        "error": "directive matches a public-surface deny pattern "
                                 "(secrets / env names / $ amounts are not publishable)"}
        open_n = sum(1 for d in _read_jsonl(_DIRECTIVES)
                     if d.get("status") in ("queued", "published"))
        if open_n >= settings()["directives_max_open"]:
            return {"ok": False, "error": "too many open directives — wait for acknowledgement"}
        row = {
            "id": hashlib.sha256(f"{_now_iso()}|{t}".encode()).hexdigest()[:16],
            "ts": _now_iso(),
            "text": t,
            "status": "queued",
        }
        _append_jsonl(_DIRECTIVES, row)
        return {"ok": True, "directive": row}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


def directives(limit: int = 50) -> list[dict]:
    """Latest-status view of the directive queue (last write per id wins)."""
    by_id: dict[str, dict] = {}
    for row in _read_jsonl(_DIRECTIVES):
        rid = row.get("id")
        if rid:
            by_id[rid] = {**by_id.get(rid, {}), **row}
    rows = sorted(by_id.values(), key=lambda r: r.get("ts", ""))
    return rows[-limit:]


def _advance_directive(rid: str, status: str) -> None:
    _append_jsonl(_DIRECTIVES, {"id": rid, "ts": _now_iso(), "status": status})


def open_directives_for_publish() -> list[dict]:
    """The rows bridge/nw_feedback ships (queued+published, capped FIFO).

    Truncates FIRST (oldest-first, so early directives are never starved), then advances
    queued→published ONLY for rows actually included in the publish payload — a row must
    never read 'published' unless it shipped (review finding, 2026-07-13)."""
    out: list[dict] = []
    try:
        cap = settings()["directives_max_open"]
        open_rows = [d for d in directives()
                     if d.get("status") in ("queued", "published") and d.get("text")]
        for d in open_rows[:cap]:   # directives() is ts-ascending → FIFO
            out.append({"id": d["id"], "created": d.get("ts", "")[:10], "text": d["text"]})
            if d.get("status") == "queued":
                _advance_directive(d["id"], "published")
        return out
    except Exception:  # noqa: BLE001
        return out


def reconcile_ack() -> dict:
    """Advance directive statuses from the macro ack (lobes.mastermind_ai in the NW context)."""
    try:
        from brain import neural_web_context as nwc
        # The context reader caches for the process lifetime; without a reset a long-lived
        # process could never observe an ack published after its first read (review finding).
        try:
            nwc._reset_context_cache()
        except Exception:  # noqa: BLE001
            pass
        lobe = (nwc.context() or {}).get("lobes", {}).get("mastermind_ai") or {}
        ack = lobe.get("ack") or {}
        seen_ids = set(ack.get("directive_ids_seen") or [])
        seen_codes = set(ack.get("nudge_codes_seen") or [])
        advanced = 0
        for d in directives():
            if d.get("status") == "published" and d.get("id") in seen_ids:
                _advance_directive(d["id"], "acknowledged")
                advanced += 1
        return {"state": "ok" if lobe else "absent",
                "directives_acknowledged": advanced,
                "nudge_codes_seen_n": len(seen_codes)}
    except Exception:  # noqa: BLE001
        return {"state": "absent", "directives_acknowledged": 0, "nudge_codes_seen_n": 0}


# ── the cycle ─────────────────────────────────────────────────────────────────────────────────

def _self_tune_state() -> dict:
    try:
        p = _ROOT / "data" / "self_tune" / "state.json"
        if p.exists():
            v = json.loads(p.read_text())
            if isinstance(v, dict):
                fams = v.get("families") or {}
                return {"families_n": len(fams),
                        "locked": [k for k, f in fams.items()
                                   if isinstance(f, dict) and f.get("proposal_only")],
                        "armed": bool(os.environ.get("MASTERMIND_SELF_TUNE", "0").strip()
                                      .lower() in ("1", "true", "yes", "on"))}
    except Exception:  # noqa: BLE001
        pass
    return {"families_n": 0, "locked": [], "armed": False}


def _journal_counts() -> dict:
    out = {"drafts": 0, "pending": 0, "lessons": 0, "pins_active": 0, "pins_unpinned": 0}
    try:
        from brain import journal
        for seat in journal.SEATS:
            out["drafts"] += len(journal.load_drafts(seat))
            out["pending"] += len(journal.pending_for(seat))
            out["lessons"] += len(journal.load_lessons(seat))
            for pin in journal.load_pins(seat):
                if pin.get("status") == "active":
                    out["pins_active"] += 1
                elif pin.get("status") == "unpinned":
                    out["pins_unpinned"] += 1
    except Exception:  # noqa: BLE001
        pass
    return out


def _agenda_top(n: int = 3) -> list[str]:
    try:
        from brain import improvement_agenda as agenda
        rep = agenda.latest() or {}
        return [str(it.get("title", ""))[:120] for it in (rep.get("items") or [])[:n]]
    except Exception:  # noqa: BLE001
        return []


def run_cycle(asof: date | str | None = None, trigger: str = "manual") -> dict:
    """One self-improvement tick. Observational only; never raises. Returns the loop-log row."""
    asof_s = (asof.isoformat() if isinstance(asof, date) else str(asof)) if asof else date.today().isoformat()
    if not (loop_flag_on() and settings()["loop_enabled"]):
        return {"ok": False, "skipped": "loop disabled", "asof": asof_s}
    cfg = settings()
    steps: dict[str, Any] = {}

    # 1. journal maintenance (idempotent, deterministic — the seats still own their lessons)
    try:
        from brain import journal
        d = journal.draft_all()
        steps["journal_drafts_added"] = sum(v for v in d.values() if isinstance(v, int)) if isinstance(d, dict) else 0
        pins_changed = 0
        for seat in journal.SEATS:
            try:
                pins_changed += len(journal.recompute_pins(seat) or [])
            except Exception:  # noqa: BLE001
                continue
        steps["pins_recomputed"] = pins_changed
    except Exception as exc:  # noqa: BLE001
        steps["journal_error"] = type(exc).__name__

    # 2. NW reflection (the dialogue substrate)
    try:
        from brain import nw_reflection
        rep = nw_reflection.persist(asof_s, nudges_max=cfg["nudges_max"],
                                    attribution_min_n=cfg["attribution_min_n"])
        steps["reflection"] = {
            "drift_n": len(rep.get("contract_drift") or []),
            "nudges_n": len(rep.get("nudges") or []),
            "coverage_rate": (rep.get("coverage") or {}).get("coverage_rate"),
            "seen_rate": (rep.get("context_quality") or {}).get("seen_rate"),
        }
        nudges_open = len(rep.get("nudges") or [])
    except Exception as exc:  # noqa: BLE001
        steps["reflection_error"] = type(exc).__name__
        nudges_open = 0

    # 3. ack reconciliation (macro → bot half of the dialogue)
    steps["ack"] = reconcile_ack()

    # 4. state snapshots for the log
    jc = _journal_counts()
    steps["journal"] = jc
    steps["self_tune"] = _self_tune_state()
    steps["agenda_top"] = _agenda_top()

    loop_n = len(_read_jsonl(_LOOP_LOG)) + 1
    summary = (f"loop {loop_n}: {steps.get('journal_drafts_added', 0)} new journal drafts, "
               f"{jc['pending']} lessons pending, {jc['pins_active']} pinned rules, "
               f"{nudges_open} open nudges to the orchestrator"
               + (f", {steps['ack']['directives_acknowledged']} directives acknowledged"
                  if steps.get("ack", {}).get("directives_acknowledged") else ""))

    row: dict[str, Any] = {
        "ts": _now_iso(), "asof": asof_s, "trigger": trigger, "loop_n": loop_n,
        "steps": steps, "nudges_open": nudges_open, "summary": summary, "ok": True,
    }
    try:
        from brain import runlog as _runlog  # optional run id correlation
        row["run_id"] = getattr(_runlog, "current_run_id", lambda: None)() or None
    except Exception:  # noqa: BLE001
        pass

    # 5. every-N-loops review
    try:
        if loop_n % max(2, int(cfg["review_every_n_loops"])) == 0:
            review = _build_review(loop_n, cfg)
            _append_jsonl(_REVIEWS, review)
            row["review"] = {"loop_n": loop_n, "assessment_n": len(review.get("assessment") or [])}
    except Exception:  # noqa: BLE001
        pass

    _append_jsonl(_LOOP_LOG, row)
    return row


def _trend(first, last) -> str:
    try:
        if first is None or last is None:
            return "flat"
        return "up" if last > first else ("down" if last < first else "flat")
    except Exception:  # noqa: BLE001
        return "flat"


def _build_review(loop_n: int, cfg: dict) -> dict:
    """Deterministic N-loop review: what was completed + an honest progress assessment."""
    n = max(2, int(cfg["review_every_n_loops"]))
    window = _read_jsonl(_LOOP_LOG, limit=n - 1)  # current row not yet appended
    completed = {
        "loops": n,
        "journal_drafts_added": sum(int(r.get("steps", {}).get("journal_drafts_added") or 0)
                                    for r in window),
        "directives_acknowledged": sum(int(r.get("steps", {}).get("ack", {})
                                           .get("directives_acknowledged") or 0) for r in window),
    }
    assessment: list[str] = []

    # nudge trajectory
    hist = []
    try:
        from brain import nw_reflection
        hist = nw_reflection._read_jsonl(nw_reflection._HISTORY, limit=n)
    except Exception:  # noqa: BLE001
        pass
    if len(hist) >= 2:
        first, last = hist[0], hist[-1]
        assessment.append(f"open nudges {_trend(first.get('nudges_n'), last.get('nudges_n'))} "
                          f"({first.get('nudges_n')} -> {last.get('nudges_n')}); "
                          f"context seen-rate {_trend(first.get('seen_rate'), last.get('seen_rate'))} "
                          f"({first.get('seen_rate')} -> {last.get('seen_rate')}); "
                          f"coverage {_trend(first.get('coverage_rate'), last.get('coverage_rate'))} "
                          f"({first.get('coverage_rate')} -> {last.get('coverage_rate')})")

    # learning-floor status (W-L falsifiers, honestly cold-start)
    try:
        from brain import outcome_ledger
        s = outcome_ledger.summary() or {}
        assessment.append(f"outcome ledger: n={s.get('n', 0)}, "
                          f"hit_rate={s.get('hit_rate')}, brier={s.get('brier')} "
                          f"(status {'scoring' if (s.get('n') or 0) >= 12 else 'building'})")
    except Exception:  # noqa: BLE001
        pass
    jc = _journal_counts()
    assessment.append(f"journal: {jc['lessons']} lessons banked, {jc['pins_active']} rules pinned "
                      f"({jc['pins_unpinned']} unpinned by their own falsifiers), "
                      f"{jc['pending']} lessons still owed by seats")
    st = _self_tune_state()
    assessment.append(f"self_tune: {'ARMED' if st['armed'] else 'dark'}, "
                      f"{st['families_n']} families tracked, {len(st['locked'])} locked proposal-only")

    review = {
        "ts": _now_iso(), "loop_n": loop_n, "window_loops": n,
        "completed": completed, "assessment": assessment,
        "agenda_top": _agenda_top(5),
    }

    # optional LLM prose (double-gated: env capability flag AND runtime setting).
    # reason_sync (reason() is async — a bare call returns a coroutine); allowed_tools=[] so the
    # model sees ONLY the deterministic counts JSON in the prompt, never the repo/ledgers; and the
    # output is deny-swept before it lands in reviews.jsonl, which rsyncs to the PUBLIC mirror —
    # a single pattern hit rejects the whole assessment (review finding, 2026-07-13).
    if llm_review_flag_on() and cfg.get("llm_review"):
        try:
            from brain import cli_bridge
            prompt = ("You are the Mastermind AI reviewing your own self-improvement loop. "
                      "In <=150 words, assess progress honestly (no alpha claims): "
                      + json.dumps({"completed": completed, "assessment": assessment})[:4000])
            res = cli_bridge.reason_sync(prompt, role="analyst", max_turns=1,
                                         allowed_tools=[], log_run=False)
            text = str(res.get("text") or "")[:2000] if isinstance(res, dict) and res.get("ok") else ""
            if text and not any(p.search(text) for p in _DIRECTIVE_DENY):
                review["llm_assessment"] = text
        except Exception:  # noqa: BLE001
            pass
    return review


# ── read surfaces for the API/admin ───────────────────────────────────────────────────────────

def loop_log(limit: int = 50) -> list[dict]:
    return _read_jsonl(_LOOP_LOG, limit=limit)


def reviews(limit: int = 12) -> list[dict]:
    return _read_jsonl(_REVIEWS, limit=limit)


def improvements() -> dict:
    """The merged 'what has actually improved' feed for the admin panel."""
    out: dict[str, Any] = {"pins": [], "self_tune": _self_tune_state(),
                           "agenda_top": _agenda_top(10), "lessons_by_taxonomy": {}}
    try:
        from brain import journal
        for seat in journal.SEATS:
            for pin in journal.load_pins(seat):
                out["pins"].append({"seat": seat, "taxonomy": pin.get("taxonomy"),
                                    "rule": str(pin.get("rule", ""))[:200],
                                    "status": pin.get("status"),
                                    "confidence": pin.get("confidence"),
                                    "pinned_on": pin.get("pinned_on"),
                                    "unpin_reason": pin.get("unpin_reason")})
            for les in journal.load_lessons(seat):
                tax = les.get("why_wrong") or ("success" if les.get("kind") == "success" else "other")
                out["lessons_by_taxonomy"][tax] = out["lessons_by_taxonomy"].get(tax, 0) + 1
    except Exception:  # noqa: BLE001
        pass
    return out


def status() -> dict:
    """The one-call admin snapshot."""
    try:
        from brain import nw_reflection
        reflection = nw_reflection.latest()
    except Exception:  # noqa: BLE001
        reflection = {}
    log_rows = loop_log(limit=5)
    return {
        "schema": "mastermind_ai_status.v1",
        "generated_at": _now_iso(),
        "flags": {"loop": loop_flag_on(), "llm_review": llm_review_flag_on()},
        "settings": settings(),
        "loop_n": (log_rows[-1].get("loop_n") if log_rows else 0),
        "last_loops": log_rows,
        "last_review": (reviews(limit=1) or [None])[-1],
        "reflection": {
            "asof": reflection.get("asof"),
            "nudges": reflection.get("nudges") or [],
            "contract_drift_n": len(reflection.get("contract_drift") or []),
            "coverage": reflection.get("coverage") or {},
            "context_quality": reflection.get("context_quality") or {},
            "attribution": reflection.get("attribution") or {},
        },
        "journal": _journal_counts(),
        "directives": directives(limit=20),
    }
