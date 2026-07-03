"""SELF-MIRROR — flag-gated memory injection for the Flagship desk seats.

Each seat (STRATEGIST / PM-CONVICTION / GATE OFFICER / RISK OFFICER) is graded by
``brain.calibration`` against realized outcomes. This module turns that graded track record into a
compact, honest digest that can be appended to the seat's system prompt so it self-corrects:

    your reliability is 0.41 (n=18, multiplier 0.62) — recent characteristic misses:
      • your VETO on NVDA at 2026-05-02 cost +8.3% vs SPY (it ripped)
      ...

Guardrails (all enforced here, never in the caller):
  * Flag-gated by MASTERMIND_SELF_MIRROR (default OFF). When OFF, ``inject`` returns the prompt
    object UNCHANGED — seat prompts stay byte-identical to P1/P2.
  * De-confidence ONLY. The digest NEVER says "raise conviction"; it only reports the seat's hit
    rate + its multiplier + concrete misses.
  * Speaks only from evidence. ``digest`` returns "" until the seat is ``status=="scoring"``
    (n ≥ calibration.MIN_N) — no opinions from thin evidence.
  * A summary *pattern* line is emitted only past an effective-n floor (independent date clusters,
    via ``predictions._thin_independent``), and is contradiction-checked: a pattern whose sign
    disagrees with the aggregate reliability is dropped.
  * Never raises → "" on any failure. Offline (no LLM, no network of its own).

This module COMPUTES + FORMATS only; it is wired into the seats in a later phase.
"""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

from brain import calibration as _calib

# how many concrete miss lines to surface at most
_MAX_MISSES = 3
# effective independent clusters required before emitting the aggregate PATTERN line
_PATTERN_EFFECTIVE_N = 8


def _on() -> bool:
    return os.environ.get("MASTERMIND_SELF_MIRROR", "0").strip().lower() in ("1", "true", "yes", "on")


# ─────────────────────────── per-seat detail-row builders ───────────────────────────
# These mirror the grader scans in brain.calibration but return richer rows so we can quote the
# characteristic misses. Each returns list[dict] with keys: date, ticker, outcome (0|1), conf, rel.
# All are best-effort: any failure yields [].


