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
no sizes, no secrets; directive text is operator-authored OR auto-drafted from a nudge template,
both scrubbed on intake)
  settings.json    — operator overrides for the doctrine `mastermind_ai:` block (bounded keys).
  loop_log.jsonl   — one row per cycle {ts, asof, run_id, trigger, loop_n, steps, nudges_open,
                     summary, review?}.
  reviews.jsonl    — one row per N-loop review {ts, asof, loop_n, window, completed, assessment}.
  directives.jsonl — operator directives {id, ts, text, source: operator|nudge:<code>,
                     status: queued|published|acknowledged|expired}.
  last_ack.json    — the last macro ack observed {ts, nudge_codes_seen, directive_ids_seen_n}.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_DIR = _ROOT / "data" / "mastermind_ai"
_SETTINGS = _DIR / "settings.json"
_LOOP_LOG = _DIR / "loop_log.jsonl"
_REVIEWS = _DIR / "reviews.jsonl"
_DIRECTIVES = _DIR / "directives.jsonl"
_LAST_ACK = _DIR / "last_ack.json"

# ── settings (doctrine defaults + bounded operator overrides) ────────────────────────────────
_DEFAULTS: dict[str, Any] = {
    "loop_enabled": True,          # soft switch inside the MASTERMIND_AI_LOOP env gate
    "review_every_n_loops": 5,     # the user's "every five loops" review cadence
    "nudges_max": 10,              # cap on nudges emitted to the orchestrator
    "attribution_min_n": 12,       # cold-start guard for NW attribution
    "llm_review": False,           # runtime half of the LLM-review double gate
    "directives_max_open": 10,     # queued+published directives the publisher may carry
    "directive_expiry_days": 14,   # published-but-never-acknowledged directives expire after N days
}
# bounds enforced on operator writes — a settings API must never widen its own surface
_BOUNDS: dict[str, tuple] = {
    "loop_enabled": (bool,),
    "review_every_n_loops": (int, 2, 50),
    "nudges_max": (int, 1, 10),
    "attribution_min_n": (int, 6, 100),
    "llm_review": (bool,),
    "directives_max_open": (int, 1, 10),
    "directive_expiry_days": (int, 3, 60),
}

_DIRECTIVE_MAX_CHARS = 280
# intake refusal patterns (public artifact downstream): secrets, env names, $ amounts, key-shaped.
# Each carries its category label — rejections name the CATEGORY, never echo the matched text.
_DIRECTIVE_DENY: list[tuple[str, re.Pattern]] = [
    ("env-flag name", re.compile(r"MASTERMIND_[A-Z_]+")),
    ("dollar amount", re.compile(r"\$[\d,]+")),
    ("key-shaped token", re.compile(r"(?i)\b[A-Za-z0-9+/]{40,}\b")),
    ("credential assignment", re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]")),
]

_NUDGE_CODE_RE = re.compile(r"^[a-z0-9_]{1,60}$")
_DIRECTIVE_SOURCE_RE = re.compile(r"^(operator|nudge:[a-z0-9_]{1,60})$")

# nudge code -> auto-drafted directive text (draft_directives_from_nudges). Public-surface law:
# every template must clear _DIRECTIVE_DENY and the 280 cap — add_directive re-gates them anyway
# (no scrub exemption for the auto-draft path; tested in tests/test_mastermind_ai.py).
_NUDGE_DIRECTIVE_TEMPLATES: dict[str, str] = {
    "fdr_cleared_absent": (
        "Restore kernel.fdr_cleared production in candidate_context — zero rows currently clear, "
        "which deadens the bot's entire typed decision ladder. "
        "(auto-drafted from nudge fdr_cleared_absent)"),
    "bottom_state_vocabulary_drift": (
        "Publish BOTTOMING/CONFIRMED bottom states when detected — the artifact only ever shows "
        "WATCH, so candidacy priors above WATCH can never fire. "
        "(auto-drafted from nudge bottom_state_vocabulary_drift)"),
    "graph_conflicts_sparse": (
        "Raise graph_conflicts coverage in candidate_context — almost no rows carry it, keeping "
        "conflict-aware entry logic dark. (auto-drafted from nudge graph_conflicts_sparse)"),
    "graph_conflicts_absent": (
        "Populate graph_conflicts in candidate_context — no rows carry it, so the entry-shrink "
        "and clean-in-conflicted legs can never fire. "
        "(auto-drafted from nudge graph_conflicts_absent)"),
    "liquidity_plumbing_absent": (
        "Populate the market lobe liquidity block — it ships all-null, so liquidity context is "
        "unusable. (auto-drafted from nudge liquidity_plumbing_absent)"),
    "candidate_context_empty": (
        "candidate_context published with zero rows — restore candidate production. "
        "(auto-drafted from nudge candidate_context_empty)"),
    "coverage_below_half": (
        "Fewer than half of the bot's decided names have a candidate_context row — widen "
        "candidate coverage toward the bot's active universe. "
        "(auto-drafted from nudge coverage_below_half)"),
    "context_stale_streak": (
        "The NW context artifact has been stale for 3+ consecutive bot runs — check the publish "
        "lane. (auto-drafted from nudge context_stale_streak)"),
    "context_absent_streak": (
        "The NW context artifact has been absent for 3+ consecutive bot runs — check the publish "
        "lane. (auto-drafted from nudge context_absent_streak)"),
    "gap_notes_elevated": (
        "The context audit reports elevated gap notes — review the gaps list in the latest "
        "audit. (auto-drafted from nudge gap_notes_elevated)"),
}


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


