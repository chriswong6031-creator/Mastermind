"""CIO / Meta-PM — the weekly accountability review for the Flagship desk seats.

This is the top of the P3 learning loop. Once per week it reads, READ-ONLY, the artifacts the
four seats (STRATEGIST, PM-CONVICTION, GATE OFFICER, RISK OFFICER) and the accountability machinery
already produce — their per-role calibration multipliers (brain/calibration.py), each seat's graded
KPI track record, the book's NAV-vs-benchmark path (nav_history), and the shadow leaderboard — and
writes a structured + human note answering one question: *what is working, and who is miscalibrated?*

It is deliberately toothless. It NEVER trades, NEVER touches a paper account, NEVER flips a flag, and
NEVER mutates a seat's behavior. It only computes numbers deterministically and surfaces RECOMMENDATIONS
(e.g. "GATE multiplier 0.62 + significant — vetoes 38% correct, review MIN_N / consider SELF_MIRROR for
gate") which the user (or a flag) acts on. The narrative prose is written by Opus (role="deep") but the
numbers and the recommendations are computed deterministically, so review() is fully unit-testable with
no LLM and degrades to an honest stub on missing data.

    from brain import cio
    cio.write()                 # build + persist data/brain/cio/<isoweek>.{json,md}
    rep = cio.review()          # the structured dict (no files written)
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_OUT = _ROOT / "data" / "brain" / "cio"

# the four Flagship seats this review grades — (calibration_key, human_label)
_SEATS = [
    ("strategist", "MACRO STRATEGIST"),
    ("pm", "PM-CONVICTION"),
    ("gate", "GATE OFFICER"),
    ("risk", "RISK OFFICER"),
]

# books to summarize NAV-vs-benchmark for. flagship is the legacy data/portfolio/ tree.
_NAV_BOOKS = [
    ("flagship", _ROOT / "data" / "portfolio" / "nav_history.jsonl"),
    ("flagship_judgment", _ROOT / "data" / "portfolios" / "flagship_judgment" / "nav_history.jsonl"),
]

_MIN_DATES = 8     # independent (thinned) clusters before a KPI t-stat is reported (mirror predictions)
_HAC_LAGS = 2      # Newey-West residual-autocorrelation lags after thinning


# ── KPI row builders — reuse the grader artifact scans, but keep the per-date (date, rel) pairs ──
# so the CIO can run the heavier date-clustered significance test on top of the binary multiplier.

def _label_name(ticker: str, asof_iso: str, horizon: int = 21) -> dict | None:
    """Synthetic-thesis labeler — reuses outcomes.label_thesis so leakage logic is never re-implemented."""
    try:
        from brain import outcomes
        return outcomes.label_thesis({
            "id": f"{asof_iso}-{ticker}", "state_asof": asof_iso,
            "entry_levels": {"ticker": ticker},
            "falsifier": {"check": {"kind": "rel_return", "subject_ticker": ticker,
                                    "vs": "SPY", "horizon_d": horizon}}}, asof=date.today())
    except Exception:  # noqa: BLE001
        return None


def _strategist_rows(asof: date) -> list[tuple[str, float]]:
    """(date, theme_median_rel) for each CONFIRMED-leadership theme whose forward window elapsed."""
    rows: list[tuple[str, float]] = []
    base = _ROOT / "data" / "committee"
    if not base.exists():
        return rows
    try:
        for d in sorted(p.name for p in base.iterdir() if p.is_dir()):
            try:
                if date.fromisoformat(d) >= asof:
                    continue
            except Exception:  # noqa: BLE001
                continue
            f = base / d / "_FLAGSHIP" / "strategist.json"
            if not f.exists():
                continue
            try:
                verdict = (json.loads(f.read_text()) or {}).get("verdict") or {}
            except Exception:  # noqa: BLE001
                continue
            for t in (verdict.get("confirmed_themes") or []):
                if t.get("stage") != "confirmed":
                    continue
                rels = []
                for tk in (t.get("names") or []):
                    lab = _label_name(tk, d)
                    if lab and lab.get("resolved") and lab.get("rel_return") is not None:
                        rels.append(float(lab["rel_return"]))
                if rels:
                    rels.sort()
                    med = rels[len(rels) // 2] if len(rels) % 2 else (rels[len(rels) // 2 - 1] + rels[len(rels) // 2]) / 2
                    rows.append((d, med))
    except Exception:  # noqa: BLE001
        pass
    return rows


def _seat_dir_rows(asof: date, subdir: str, actions: tuple[str, ...], sign: int) -> list[tuple[str, float]]:
    """Generic (date, signed_rel) rows for gate/risk: read data/<subdir>/<d>/decisions.json, label
    each name whose action matched. `sign` flips the sign so a 'good' decision is a positive KPI value
    (gate/risk both want rel<0 → multiply rel by -1 so good outcomes read positive in the t-test)."""
    rows: list[tuple[str, float]] = []
    base = _ROOT / "data" / subdir
    if not base.exists():
        return rows
    try:
        for d in sorted(p.name for p in base.iterdir() if p.is_dir()):
            try:
                if date.fromisoformat(d) >= asof:
                    continue
            except Exception:  # noqa: BLE001
                continue
            f = base / d / "decisions.json"
            if not f.exists():
                continue
            try:
                result = (json.loads(f.read_text()) or {}).get("result") or {}
            except Exception:  # noqa: BLE001
                continue
            for dec in (result.get("decisions") or []):
                if dec.get("action") not in actions:
                    continue
                lab = _label_name(dec.get("ticker") or "", d)
                if lab and lab.get("resolved") and lab.get("rel_return") is not None:
                    rows.append((d, sign * float(lab["rel_return"])))
    except Exception:  # noqa: BLE001
        pass
    return rows


def _pm_rows(asof: date) -> list[tuple[str, float]]:
    """(date, rel_return) for resolved PM-conviction theses (thesis_id ending '-conv')."""
    rows: list[tuple[str, float]] = []
    try:
        from brain import outcomes
        from brain.ledger import all_theses
    except Exception:  # noqa: BLE001
        return rows
    try:
        for t in all_theses():
            tid = str(t.get("id") or "")
            if not (tid.endswith("-conv") or t.get("source") == "flagship_judgment"):
                continue
            d = str(t.get("state_asof") or "")[:10]
            try:
                if not d or date.fromisoformat(d) >= asof:
                    continue
            except Exception:  # noqa: BLE001
                continue
            lab = outcomes.label_thesis(t, asof)
            if lab and lab.get("resolved") and lab.get("rel_return") is not None:
                rows.append((d, float(lab["rel_return"])))
    except Exception:  # noqa: BLE001
        pass
    return rows


def _kpi_rows(seat: str, asof: date) -> list[tuple[str, float]]:
    """Per-seat (date, signed_kpi_value) where a positive value = a 'good' realized decision."""
    if seat == "strategist":
        return _strategist_rows(asof)
    if seat == "pm":
        return _pm_rows(asof)
    if seat == "gate":
        # good veto/withhold ⇔ rel<0 → flip sign so good reads positive
        return _seat_dir_rows(asof, "gate_officer", ("veto", "withhold"), sign=-1)
    if seat == "risk":
        # good exit ⇔ post-exit rel<0 → flip sign
        return _seat_dir_rows(asof, "risk_officer", ("exit",), sign=-1)
    return []


def _kpi_significance(rows: list[tuple[str, float]]) -> dict:
    """Date-cluster the per-decision KPI values to independent windows and run Newey-West.

    Returns {n, effective_n, mean, t, p, ci95, significant}. `significant` requires p<0.05 AND the
    95% CI lower bound > 0 (a directional gate: the seat's decisions are reliably *good*, not just
    non-zero). Degrades to an honest building stub below _MIN_DATES independent clusters. Never raises."""
    out = {"n": len(rows), "effective_n": 0, "mean": None, "t": None, "p": None,
           "ci95": None, "significant": False, "status": "building"}
    if not rows:
        return out
    try:
        import pandas as pd
        from portfolio.predictions import _thin_independent
        # one observation per date first (mean KPI value that day), then thin to non-overlapping windows
        from collections import defaultdict
        by_date: dict[str, list[float]] = defaultdict(list)
        for d, v in rows:
            by_date[d].append(v)
        pairs = [(d, sum(vs) / len(vs)) for d, vs in by_date.items() if vs]
        vals = _thin_independent(pairs)
        out["effective_n"] = len(vals)
        if len(vals) < _MIN_DATES:
            return out
        s = pd.Series(vals, dtype=float)
        mean = float(s.mean())
        out["mean"] = round(mean, 4)
        try:
            from engine.validation import newey_west_tstat
            nw = newey_west_tstat(s, lags=_HAC_LAGS)
            se, t, p = nw.get("se"), nw.get("t"), nw.get("p")
        except Exception:  # noqa: BLE001 — vendor/macro not present; fall back to a plain t-stat
            n = len(vals)
            sd = float(s.std(ddof=1)) if n > 1 else 0.0
            se = (sd / (n ** 0.5)) if sd else None
            t = (mean / se) if se else None
            p = None
        ci = None
        if se is not None:
            ci = [round(mean - 1.96 * se, 4), round(mean + 1.96 * se, 4)]
        out.update({"t": round(t, 2) if t is not None else None,
                    "p": round(p, 4) if p is not None else None, "ci95": ci,
                    "significant": bool((p is not None and p < 0.05 and ci and ci[0] > 0)),
                    "status": "scoring"})
    except Exception:  # noqa: BLE001
        pass
    return out


# ── book NAV vs benchmark ──────────────────────────────────────────────────────────────────────

def _nav_summary() -> list[dict]:
    """Per-book {book, n_points, nav, spy_nav, ret_pct, spy_ret_pct, excess_pct} from nav_history.jsonl."""
    out = []
    for book, path in _NAV_BOOKS:
        try:
            if not path.exists():
                continue
            rows = []
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
            if len(rows) < 2:
                continue
            first, last = rows[0], rows[-1]
            nav0, nav1 = first.get("nav"), last.get("nav")
            spy0, spy1 = first.get("spy_nav"), last.get("spy_nav")
            ret = (nav1 / nav0 - 1.0) if (nav0 and nav1) else None
            spy_ret = (spy1 / spy0 - 1.0) if (spy0 and spy1) else None
            excess = (ret - spy_ret) if (ret is not None and spy_ret is not None) else None
            out.append({"book": book, "n_points": len(rows),
                        "start": first.get("date"), "end": last.get("date"),
                        "nav": nav1, "spy_nav": spy1,
                        "ret_pct": round(ret * 100, 2) if ret is not None else None,
                        "spy_ret_pct": round(spy_ret * 100, 2) if spy_ret is not None else None,
                        "excess_pct": round(excess * 100, 2) if excess is not None else None})
        except Exception:  # noqa: BLE001
            continue
    return out


# ── reputation + recommendation logic (deterministic) ──────────────────────────────────────────

def _reputation(mult: float | None, status: str, sig: dict) -> str:
    """well_calibrated | overconfident | inert — from the multiplier vs 1.0 and KPI significance."""
    if status != "scoring" or mult is None:
        return "inert"                       # below MIN_N resolved decisions — not yet judgeable
    if mult <= 0.85:
        return "overconfident"               # calibration is materially shrinking this seat
    return "well_calibrated"


def _recommendation(seat_key: str, label: str, cal: dict, sig: dict, rep: str) -> str | None:
    """A single concrete, non-binding tuning recommendation for a seat (or None if nothing to flag)."""
    n = cal.get("n") or 0
    mult = cal.get("multiplier")
    rel = cal.get("reliability")
    if rep == "inert":
        return f"{label}: inert (n={n} < MIN_N) — accumulate more resolved decisions; no action."
    if rep == "overconfident":
        relpct = f"{rel * 100:.0f}%" if rel is not None else "n/a"
        sigtag = "significant" if sig.get("significant") else "not yet significant"
        return (f"{label}: multiplier {mult}, {sigtag} — reliability {relpct} (n={n}). "
                f"Review MIN_N / consider enabling SELF_MIRROR for {seat_key} so the seat self-corrects.")
    if rep == "well_calibrated" and sig.get("significant"):
        return f"{label}: well-calibrated and significant (mult {mult}, n={n}) — leave as is."
    return None


def review(asof: date | None = None) -> dict:
    """Pure, deterministic, READ-ONLY aggregation of the desk's accountability state. Never raises,
    never trades, never mutates a seat. The narrative `note_md` is filled by write() via Opus; here
    note_md carries a deterministic fallback so review() alone is a complete, testable artifact."""
    asof = asof or date.today()
    try:
        from brain import calibration
        agents = (calibration.load() or {}).get("agents") or {}
    except Exception:  # noqa: BLE001
        agents = {}

    per_seat = []
    for key, label in _SEATS:
        cal = dict(agents.get(key) or {})
        if not cal:
            cal = {"n": 0, "reliability": None, "mean_confidence": None,
                   "multiplier": 1.0, "status": "building"}
        try:
            sig = _kpi_significance(_kpi_rows(key, asof))
        except Exception:  # noqa: BLE001
            sig = {"n": 0, "effective_n": 0, "mean": None, "t": None, "p": None,
                   "ci95": None, "significant": False, "status": "building"}
        rep = _reputation(cal.get("multiplier"), cal.get("status") or "building", sig)
        per_seat.append({
            "seat": key, "label": label,
            "reliability": cal.get("reliability"),
            "multiplier": cal.get("multiplier"),
            "n_resolved": cal.get("n"),
            "calibration_status": cal.get("status"),
            "kpis": sig,
            "reputation": rep,
            "recommendation": _recommendation(key, label, cal, sig, rep),
        })

    try:
        book_summary = _nav_summary()
    except Exception:  # noqa: BLE001
        book_summary = []
    try:
        from portfolio import shadow_books
        leaderboard = shadow_books.load_leaderboard() or {}
    except Exception:  # noqa: BLE001
        leaderboard = {}

    # READ-ONLY firm-level cross-book exposure flag — purely observational (it never trades or
    # resizes a book). Surface the top name-level pile-ups as a single line so the weekly note also
    # answers "where are the books concentrated together?". Degrades to None on any failure.
    try:
        from portfolio import firm_exposure
        firm = firm_exposure.summary()
    except Exception:  # noqa: BLE001
        firm = None
    firm_flags = [f for f in ((firm or {}).get("flags") or []) if f.get("kind") == "name"]

    whats_working = [s["label"] for s in per_seat
                     if s["reputation"] == "well_calibrated" and (s["kpis"] or {}).get("significant")]
    whats_broken = [s["label"] for s in per_seat if s["reputation"] == "overconfident"]
    tuning = [s["recommendation"] for s in per_seat if s.get("recommendation")]

    rep = {
        "as_of": asof.isoformat(),
        "iso_week": _isoweek(asof),
        "per_seat": per_seat,
        "book_summary": book_summary,
        "leaderboard": leaderboard,
        "firm_exposure": firm,
        "whats_working": whats_working,
        "whats_broken": whats_broken,
        "tuning_recommendations": tuning,
        "note_md": _fallback_note(asof, per_seat, book_summary, whats_working, whats_broken,
                                  tuning, firm_flags),
    }
    return rep


def _isoweek(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _fallback_note(asof, per_seat, book_summary, working, broken, tuning, firm_flags=None) -> str:
    """A deterministic human note used when no LLM is available (write() upgrades it to Opus prose)."""
    L = [f"# CIO / Meta-PM weekly review — {_isoweek(asof)} (as of {asof.isoformat()})", "",
         "_Accountability review of the Flagship desk seats. This note recommends only — it does not "
         "trade and does not change any seat's behavior._", "", "## Per-seat calibration"]
    for s in per_seat:
        mult = s["multiplier"]
        rel = s["reliability"]
        relpct = f"{rel * 100:.0f}%" if rel is not None else "n/a"
        L.append(f"- **{s['label']}** — {s['reputation']} · multiplier {mult} · reliability {relpct} "
                 f"· n={s['n_resolved']} ({s['calibration_status']})"
                 + (" · KPI significant" if (s['kpis'] or {}).get('significant') else ""))
    L += ["", "## Books — NAV vs SPY"]
    if book_summary:
        for b in book_summary:
            L.append(f"- **{b['book']}** — ret {b['ret_pct']}% vs SPY {b['spy_ret_pct']}% "
                     f"(excess {b['excess_pct']}%, {b['n_points']} pts)")
    else:
        L.append("- (no NAV history yet)")
    # READ-ONLY firm-level cross-book concentration — observational only (never resizes a book).
    L += ["", "## Firm exposure — cross-book concentration (monitor only)"]
    if firm_flags:
        for f in firm_flags[:6]:
            books = ", ".join(f.get("books_holding") or [])
            L.append(f"- **{f.get('ticker')}** — {f.get('n_books')} book(s) "
                     f"({books}) · firm wt {f.get('firm_weight_pct')}% · {f.get('reason')}")
    else:
        L.append("- (no cross-book pile-ups flagged)")
    L += ["", "## What is working"]
    L += [f"- {w}" for w in working] or ["- (nothing has cleared the significance bar yet)"]
    L += ["", "## Who is miscalibrated"]
    L += [f"- {b}" for b in broken] or ["- (no seat is currently flagged overconfident)"]
    L += ["", "## Tuning recommendations (non-binding)"]
    L += [f"- {t}" for t in tuning] or ["- (none)"]
    return "\n".join(L)


def _narrate(rep: dict) -> str | None:
    """Ask Opus (role='deep') to write the prose 'what's working / who's miscalibrated' note from the
    deterministically-computed numbers. Returns None on any failure → caller keeps the fallback note."""
    try:
        from brain import client
        if not client.available():
            return None
        payload = {k: rep[k] for k in
                   ("as_of", "iso_week", "per_seat", "book_summary", "whats_working",
                    "whats_broken", "tuning_recommendations") if k in rep}
        sys = (
            "You are the CIO / Meta-PM of a paper-only, narrative US-equity desk. You are writing the "
            "weekly accountability note for four reasoning seats (MACRO STRATEGIST, PM-CONVICTION, "
            "GATE OFFICER, RISK OFFICER). You are given their deterministically-computed calibration "
            "multipliers, graded KPIs, book NAV-vs-SPY, and reputation labels. Write a tight markdown "
            "note: (1) what is working, (2) who is miscalibrated and the characteristic pattern, "
            "(3) the non-binding tuning recommendations already computed — restated in plain English. "
            "RULES: you recommend only; never propose trades, sizing, or auto-changes. Be blunt and "
            "honest, no moralizing. Do NOT invent numbers — use only what is in the payload. Treat "
            "'inert' seats (n < MIN_N) as not-yet-judgeable, not as failures.")
        txt, _meta = client.call_model(sys, json.dumps(payload, default=str), role="deep", max_tokens=1500)
        return txt or None
    except Exception:  # noqa: BLE001
        return None


def write(asof: date | None = None, narrate: bool = True) -> dict:
    """Build the review and persist data/brain/cio/<isoweek>.{json,md}. Returns
    {ok, week, json_path, md_path, narrated}. Never raises; honest result on failure."""
    asof = asof or date.today()
    try:
        rep = review(asof)
    except Exception:  # noqa: BLE001
        rep = {"as_of": asof.isoformat(), "iso_week": _isoweek(asof), "per_seat": [],
               "book_summary": [], "leaderboard": {}, "whats_working": [], "whats_broken": [],
               "tuning_recommendations": [], "note_md": f"# CIO review — {_isoweek(asof)}\n\n(no data)"}

    narrated = False
    if narrate:
        prose = _narrate(rep)
        if prose:
            rep["note_md"] = prose
            narrated = True

    week = rep.get("iso_week") or _isoweek(asof)
    json_path = _OUT / f"{week}.json"
    md_path = _OUT / f"{week}.md"
    ok = True
    try:
        _OUT.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(rep, indent=2, default=str))
        md_path.write_text(rep.get("note_md") or "")
    except Exception:  # noqa: BLE001
        ok = False
    return {"ok": ok, "week": week, "narrated": narrated,
            "json_path": str(json_path), "md_path": str(md_path)}