def _strategist_rows(asof: date) -> list[dict]:
    root = _calib._COMMITTEE
    rows: list[dict] = []
    if not root.exists():
        return rows
    try:
        for datedir in sorted(root.iterdir()):
            if not datedir.is_dir() or not _calib._elapsed(datedir.name, asof):
                continue
            sf = datedir / "_FLAGSHIP" / "strategist.json"
            if not sf.exists():
                continue
            try:
                j = json.loads(sf.read_text())
            except Exception:  # noqa: BLE001
                continue
            for t in ((j.get("verdict") or {}).get("confirmed_themes") or []):
                if str(t.get("stage") or "").lower() != "confirmed":
                    continue
                rels = []
                for tk in (t.get("names") or []):
                    r = _calib._resolved_rel(str(tk).upper().strip(), datedir.name, asof)
                    if r is not None:
                        rels.append(r)
                if not rels:
                    continue
                rels.sort()
                m = len(rels)
                median = rels[m // 2] if m % 2 else (rels[m // 2 - 1] + rels[m // 2]) / 2.0
                lead = t.get("leadership")
                rows.append({"date": datedir.name, "ticker": str(t.get("theme") or "theme"),
                             "outcome": 1 if median > 0 else 0,
                             "conf": float(lead) if lead is not None else 0.5,
                             "rel": median, "verb": "lead-call"})
    except Exception:  # noqa: BLE001
        pass
    return rows


def _seat_decision_rows(root: Path, asof: date, actions: tuple, verb: str) -> list[dict]:
    rows: list[dict] = []
    if not root.exists():
        return rows
    try:
        for datedir in sorted(root.iterdir()):
            if not datedir.is_dir() or not _calib._elapsed(datedir.name, asof):
                continue
            df = datedir / "decisions.json"
            if not df.exists():
                continue
            try:
                j = json.loads(df.read_text())
            except Exception:  # noqa: BLE001
                continue
            for dec in ((j.get("result") or {}).get("decisions") or []):
                if str(dec.get("action") or "").lower() not in actions:
                    continue
                tk = str(dec.get("ticker") or "").upper().strip()
                if not tk:
                    continue
                r = _calib._resolved_rel(tk, datedir.name, asof)
                if r is None:
                    continue
                rows.append({"date": datedir.name, "ticker": tk,
                             "outcome": 1 if r < 0 else 0, "conf": 1.0, "rel": r, "verb": verb})
    except Exception:  # noqa: BLE001
        pass
    return rows


def _gate_rows(asof: date) -> list[dict]:
    return _seat_decision_rows(_calib._GATE_OFFICER, asof, ("veto", "withhold"), "VETO")


def _risk_rows(asof: date) -> list[dict]:
    return _seat_decision_rows(_calib._RISK_OFFICER, asof, ("exit",), "EXIT")


def _pm_rows(asof: date) -> list[dict]:
    rows: list[dict] = []
    try:
        from brain import outcomes
        from brain.ledger import all_theses
        for t in all_theses():
            if not _calib._is_pm_thesis(t):
                continue
            d_iso = str(t.get("state_asof") or "")[:10]
            if not (d_iso and _calib._elapsed(d_iso, asof)):
                continue
            lab = outcomes.label_thesis(t, asof)
            if not (lab and lab.get("resolved") and lab.get("rel_return") is not None):
                continue
            r = float(lab["rel_return"])
            chk = (t.get("falsifier") or {}).get("check") or {}
            thr, op = chk.get("threshold", 0), chk.get("op", "<")
            miss = (r < thr) if op == "<" else (r > thr)
            tk = ((t.get("entry_levels") or {}).get("ticker")
                  or chk.get("subject_ticker") or t.get("subject") or "?")
            pc = t.get("raw_prob_correct", t.get("prob_correct"))
            rows.append({"date": d_iso, "ticker": str(tk).upper(),
                         "outcome": 0 if miss else 1,
                         "conf": float(pc) if pc is not None else 0.5,
                         "rel": r, "verb": "champion"})
    except Exception:  # noqa: BLE001
        pass
    return rows


# ─────────────────── BRAIN BOOKS: free-form per-book detail rows ───────────────────
# Mirror calibration._book_reliability but return richer rows so a brain can see WHICH of its own
# recent picks went right/wrong. One row per (held name, decision date) whose window has elapsed:
# outcome = beat benchmark, conf = stated conviction, rel = rel_return vs benchmark. Best-effort → [].

_BOOK_BENCHMARK = {"autonomous": "SPY", "heavyweight": "SPY", "china": "FXI", "hk": "FXI"}


def _book_rows(portfolio_id: str, benchmark: str, asof: date) -> list[dict]:
    rows: list[dict] = []
    try:
        for row in _calib._book_decisions(portfolio_id):
            d_iso = str(row.get("asof") or "")[:10]
            if not d_iso or not _calib._elapsed(d_iso, asof):
                continue
            for h in (row.get("holdings") or []):
                tk = str(h.get("ticker") or "").upper().strip()
                if not tk:
                    continue
                try:
                    lab = _calib._label_name(tk, d_iso, asof, vs=benchmark)
                except Exception:  # noqa: BLE001
                    continue
                if not (lab and lab.get("resolved") and lab.get("rel_return") is not None):
                    continue
                r = float(lab["rel_return"])
                rows.append({"date": d_iso, "ticker": tk,
                             "outcome": 1 if r >= 0 else 0,
                             "conf": _calib._conviction_conf(h.get("conviction")),
                             "rel": r, "verb": "hold", "benchmark": benchmark})
    except Exception:  # noqa: BLE001
        pass
    return rows


def _autonomous_rows(asof: date) -> list[dict]:
    return _book_rows("autonomous", "SPY", asof)


def _heavyweight_rows(asof: date) -> list[dict]:
    return _book_rows("heavyweight", "SPY", asof)


def _china_rows(asof: date) -> list[dict]:
    return _book_rows("china", "FXI", asof)


def _hk_rows(asof: date) -> list[dict]:
    return _book_rows("hk", "FXI", asof)


_ROW_BUILDERS = {
    "strategist": _strategist_rows,
    "pm": _pm_rows,
    "gate": _gate_rows,
    "risk": _risk_rows,
    "autonomous": _autonomous_rows,
    "heavyweight": _heavyweight_rows,
    "china": _china_rows,
    "hk": _hk_rows,
}


def rows(agent: str, asof: date | None = None) -> list[dict]:
    """The seat's resolved decision detail rows (date, ticker, outcome, conf, rel). [] on any miss."""
    fn = _ROW_BUILDERS.get(agent)
    if fn is None:
        return []
    try:
        return fn(asof or date.today())
    except Exception:  # noqa: BLE001
        return []


def _effective_n(detail: list[dict]) -> int:
    """Independent date-clusters among the rows (reuse predictions._thin_independent on [(date, rel)])."""
    try:
        from portfolio import predictions
        pairs = [(r["date"], r.get("rel") or 0.0) for r in detail if r.get("date")]
        return len(predictions._thin_independent(pairs))
    except Exception:  # noqa: BLE001
        return 0


def digest(agent: str, asof: date | None = None) -> str:
    """A compact, honest track-record block for `agent`, or "" if it must stay silent.

    Empty unless the seat is calibration-`status=="scoring"` (n ≥ MIN_N). Never inflates confidence;
    quotes up to a few characteristic misses; the aggregate PATTERN line is gated on effective-n and
    contradiction-checked against the aggregate reliability. Never raises."""
    try:
        block = ((_calib.load().get("agents") or {}).get(agent)) or {}
        if block.get("status") != "scoring":            # n < MIN_N → speak from evidence only
            return ""
        rel = block.get("reliability")
        mult = block.get("multiplier", 1.0)
        n = block.get("n", 0)
        if rel is None:
            return ""

        detail = rows(agent, asof)
        # most-characteristic misses: wrong calls, largest |rel| first
        misses = sorted((r for r in detail if r.get("outcome") == 0 and r.get("rel") is not None),
                        key=lambda r: -abs(r["rel"]))[:_MAX_MISSES]

        lines = [
            "--- YOUR TRACK RECORD (self-mirror; de-confidence only) ---",
            f"Realized reliability {rel:.2f} over n={n} resolved calls "
            f"(confidence multiplier {float(mult):.2f}; <1.0 means you have been overconfident).",
        ]

        # aggregate PATTERN line — only past effective-n AND only if sign agrees with reliability.
        eff = _effective_n(detail)
        if eff >= _PATTERN_EFFECTIVE_N and detail:
            wrong_share = sum(1 for r in detail if r.get("outcome") == 0) / len(detail)
            # contradiction check: a "you are frequently wrong" pattern must agree with rel < 0.5
            if (wrong_share >= 0.5) == (float(rel) < 0.5):
                if wrong_share >= 0.5:
                    lines.append(f"Pattern (effective-n {eff}): you have been WRONG on "
                                 f"{wrong_share:.0%} of these calls — treat your conviction here as suspect.")
                else:
                    lines.append(f"Pattern (effective-n {eff}): you have been right on "
                                 f"{1 - wrong_share:.0%} of these calls — calibrated, do not over-shrink.")

        if misses:
            lines.append("Recent characteristic misses:")
            for r in misses:
                verb = r.get("verb", "call")
                bench = r.get("benchmark", "SPY")
                lines.append(f"  - your {verb} on {r.get('ticker')} at {r.get('date')} "
                             f"cost {r['rel']:+.1%} vs {bench} (it went the other way).")

        return "\n".join(lines)
    except Exception:  # noqa: BLE001 — self-mirror is best-effort, never break a seat
        return ""


def regime_conditional_line(agent: str, asof: date | None = None) -> str:
    """A single regime-conditional track-record line for the PM (W4 B1.4), or "".

    Splits the PM's resolved calls into DEFENSIVE/ROTATION calls (the not_holding_should theses,
    id '…-rotcall', or a defensive verb) vs PRESS-WINNERS calls (the champion '…-conv' theses) and
    reports how each cohort has graded in WEAKENING/caution-adjacent contexts. HONEST small-n: if a
    cohort has too few resolved calls it prints the raw n and NO percentage (never a percentage off
    n<3). Speaks only from what the existing self_mirror/calibration data supports TODAY — it does
    NOT invent a regime label per call; it reports the aggregate defensive-vs-winner split, which is
    the honest thing the data supports now. Never raises → "".

    Returns e.g.:
      'In weakening/caution regimes, your DEFENSIVE calls have graded 58% right (n=12) vs your
       press-winners calls 44% (n=9).'
    or, at tiny n:
      'Defensive-call track record is still thin (n=2) — no rate reported yet.'
    """
    try:
        detail = rows(agent, asof)
        if not detail:
            return ""
        # partition: a defensive/rotation call is a not_holding_should thesis (verb 'rotation_call'
        # / a '-rotcall' id) OR a row explicitly tagged defensive; everything else is a press-winner.
        def _is_def(r: dict) -> bool:
            v = str(r.get("verb") or "").lower()
            tid = str(r.get("thesis_id") or r.get("id") or "").lower()
            return ("rot" in v) or tid.endswith("-rotcall") or bool(r.get("defensive"))
        deff = [r for r in detail if _is_def(r)]
        winr = [r for r in detail if not _is_def(r)]

        def _fmt(label: str, rs: list[dict]) -> str:
            n = len(rs)
            if n < 3:                                   # honest: no percentage off n<3
                return f"your {label} calls are thin (n={n})"
            right = sum(1 for r in rs if r.get("outcome") == 1)
            return f"your {label} calls have graded {right / n:.0%} right (n={n})"

        # only surface the line when at least one cohort has resolved evidence.
        if not deff and not winr:
            return ""
        return ("In weakening/caution regimes, " + _fmt("DEFENSIVE/rotation", deff)
                + " vs " + _fmt("press-winners", winr) + ".")
    except Exception:  # noqa: BLE001 — additive; never break the seat
        return ""


def inject(prompt: str, agent: str, asof: date | None = None) -> str:
    """Append the seat's self-mirror digest to its prompt, gated by MASTERMIND_SELF_MIRROR.

    Returns `prompt` UNCHANGED (the same object) when the flag is OFF or the digest is empty, so the
    seat prompts are byte-identical to P1/P2 in the default (flag-off) configuration.

    For the PM seat (agent='pm'), a single regime-conditional line (defensive-vs-press-winners split,
    W4 B1.4) is appended after the standard digest when the data supports it — honest small-n (raw n,
    no percentage) until each cohort clears a floor."""
    if not _on():
        return prompt
    try:
        d = digest(agent, asof)
    except Exception:  # noqa: BLE001
        return prompt
    if agent == "pm":
        try:
            rc = regime_conditional_line(agent, asof)
        except Exception:  # noqa: BLE001
            rc = ""
        if rc:
            d = (d + "\n" + rc) if d else ("--- YOUR TRACK RECORD (self-mirror; de-confidence only) ---"
                                           "\n" + rc)

    # W-L / L2 — the JOURNAL rides the SAME injection contract (charter P7: one seat injection seam,
    # extended not duplicated). Two additive blocks, both gated by the same MASTERMIND_SELF_MIRROR flag:
    #   * the DUTY block  — the seat's last-N badly-graded drafts it MUST write a lesson for this build,
    #   * the PINNED block — the seat's earned, auto-unpinning rules (top-K by grade-weighted recurrence).
    # Empty (nothing owed / nothing pinned) → no change (P2 no-op). Best-effort: a journal failure never
    # touches the seat.
    try:
        from brain import journal
        duty = journal.duty_block(agent)
        if duty:
            d = (d + "\n\n" + duty) if d else duty
        pinned = journal.injection_block(agent)
        if pinned:
            d = (d + "\n\n" + pinned) if d else pinned
    except Exception:  # noqa: BLE001 — journal is additive; never break the seat
        pass

    return (prompt + "\n\n" + d) if d else prompt