def _parse_ts(s: str) -> datetime | None:
    """Parse a _now_iso timestamp; None on failure (P2 — an unparseable ts never expires)."""
    try:
        return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


# ── directives (operator → orchestrator) ─────────────────────────────────────────────────────

def add_directive(text: str, source: str | None = None) -> dict:
    """Queue a scrubbed operator directive for the next feedback publish. Returns {ok,...}.

    source tags provenance ('operator' or 'nudge:<code>'; anything else degrades to
    'operator'). Auto-drafted texts pass the EXACT same gate as operator-typed ones."""
    try:
        t = str(text or "").strip()
        if not t:
            return {"ok": False, "error": "empty"}
        if len(t) > _DIRECTIVE_MAX_CHARS:
            return {"ok": False, "error": f"too long (max {_DIRECTIVE_MAX_CHARS} chars)"}
        for cat, pat in _DIRECTIVE_DENY:
            if pat.search(t):
                # name the category only — echoing the match would republish the very
                # string the pattern exists to keep off the public artifact
                return {"ok": False,
                        "error": f"directive matches a public-surface deny pattern ({cat})"}
        # cap on the FOLDED latest-status view — raw rows would double-count a directive's
        # queued row + its published delta, and an expired/acked row must free its slot
        open_n = sum(1 for d in directives(limit=500)
                     if d.get("status") in ("queued", "published"))
        if open_n >= settings()["directives_max_open"]:
            return {"ok": False, "error": "too many open directives — wait for acknowledgement"}
        src = source if isinstance(source, str) and _DIRECTIVE_SOURCE_RE.match(source) else "operator"
        row = {
            "id": hashlib.sha256(f"{_now_iso()}|{t}".encode()).hexdigest()[:16],
            "ts": _now_iso(),
            "text": t,
            "source": src,
            "status": "queued",
        }
        _append_jsonl(_DIRECTIVES, row)
        return {"ok": True, "directive": row}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


def _directive_text_for_nudge(code: str, detail: str) -> str:
    """Template text for a known nudge code; unknown codes get the truncated generic form."""
    t = _NUDGE_DIRECTIVE_TEMPLATES.get(code)
    if t:
        return t
    head = f"Address reflection finding {code}: "
    tail = ". (auto-drafted)"
    room = max(0, _DIRECTIVE_MAX_CHARS - len(head) - len(tail))
    body = str(detail or "").strip()
    # deny-scan the FULL detail before truncating — [:room] could cut a >=40-char key-shaped
    # run below the pattern floor and slip a credential prefix past add_directive's gate
    if any(p.search(body) for _, p in _DIRECTIVE_DENY):
        body = "detail withheld by the public-surface deny filter"
    return head + body[:room] + tail


