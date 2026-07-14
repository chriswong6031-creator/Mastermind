"""brain/nw_reflection.py — the Mastermind AI reflection engine over the Neural Web bridge (W-AI).

Deterministic, offline, NO LLM, NO authority. Reads the published NW context (through
brain.neural_web_context — the sole reader), the bot's own graded ledgers, and the context-audit
sidecar, and answers one question: *are we using the Neural Web well, and where is the context
wrong, missing, or stale?* Its outputs are the substrate for the bot→macro nudge channel
(bridge/nw_feedback v3), the improvement agenda's `nw-context-drift` source, and the admin panel.

Report schema ``nw_reflection.v1``::

    {schema, asof, generated_at,
     contract_drift: [{code, field, status: dead|partial|ok|unknown, detail, severity}],
     coverage:       {open_theses_n, resolved_recent_n, with_context_row_n, coverage_rate,
                      context_rows_n, state},
     attribution:    {state: building|scoring, n_resolved, joinable_n, note},
     context_quality:{window_runs, n_present, n_stale, n_absent, seen_rate,
                      current_streak, gap_notes_latest, asof_lag_days_latest},
     nudges:         [{code, kind, severity, detail, first_seen, builds_seen}],
     nudges_dropped_n: int  (candidates cut by the max_n cap — dropped, never 'resolved'),
     nudges_resolved_recent: [{code, resolved_on}]  (<=5 newest tombstones from the registry)}

Charter law: P2 everywhere — every block degrades to an honest empty/'unknown' state, never raises,
never fabricates a grade (attribution stays 'building' at n<min_n). All nudge codes and details are
public-surface safe by construction: codes match ^[a-z0-9_]{1,60}$, details are template strings
with counts only — no tickers, no prose from any ledger, no env names.

Persistence (all under data/nw_reflection/ — auto-rsynced to the public mirror, so the same
counts-only discipline binds):
  * latest.json      — the full current report.
  * history.jsonl    — one line per asof (latest-wins: a same-asof re-run replaces its row, so a
                       morning run's worse numbers never freeze for the day), for trend reads.
  * nudge_state.json — the nudge lifecycle registry (nw_nudge_state.v1): per-code
                       first_seen/last_seen/builds_seen/status/resolved_on. Counters survive a
                       skipped build; resolution is a dated tombstone judged on the PRE-CAP
                       candidate set, and only when the code's detector actually ran (W-AI.1).
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_OUT_DIR = _ROOT / "data" / "nw_reflection"
_LATEST = _OUT_DIR / "latest.json"
_HISTORY = _OUT_DIR / "history.jsonl"
_NUDGE_STATE = _OUT_DIR / "nudge_state.json"

SCHEMA = "nw_reflection.v1"
_NUDGE_STATE_SCHEMA = "nw_nudge_state.v1"

# rolling window over the nw_context_audit sidecar
_AUDIT_WINDOW_RUNS = 30
# attribution refuses to score below this many joinable graded outcomes (cold-start honesty)
_ATTRIBUTION_MIN_N = 12
# hard cap on emitted nudges (mirrors the public artifact bound)
_NUDGES_MAX = 10
# coverage-nudge hysteresis: fires below the floor, and once open clears only at the ceiling —
# the live rate flapped at exactly 0.5, and a knife-edge nudge is noise, not a signal
_COVERAGE_NUDGE_FLOOR = 0.5
_COVERAGE_NUDGE_CLEAR = 0.55

_CODE_RE = re.compile(r"^[a-z0-9_]{1,60}$")

# The decision-policy fields brain/neural_web_context.decision_signals() consumes. Drift in any of
# these means the typed ladder is (partly) dead against the live artifact — the exact failure class
# found in the 2026-07-13 recon: zero graph_conflicts rows, no BOTTOMING/CONFIRMED states.
_POLICY_STATES = ("WATCH", "BOTTOMING", "CONFIRMED")  # keys of NW_CANDIDACY_SCORES


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_jsonl(p: Path, limit: int | None = None) -> list[dict]:
    """Tail-read a JSONL file; [] on any failure. limit = keep last N rows."""
    try:
        if not p.exists():
            return []
        rows: list[dict] = []
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(row, dict):
                rows.append(row)
        return rows[-limit:] if limit else rows
    except Exception:  # noqa: BLE001
        return []


def _safe_code(raw: str) -> str:
    s = str(raw).lower().strip().replace("-", "_").replace(".", "_")[:60]
    return s if _CODE_RE.match(s) else "other"


# ─────────────────────────────────────────────────────────────────────────────
# contract drift — decision-policy fields vs the live artifact
# ─────────────────────────────────────────────────────────────────────────────

def contract_drift() -> list[dict]:
    """Inspect the live NW context for the fields the typed decision ladder needs.

    Returns a list of drift rows; [] when the context is absent/stale (absence is a
    context_quality problem, not a drift claim — never report drift on missing data).
    """
    try:
        from brain import neural_web_context as nwc
        c = nwc.context()
        if not c:
            return []
        cc = c.get("candidate_context")
        rows = list(cc.values()) if isinstance(cc, dict) else []
        n = len(rows)
        out: list[dict] = []
        if n == 0:
            return [{
                "code": "candidate_context_empty", "field": "candidate_context",
                "status": "dead", "severity": "high",
                "detail": "candidate_context present but carries 0 rows",
            }]

        # graph_conflicts — the shrink leg + clean_in_conflicted leg both key off it
        gc_n = sum(1 for r in rows if isinstance(r, dict) and isinstance(r.get("graph_conflicts"), list))
        if gc_n == 0:
            out.append({
                "code": "graph_conflicts_absent", "field": "candidate_context.graph_conflicts",
                "status": "dead", "severity": "high",
                "detail": f"0/{n} candidate rows carry graph_conflicts — the entry-shrink and "
                          f"clean-in-conflicted legs of the decision ladder can never fire",
            })
        elif gc_n < max(1, n // 10):
            out.append({
                "code": "graph_conflicts_sparse", "field": "candidate_context.graph_conflicts",
                "status": "partial", "severity": "medium",
                "detail": f"{gc_n}/{n} candidate rows carry graph_conflicts",
            })

        # kernel.fdr_cleared — THE per-name gate: decision_signals() is inert for any name
        # without fdr_cleared=True, so 0/n cleared deadens the ENTIRE typed ladder regardless
        # of every other field (review finding: the live artifact is 0/~230 today).
        fdr_n = sum(1 for r in rows
                    if isinstance(r, dict) and isinstance(r.get("kernel"), dict)
                    and r["kernel"].get("fdr_cleared") is True)
        if fdr_n == 0:
            out.append({
                "code": "fdr_cleared_absent", "field": "candidate_context.kernel.fdr_cleared",
                "status": "dead", "severity": "high",
                "detail": f"0/{n} candidate rows are fdr_cleared — every leg of the typed "
                          f"decision ladder is per-name inert regardless of other fields",
            })

        # bottom_state vocabulary vs the candidacy priors
        states: dict[str, int] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            b = r.get("bottom")
            if isinstance(b, dict):
                s = b.get("bottom_state") or b.get("state")
                if isinstance(s, str):
                    states[s] = states.get(s, 0) + 1
        matched = sum(states.get(s, 0) for s in _POLICY_STATES)
        missing = [s for s in _POLICY_STATES if states.get(s, 0) == 0]
        if states and missing:
            sev = "high" if matched == states.get("WATCH", 0) else "medium"
            out.append({
                "code": "bottom_state_vocabulary_drift", "field": "candidate_context.bottom.bottom_state",
                "status": "partial", "severity": sev,
                "detail": f"policy states never observed: {'/'.join(missing)} "
                          f"(observed vocabulary has {len(states)} values over {n} rows) — "
                          f"candidacy priors above WATCH can never fire",
            })

        # market plane inputs the clean-in-conflicted leg + strategist payload use
        plane = nwc.market_plane() or {}
        if plane.get("stale"):
            pass  # staleness is context_quality's job
        else:
            if plane.get("contradiction_count") in (None, 0) and not (plane.get("contradiction_summary") or {}):
                out.append({
                    "code": "contradictions_empty", "field": "lobes.contradictions",
                    "status": "unknown", "severity": "low",
                    "detail": "market-plane contradiction records empty — clean-in-conflicted tell inert",
                })
            liq = plane.get("liquidity") or {}
            if isinstance(liq, dict) and all(v is None for v in liq.values()):
                out.append({
                    "code": "liquidity_plumbing_absent", "field": "lobes.market.liquidity_plumbing",
                    "status": "dead", "severity": "medium",
                    "detail": "liquidity plumbing block absent from the market lobe",
                })
        return out
    except Exception:  # noqa: BLE001
        return []


# ─────────────────────────────────────────────────────────────────────────────
# coverage — the book's names vs the context's candidate rows
# ─────────────────────────────────────────────────────────────────────────────

def coverage() -> dict:
    """Counts-only join: which of our decided subjects have an NW candidate row."""
    empty = {"state": "absent", "open_theses_n": 0, "resolved_recent_n": 0,
             "with_context_row_n": 0, "coverage_rate": None, "context_rows_n": 0}
    try:
        from brain import neural_web_context as nwc
        c = nwc.context()
        cc = c.get("candidate_context") if isinstance(c, dict) else None
        context_keys = {k.upper() for k in cc} if isinstance(cc, dict) else set()

        subjects: set[str] = set()
        open_n = 0
        try:
            from brain import ledger
            for t in ledger.all_theses():
                if t.get("status", "open") == "open":
                    subj = str(t.get("subject", "")).upper()
                    if subj:
                        subjects.add(subj)
                        open_n += 1
        except Exception:  # noqa: BLE001
            pass

        resolved_recent = 0
        for row in _read_jsonl(_ROOT / "data" / "brain" / "outcome_ledger.jsonl", limit=200):
            subj = str(row.get("subject", "")).upper()
            if subj:
                subjects.add(subj)
                resolved_recent += 1

        if not context_keys and not subjects:
            return empty
        with_row = sum(1 for s in subjects if s in context_keys)
        rate = round(with_row / len(subjects), 3) if subjects else None
        return {
            "state": "ok" if context_keys else "context_absent",
            "open_theses_n": open_n,
            "resolved_recent_n": resolved_recent,
            "with_context_row_n": with_row,
            "coverage_rate": rate,
            "context_rows_n": len(context_keys),
        }
    except Exception:  # noqa: BLE001
        return empty


# ─────────────────────────────────────────────────────────────────────────────
# attribution — NW signal usefulness vs graded outcomes (cold-start honest)
# ─────────────────────────────────────────────────────────────────────────────

def attribution(min_n: int = _ATTRIBUTION_MIN_N) -> dict:
    """NW-usage attribution. Refuses to score below min_n joinable rows (P3 — no fabricated edge).

    A row is *joinable* when a decision-time NW stamp exists for it. Today no such stamp is
    written into the outcome ledger, so joinable_n is 0 and the state is honestly 'building';
    the follow-up (design §6) stamps NW candidacy into decision provenance once the first
    cohort matures.
    """
    try:
        rows = _read_jsonl(_ROOT / "data" / "brain" / "outcome_ledger.jsonl")
        n_resolved = len(rows)
        joinable = [r for r in rows if isinstance(r.get("nw_at_entry"), dict)]
        if len(joinable) < min_n:
            return {"state": "building", "n_resolved": n_resolved, "joinable_n": len(joinable),
                    "note": f"needs >={min_n} graded outcomes with a decision-time NW stamp"}
        by_covered: dict[str, dict[str, int]] = {"covered": {"n": 0, "hits": 0},
                                                 "uncovered": {"n": 0, "hits": 0}}
        for r in joinable:
            bucket = "covered" if (r.get("nw_at_entry") or {}).get("had_row") else "uncovered"
            by_covered[bucket]["n"] += 1
            by_covered[bucket]["hits"] += 1 if r.get("outcome") == 1 else 0
        return {"state": "scoring", "n_resolved": n_resolved, "joinable_n": len(joinable),
                "by_covered": by_covered}
    except Exception:  # noqa: BLE001
        return {"state": "building", "n_resolved": 0, "joinable_n": 0,
                "note": "attribution read failed — degraded to building"}


# ─────────────────────────────────────────────────────────────────────────────
# context quality — the audit sidecar trend
# ─────────────────────────────────────────────────────────────────────────────

def context_quality(window: int = _AUDIT_WINDOW_RUNS) -> dict:
    """Rolling present/stale/absent read over data/brain/nw_context_audit.jsonl."""
    try:
        rows = _read_jsonl(_ROOT / "data" / "brain" / "nw_context_audit.jsonl", limit=window)
        if not rows:
            return {"state": "accruing", "window_runs": 0}
        counts = {"present": 0, "stale": 0, "absent": 0}
        for r in rows:
            s = str(r.get("status", "absent"))
            counts[s if s in counts else "absent"] += 1
        streak_status = str(rows[-1].get("status", "absent"))
        streak = 0
        for r in reversed(rows):
            if str(r.get("status", "absent")) == streak_status:
                streak += 1
            else:
                break
        latest = rows[-1]
        n = len(rows)
        return {
            "state": "ok",
            "window_runs": n,
            "n_present": counts["present"],
            "n_stale": counts["stale"],
            "n_absent": counts["absent"],
            "seen_rate": round(counts["present"] / n, 3),
            "current_streak": {"status": streak_status, "runs": streak},
            "gap_notes_latest": latest.get("gap_notes_count"),
            "asof_lag_days_latest": latest.get("age_days"),
        }
    except Exception:  # noqa: BLE001
        return {"state": "accruing", "window_runs": 0}


# ─────────────────────────────────────────────────────────────────────────────
# nudges — the structured asks to the orchestrator
# ─────────────────────────────────────────────────────────────────────────────

# every non-drift nudge code is fixed vocabulary; anything else was minted by contract_drift()
_KIND_BY_CODE = {"coverage_below_half": "coverage_gap", "context_stale_streak": "staleness",
                 "context_absent_streak": "staleness", "gap_notes_elevated": "staleness"}


def _code_kind(code: str) -> str:
    return _KIND_BY_CODE.get(code, "contract_drift")


def _prior_nudges() -> dict[str, dict]:
    """{code: nudge} from the last persisted report — one-time legacy counter seed for codes
    that predate nudge_state.json (the registry owns the counters from then on)."""
    try:
        if _LATEST.exists():
            prev = json.loads(_LATEST.read_text())
            return {n.get("code"): n for n in (prev.get("nudges") or []) if isinstance(n, dict)}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _read_nudge_state() -> dict:
    try:
        if _NUDGE_STATE.exists():
            v = json.loads(_NUDGE_STATE.read_text())
            if isinstance(v, dict) and isinstance(v.get("codes"), dict):
                return v
    except Exception:  # noqa: BLE001
        pass
    return {"schema": _NUDGE_STATE_SCHEMA, "codes": {}}


def _write_nudge_state(state: dict) -> None:
    try:
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        _NUDGE_STATE.write_text(json.dumps(state, indent=2, default=str))
    except Exception:  # noqa: BLE001
        pass


def _update_nudge_registry(candidates: list[dict], asof: str, ran_kinds: set[str]) -> dict:
    """Advance nudge_state.json from the PRE-CAP candidate set; returns the codes map.

    Lifecycle law: a candidate code is open (builds_seen counted once per asof — a same-asof
    re-run never double-counts); an open code absent from the candidates resolves with a dated
    tombstone ONLY when its detector actually ran this build (ran_kinds) — an absent context
    can never claim a drift code was fixed. A resolved code that reappears reopens keeping its
    original first_seen. Writes are P2 fail-soft."""
    state = _read_nudge_state()
    codes: dict = state.setdefault("codes", {})
    candidate_codes = {c["code"] for c in candidates}
    legacy: dict | None = None
    for code in candidate_codes:
        ent = codes.get(code)
        if not isinstance(ent, dict):
            if legacy is None:
                legacy = _prior_nudges()
            p = legacy.get(code) or {}
            codes[code] = {"first_seen": p.get("first_seen") or asof, "last_seen": asof,
                           "builds_seen": int(p.get("builds_seen") or 0) + 1,
                           "status": "open", "resolved_on": None}
            continue
        if ent.get("last_seen") != asof:
            ent["builds_seen"] = int(ent.get("builds_seen") or 0) + 1
        ent["last_seen"] = asof
        ent["status"] = "open"
        ent["resolved_on"] = None
    for code, ent in codes.items():
        if code in candidate_codes or not isinstance(ent, dict) or ent.get("status") != "open":
            continue
        if _code_kind(code) not in ran_kinds:
            continue  # detector never ran — "vanished" is not "fixed"
        ent["status"] = "resolved"
        ent["resolved_on"] = asof
    _write_nudge_state(state)
    return codes


def _nudge_candidates(drift: list[dict], cov: dict, quality: dict) -> list[dict]:
    """The FULL pre-cap candidate list {code, kind, severity, detail} — the registry update and
    the resolution judgement both run against this set, never the capped emission."""
    out: list[dict] = []

    def _emit(code: str, kind: str, severity: str, detail: str) -> None:
        out.append({"code": _safe_code(code), "kind": kind, "severity": severity,
                    "detail": detail})

    for d in drift:
        if d.get("status") in ("dead", "partial"):
            _emit(d.get("code", "drift"), "contract_drift", d.get("severity", "medium"),
                  d.get("detail", ""))

    # absence-is-not-drift (the contract_drift rule applies here too): a coverage claim is
    # only honest when the context was actually PRESENT — a stale/absent artifact is a
    # staleness story, not a coverage ask (review finding, 2026-07-13).
    rate = cov.get("coverage_rate")
    if (cov.get("state") == "ok" and isinstance(rate, (int, float))
            and cov.get("open_theses_n", 0) + cov.get("resolved_recent_n", 0) >= 5):
        # hysteresis: an already-open nudge keeps firing until the rate CLEARS the band —
        # the live rate sat at exactly 0.5 and flapped the nudge on alternating builds
        reg = _read_nudge_state().get("codes", {}).get("coverage_below_half") or {}
        threshold = (_COVERAGE_NUDGE_CLEAR if isinstance(reg, dict)
                     and reg.get("status") == "open" else _COVERAGE_NUDGE_FLOOR)
        if rate < threshold:
            _emit("coverage_below_half", "coverage_gap", "medium",
                  f"only {cov.get('with_context_row_n')} of our decided subjects have a "
                  f"candidate_context row (rate {rate}; fires <{_COVERAGE_NUDGE_FLOOR}, "
                  f"clears at >={_COVERAGE_NUDGE_CLEAR})")

    if quality.get("state") == "ok":
        streak = quality.get("current_streak") or {}
        if streak.get("status") in ("stale", "absent") and int(streak.get("runs") or 0) >= 3:
            _emit(f"context_{streak['status']}_streak", "staleness", "high",
                  f"context {streak['status']} for {streak['runs']} consecutive runs")
        gaps = quality.get("gap_notes_latest")
        if isinstance(gaps, int) and gaps >= 3:
            _emit("gap_notes_elevated", "staleness", "low",
                  f"latest build carries {gaps} producer gap notes")
    return out


def _derive_nudges_state(drift: list[dict], cov: dict, quality: dict, asof: str,
                         max_n: int, drift_ran: bool | None) -> tuple[list[dict], int, dict]:
    """(emitted, candidates_n, registry_codes) — the full fold behind derive_nudges; build()
    uses the extras for the honest dropped/resolved accounting."""
    candidates = _nudge_candidates(drift, cov, quality)
    if drift_ran is None:
        # contract_drift() returns [] for BOTH a healthy and an absent context — without the
        # caller's word only a non-empty drift list proves the detector ran (conservative:
        # never resolve on ambiguity).
        drift_ran = bool(drift)
    ran_kinds: set[str] = set()
    if drift_ran:
        ran_kinds.add("contract_drift")
    if cov.get("state") == "ok":
        ran_kinds.add("coverage_gap")
    if quality.get("state") == "ok":
        ran_kinds.add("staleness")
    codes = _update_nudge_registry(candidates, asof, ran_kinds)
    out: list[dict] = []
    for c in candidates:
        ent = codes.get(c["code"]) or {}
        out.append({**c, "first_seen": ent.get("first_seen") or asof,
                    "builds_seen": int(ent.get("builds_seen") or 1)})
    sev_rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda x: (sev_rank.get(x.get("severity"), 3), x.get("code", "")))
    return out[:max_n], len(candidates), codes


def derive_nudges(drift: list[dict], cov: dict, quality: dict, asof: str,
                  max_n: int = _NUDGES_MAX, *, drift_ran: bool | None = None) -> list[dict]:
    """Fold the report blocks into ≤max_n coded nudges, counters from the lifecycle registry."""
    return _derive_nudges_state(drift, cov, quality, asof, max_n, drift_ran)[0]


# ─────────────────────────────────────────────────────────────────────────────
# report + persistence
# ─────────────────────────────────────────────────────────────────────────────

def build(asof: date | str | None = None, *, nudges_max: int | None = None,
          attribution_min_n: int | None = None) -> dict:
    """Assemble the full nw_reflection.v1 report + advance the nudge lifecycle registry.
    Never raises.

    Optional overrides come from brain.mastermind_ai settings (one-way import direction:
    mastermind_ai → nw_reflection, never the reverse)."""
    asof_s = (asof.isoformat() if isinstance(asof, date) else str(asof)) if asof else date.today().isoformat()
    try:
        # The NW context reader caches for the PROCESS lifetime and nothing else in a dark-flag
        # deployment ever re-primes it — without this reset a long-lived uvicorn process would
        # reflect the artifact as it stood at first read for days (review finding, 2026-07-13).
        # macro_refresh heals the file on disk every 3h; a fresh read is strictly more honest.
        try:
            from brain.neural_web_context import _reset_context_cache
            _reset_context_cache()
        except Exception:  # noqa: BLE001
            pass
        drift = contract_drift()
        cov = coverage()
        qual = context_quality()
        attr = attribution(min_n=attribution_min_n or _ATTRIBUTION_MIN_N)
        # contract_drift() returns [] for both a healthy and an absent artifact — resolution
        # honesty needs to know whether the detector actually ran (context present).
        try:
            from brain import neural_web_context as nwc
            drift_ran = bool(drift) or bool(nwc.context())
        except Exception:  # noqa: BLE001
            drift_ran = bool(drift)
        nudges, candidates_n, reg_codes = _derive_nudges_state(
            drift, cov, qual, asof_s, nudges_max or _NUDGES_MAX, drift_ran)
        resolved = sorted(
            ({"code": k, "resolved_on": v.get("resolved_on")}
             for k, v in reg_codes.items()
             if isinstance(v, dict) and v.get("status") == "resolved" and v.get("resolved_on")),
            key=lambda r: (str(r["resolved_on"]), str(r["code"])), reverse=True)
        return {
            "schema": SCHEMA,
            "asof": asof_s,
            "generated_at": _now_iso(),
            "contract_drift": drift,
            "coverage": cov,
            "attribution": attr,
            "context_quality": qual,
            "nudges": nudges,
            "nudges_dropped_n": max(0, candidates_n - len(nudges)),
            "nudges_resolved_recent": resolved[:5],
        }
    except Exception:  # noqa: BLE001
        return {"schema": SCHEMA, "asof": asof_s, "generated_at": _now_iso(),
                "contract_drift": [], "coverage": {"state": "absent"},
                "attribution": {"state": "building", "n_resolved": 0, "joinable_n": 0},
                "context_quality": {"state": "accruing", "window_runs": 0}, "nudges": [],
                "nudges_dropped_n": 0, "nudges_resolved_recent": []}


def persist(asof: date | str | None = None, *, nudges_max: int | None = None,
            attribution_min_n: int | None = None) -> dict:
    """build() + write latest.json + upsert history.jsonl (latest-wins per asof). Never raises."""
    rep = build(asof, nudges_max=nudges_max, attribution_min_n=attribution_min_n)
    try:
        _OUT_DIR.mkdir(parents=True, exist_ok=True)
        _LATEST.write_text(json.dumps(rep, indent=2, default=str))
        compact = {
            "asof": rep["asof"], "generated_at": rep["generated_at"],
            "drift_n": len(rep.get("contract_drift") or []),
            "nudges_n": len(rep.get("nudges") or []),
            "coverage_rate": (rep.get("coverage") or {}).get("coverage_rate"),
            "seen_rate": (rep.get("context_quality") or {}).get("seen_rate"),
            "attribution_state": (rep.get("attribution") or {}).get("state"),
        }
        rows = _read_jsonl(_HISTORY)
        if any(r.get("asof") == rep.get("asof") for r in rows):
            # latest-wins per asof: the file is ~1 line/day so a full rewrite is fine — and it
            # fixes the day-one divergence where a morning run's worse numbers froze for the day
            replaced = False
            kept: list[dict] = []
            for r in rows:
                if r.get("asof") == rep.get("asof"):
                    if not replaced:
                        kept.append(compact)
                        replaced = True
                    continue
                kept.append(r)
            _HISTORY.write_text("".join(json.dumps(r, default=str) + "\n" for r in kept))
        else:
            with _HISTORY.open("a") as fh:
                fh.write(json.dumps(compact, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass
    return rep


def latest() -> dict:
    """The last persisted report; {} when absent."""
    try:
        if _LATEST.exists():
            v = json.loads(_LATEST.read_text())
            return v if isinstance(v, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}