def draft_directives_from_nudges(codes: list[str] | None = None) -> dict:
    """Draft directives from the currently open reflection nudges — operator ONE-CLICK bulk
    authoring. The click IS the authority: the loop never calls this on its own.

    codes missing/empty means all currently open nudges. Returns
    {ok, queued: [{id, code, text}], skipped: [{code, reason}], open_slots}."""
    try:
        from brain import nw_reflection
        open_nudges: dict[str, dict] = {}
        for n in (nw_reflection.latest().get("nudges") or []):
            if isinstance(n, dict) and n.get("code"):
                open_nudges.setdefault(str(n["code"]), n)
        skipped: list[dict] = []
        wanted: list[str] = []
        for c in ([str(c) for c in codes] if codes else list(open_nudges)):
            if c in wanted:
                continue
            if c not in open_nudges:
                skipped.append({"code": c[:60], "reason": "no such open nudge"})
                continue
            wanted.append(c)
        # highest-severity first, oldest first within a severity — the slots should go to
        # the longest-standing asks
        sev_rank = {"high": 0, "medium": 1, "low": 2}
        wanted.sort(key=lambda c: (sev_rank.get(open_nudges[c].get("severity"), 3),
                                   str(open_nudges[c].get("first_seen") or ""), c))
        view = directives(limit=500)
        open_sources = {d.get("source") for d in view
                        if d.get("status") in ("queued", "published")}
        slots = max(0, settings()["directives_max_open"]
                    - sum(1 for d in view if d.get("status") in ("queued", "published")))
        queued: list[dict] = []
        for c in wanted:
            if f"nudge:{c}" in open_sources:
                skipped.append({"code": c, "reason": "open directive already exists for this nudge"})
                continue
            if slots <= 0:
                skipped.append({"code": c, "reason": "no open directive slots"})
                continue
            res = add_directive(_directive_text_for_nudge(c, str(open_nudges[c].get("detail") or "")),
                                source=f"nudge:{c}")
            if res.get("ok"):
                d = res["directive"]
                queued.append({"id": d["id"], "code": c, "text": d["text"]})
                open_sources.add(f"nudge:{c}")
                slots -= 1
            else:
                skipped.append({"code": c, "reason": str(res.get("error") or "rejected")})
        return {"ok": True, "queued": queued, "skipped": skipped, "open_slots": slots}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": type(exc).__name__}


def directives(limit: int = 50) -> list[dict]:
    """Latest-status view of the directive queue (last write per id wins)."""
    by_id: dict[str, dict] = {}
    for row in _read_jsonl(_DIRECTIVES):
        rid = row.get("id")
        if rid:
            by_id[rid] = {**by_id.get(rid, {}), **row}
    # str() the key — a corrupt hand-edited ts (e.g. an int) must degrade the ordering,
    # never raise a TypeError up through status() and blank the whole admin snapshot
    rows = sorted(by_id.values(), key=lambda r: str(r.get("ts", "")))
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


def _write_last_ack(seen_codes: set, seen_ids: set) -> None:
    """Persist the ack sighting for dialogue_health. Fail-soft; codes re-validated —
    this file lands on the public mirror."""
    try:
        codes = sorted(c for c in seen_codes if isinstance(c, str) and _NUDGE_CODE_RE.match(c))
        _DIR.mkdir(parents=True, exist_ok=True)
        _LAST_ACK.write_text(json.dumps({
            "ts": _now_iso(),
            "nudge_codes_seen": codes,
            "directive_ids_seen_n": len(seen_ids),
        }, indent=2))
    except Exception:  # noqa: BLE001
        pass


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
        if ack:
            _write_last_ack(seen_codes, seen_ids)
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


def expire_stale_published() -> dict:
    """Expire published directives never acknowledged within directive_expiry_days.

    Judged on the LAST transition to published; appends 'expired' delta rows so the fold
    drops them from the publish wire and the open cap. Never raises."""
    out = {"expired": 0}
    try:
        days = int(settings()["directive_expiry_days"])
        cut = datetime.now(timezone.utc) - timedelta(days=days)
        last_pub: dict[str, str] = {}
        for row in _read_jsonl(_DIRECTIVES):
            if row.get("id") and row.get("status") == "published":
                last_pub[row["id"]] = row.get("ts", "")
        for d in directives(limit=500):
            if d.get("status") != "published":
                continue
            ts = _parse_ts(last_pub.get(d.get("id"), ""))
            if ts is not None and ts < cut:
                _advance_directive(d["id"], "expired")
                out["expired"] += 1
    except Exception:  # noqa: BLE001
        pass
    return out


def dialogue_health() -> dict:
    """The bot↔orchestrator dialogue health block for status() (W-AI.1 contract).

    counterparty is 'live' once ANY macro ack has ever been observed (loop-log ack state 'ok'
    or a persisted last_ack.json) — 'absent' means we have only ever spoken into the void."""
    out: dict[str, Any] = {"counterparty": "absent", "ack_seen_ever": False,
                           "loops_since_ack": None, "published_open_n": 0, "queued_n": 0,
                           "expired_n": 0, "oldest_published_age_days": None, "last_ack": None}
    try:
        rows = _read_jsonl(_LOOP_LOG)
        for i, r in enumerate(rows):
            if ((r.get("steps") or {}).get("ack") or {}).get("state") == "ok":
                out["ack_seen_ever"] = True
                out["loops_since_ack"] = len(rows) - 1 - i
        try:
            if _LAST_ACK.exists():
                v = json.loads(_LAST_ACK.read_text())
                if isinstance(v, dict):
                    out["last_ack"] = v
                    out["ack_seen_ever"] = True
        except Exception:  # noqa: BLE001
            pass
        out["counterparty"] = "live" if out["ack_seen_ever"] else "absent"
        last_pub: dict[str, str] = {}
        for row in _read_jsonl(_DIRECTIVES):
            if row.get("id") and row.get("status") == "published":
                last_pub[row["id"]] = row.get("ts", "")
        oldest: datetime | None = None
        for d in directives(limit=500):
            st = d.get("status")
            if st == "queued":
                out["queued_n"] += 1
            elif st == "published":
                out["published_open_n"] += 1
                ts = _parse_ts(last_pub.get(d.get("id"), ""))
                if ts is not None and (oldest is None or ts < oldest):
                    oldest = ts
            elif st == "expired":
                out["expired_n"] += 1
        if oldest is not None:
            out["oldest_published_age_days"] = max(0, (datetime.now(timezone.utc) - oldest).days)
    except Exception:  # noqa: BLE001
        pass
    return out


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
    # Titles land in loop_log.jsonl / reviews.jsonl, which rsync to the public mirror. Scrub
    # them like the LLM assessment: today every title is a code-authored count template, but a
    # future agenda source must not leak a secret / env name / $ amount through this seam.
    try:
        from brain import improvement_agenda as agenda
        rep = agenda.latest() or {}
        out: list[str] = []
        for it in (rep.get("items") or [])[:n]:
            title = str(it.get("title", ""))[:120]
            if any(p.search(title) for _, p in _DIRECTIVE_DENY):
                title = "agenda item withheld by the public-surface deny filter"
            out.append(title)
        return out
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

    # 3. ack reconciliation (macro → bot half of the dialogue) + directive expiry
    steps["ack"] = reconcile_ack()
    steps["directive_expiry"] = expire_stale_published()

    # 4. state snapshots for the log
    jc = _journal_counts()
    steps["journal"] = jc
    steps["self_tune"] = _self_tune_state()
    steps["agenda_top"] = _agenda_top()

    loop_rows = _read_jsonl(_LOOP_LOG)
    # loop_n survives log truncation/rotation: continue the last recorded counter, not row count
    try:
        loop_n = (int(loop_rows[-1].get("loop_n")) if loop_rows else 0) + 1
    except Exception:  # noqa: BLE001
        loop_n = len(loop_rows) + 1
    summary = (f"loop {loop_n}: {steps.get('journal_drafts_added', 0)} new journal drafts, "
               f"{jc['pending']} lessons pending, {jc['pins_active']} pinned rules, "
               f"{nudges_open} open nudges to the orchestrator"
               + (f", {steps['ack']['directives_acknowledged']} directives acknowledged"
                  if steps.get("ack", {}).get("directives_acknowledged") else ""))
    # one honest clause when nobody has been listening — the loop must not read as healthy
    # dialogue while every publish lands in the void
    try:
        if (steps.get("ack") or {}).get("state") != "ok":
            absent = 1
            for r in reversed(loop_rows):
                if ((r.get("steps") or {}).get("ack") or {}).get("state") == "ok":
                    break
                absent += 1
            if absent >= 3:
                summary += f"; macro ack absent {absent} loops"
    except Exception:  # noqa: BLE001
        pass

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
    dh = dialogue_health()
    if dh["counterparty"] == "absent":
        assessment.append(f"orchestrator dialogue: no macro ack ever seen — "
                          f"{dh['published_open_n']} directives published into the void, "
                          f"{dh['expired_n']} expired unacknowledged")
    else:
        assessment.append(f"orchestrator dialogue: counterparty live "
                          f"({dh['loops_since_ack']} loops since last ack), "
                          f"{dh['published_open_n']} published open, {dh['expired_n']} expired")

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
            if text and not any(p.search(text) for _, p in _DIRECTIVE_DENY):
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
        "dialogue": dialogue_health(),
    }
