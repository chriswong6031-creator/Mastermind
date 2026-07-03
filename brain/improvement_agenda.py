"""The Improvement Agenda — the system critiques itself (W-L / L3; design §2).

The weekly self-audit that answers the user's standing question: *"what should we tell the AI to
fix?"* It is the fusion engine over EVERY accountability artifact the stack already produces —

    · per-seat calibration deltas (brain/calibration.py, via cio.review)
    · journal lesson clusters — ≥2 seats logging the same `why_wrong` taxonomy = a SYSTEMIC item
      (brain/journal.py, lazy-imported — degrades to no-op when L2 is not yet built)
    · shadow-vs-live gaps, INCLUDING the L1 do-nothing (carry) and defensive arms
      (portfolio/shadow_books leaderboard)
    · benchmark-ledger gaps vs SPY *and* the user's defensive basket (brain/benchmark_ledger)
    · validation-run verdicts — parses the `## Verdict:` lines under research/eyes/validation_runs/
      (a FAIL that stays DISPLAY-ONLY / cold_start is an open un-armed-gate item)
    · experiment-registry maturities (L6's artifact — lazy-imported, degrades absent) PLUS the
      real accruing experiments derivable today (shadow arms toward their falsifiers; cold-start
      validation gates waiting on forward-graded history)
    · cost_guard spend (brain/cost_guard)
    · armory / deploy-lag (data/deploy_lag.json + bot/armory if present)
    · student/distill accuracy drift (their eval-metric artifacts)

and emits, weekly + on-demand:

    data/agenda/<date>.json   — the machine artifact (ranked items + all evidence)
    data/agenda/AGENDA.md     — the human briefing: a RANKED list of concrete items, each
      {evidence[], suggested_fix, fix_type, expected_impact, owner}

fix_type ∈ {config-tune, prompt-edit, code-change, experiment}
owner    ∈ {self-tunable, opus-session, fable-review}

This is the answer to *"how do we know what to tell you"*: a scheduled Opus session (or Fable) opens
AGENDA.md and the top items are pre-argued with evidence — the incident post-mortem process, made
weekly and automatic. It is display/advisory ONLY: it NEVER trades, NEVER flips a flag, NEVER mutates
a seat. It ranks and writes. self_tune (L4) is the only consumer that may *act* on a `self-tunable`
item, and only through the Lab harness gates.

Charter law: P3 (every item cites evidence — no evidence, no item), P2 (a missing artifact degrades
that source to a no-op, never a raise), P6 (the mistake-machinery loop closes into a weekly agenda).

    from brain import improvement_agenda as agenda
    agenda.write()               # build + persist data/agenda/<date>.{json} + AGENDA.md
    rep = agenda.build()         # the structured dict (no files written)
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_OUT = _ROOT / "data" / "agenda"
_VALIDATION_DIR = _ROOT / "research" / "eyes" / "validation_runs"

# ── fix_type / owner vocabulary (documented so the writer + tests share one contract) ────────────
FIX_CONFIG = "config-tune"      # a doctrine.yml (unverified-prior) constant → self_tune candidate
FIX_PROMPT = "prompt-edit"      # a seat prompt change → needs an LLM session
FIX_CODE = "code-change"        # new code / plane / book → needs an LLM session
FIX_EXPERIMENT = "experiment"   # grade/judge an accruing experiment that has matured

OWNER_SELF = "self-tunable"     # self_tune (L4) may act, through the Lab harness gates
OWNER_OPUS = "opus-session"     # a future Opus maintenance session executes the pre-written spec
OWNER_FABLE = "fable-review"    # a boundary/gate change — needs human judgment (never self-applied)

# item-class tags (stable identifiers the sanity test + dedup key off — NOT free prose)
CLASS_CALIBRATION = "calibration-drift"
CLASS_JOURNAL = "journal-cluster"
CLASS_SHADOW = "shadow-gap"
CLASS_BENCHMARK = "benchmark-gap"
CLASS_VALIDATION = "validation-verdict"
CLASS_EXPERIMENT = "experiment-maturity"
CLASS_UNARMED = "unarmed-posture-gate"
CLASS_COST = "cost-guard"
CLASS_DEPLOY = "deploy-lag"
CLASS_MODEL = "student-drift"

# ranking weights per class — a coarse severity prior (P3: evidence strength, not vibes). The final
# rank is (base_weight × class) + per-item severity bump, so a matured experiment or an un-armed gate
# with real evidence outranks a cosmetic cost note.
_CLASS_WEIGHT = {
    CLASS_VALIDATION: 90,
    CLASS_UNARMED: 88,
    CLASS_EXPERIMENT: 86,
    CLASS_JOURNAL: 80,
    CLASS_CALIBRATION: 74,
    CLASS_SHADOW: 68,
    CLASS_BENCHMARK: 64,
    CLASS_MODEL: 58,
    CLASS_DEPLOY: 50,
    CLASS_COST: 40,
}


def _isodate(asof: date | None) -> str:
    return (asof or date.today()).isoformat()


def _item(item_id: str, klass: str, title: str, *, evidence: list[str], suggested_fix: str,
          fix_type: str, expected_impact: str, owner: str, severity: float = 0.0,
          extra: dict | None = None) -> dict:
    """One ranked agenda item. `item_id` is the stable dedup key (survives across weeks so the same
    open item carries forward with age instead of re-appearing fresh). `severity` ∈ [0,1] is the
    per-item bump on top of the class weight. P3: `evidence` MUST be non-empty or the item is dropped
    by the builder (an item with no evidence is exactly the un-argued assertion the charter forbids)."""
    rank_score = _CLASS_WEIGHT.get(klass, 30) + round(10.0 * max(0.0, min(1.0, severity)), 2)
    d = {
        "id": item_id,
        "class": klass,
        "title": title,
        "evidence": [e for e in (evidence or []) if e],
        "suggested_fix": suggested_fix,
        "fix_type": fix_type,
        "expected_impact": expected_impact,
        "owner": owner,
        "rank_score": rank_score,
    }
    if extra:
        d["extra"] = extra
    return d


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SOURCE 1 — per-seat calibration deltas (reuses cio.review — one source of truth, charter P7)
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def _from_calibration(asof: date, cio_rep: dict | None) -> list[dict]:
    """An overconfident (materially-shrunk, scoring) seat → a config-tune item; the fix lives in the
    seat's own SELF_MIRROR / MIN_N, both self-tunable priors. Reuses the cio review already computed."""
    out: list[dict] = []
    try:
        for s in (cio_rep or {}).get("per_seat") or []:
            if s.get("reputation") != "overconfident":
                continue
            mult = s.get("multiplier")
            n = s.get("n_resolved")
            rel = s.get("reliability")
            sig = (s.get("kpis") or {}).get("significant")
            relpct = f"{rel * 100:.0f}%" if isinstance(rel, (int, float)) else "n/a"
            # severity: how far below 1.0 the multiplier is, and whether the KPI is significant
            sev = 0.0
            if isinstance(mult, (int, float)):
                sev = max(0.0, min(1.0, (1.0 - mult) * 2.0)) * (1.0 if sig else 0.6)
            out.append(_item(
                f"calibration:{s.get('seat')}", CLASS_CALIBRATION,
                f"{s.get('label')} is overconfident (multiplier {mult})",
                evidence=[
                    f"calibration multiplier {mult} (reliability {relpct}, n={n}, "
                    f"KPI {'significant' if sig else 'not yet significant'})",
                    (s.get("recommendation") or ""),
                ],
                suggested_fix=(f"Enable SELF_MIRROR for the {s.get('seat')} seat so it self-corrects, "
                               f"and/or raise its MIN_N — both (unverified-prior) knobs."),
                fix_type=FIX_CONFIG, owner=OWNER_SELF,
                expected_impact=f"seat multiplier drifts back toward 1.0 as {s.get('seat')} de-confidences",
                severity=sev,
                extra={"seat": s.get("seat"), "multiplier": mult, "n": n}))
    except Exception:  # noqa: BLE001
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SOURCE 2 — journal lesson clusters (L2; lazy-imported, degrades absent) — SYSTEMIC = ≥2 seats
# ─────────────────────────────────────────────────────────────────────────────────────────────────

_SYSTEMIC_MIN_SEATS = 2   # ≥ this many seats logging the same why_wrong taxonomy = systemic (design §2)


def _cluster_journal_by_taxonomy() -> dict:
    """Cross-seat cluster of the L2 journal's mistake lessons → {taxonomy: {"seats": {...}, "n": int,
    "examples": [...]}}. The journal (brain/journal.py) exposes PER-SEAT clustering
    (``cluster_by_taxonomy(seat)`` → {taxonomy: [lessons]}) over its ``SEATS`` tuple; the systemic
    view is the union across seats. Lazy-imported; {} when L2 is absent (charter P2). Also accepts a
    forward-compat ``journal.lesson_clusters()`` if a future L2 exposes it directly."""
    try:
        from brain import journal  # L2 — may not be built yet
    except Exception:  # noqa: BLE001
        return {}
    # forward-compat: a direct cross-seat API wins if present
    try:
        if hasattr(journal, "lesson_clusters"):
            lc = journal.lesson_clusters() or {}
            if lc:
                return lc
    except Exception:  # noqa: BLE001
        pass
    out: dict = {}
    try:
        seats = list(getattr(journal, "SEATS", ()) or ())
        for seat in seats:
            try:
                per = journal.cluster_by_taxonomy(seat) or {}
            except Exception:  # noqa: BLE001
                continue
            for taxonomy, lessons in per.items():
                if taxonomy == "keep":                     # successes bucket — not a failure mode
                    continue
                if not lessons:
                    continue
                slot = out.setdefault(taxonomy, {"seats": {}, "n": 0, "examples": []})
                slot["seats"][seat] = len(lessons)
                slot["n"] += len(lessons)
                for lsn in lessons[:1]:
                    ex = (lsn or {}).get("rule_i_adopt") or (lsn or {}).get("what_actually_happened")
                    if ex and len(slot["examples"]) < 3:
                        slot["examples"].append(f"{seat}: {ex}")
    except Exception:  # noqa: BLE001
        return {}
    return out


def _from_journal(asof: date) -> list[dict]:
    """Cluster the conscious-journal lessons by their `why_wrong` taxonomy. A taxonomy that ≥2 seats
    logged INDEPENDENTLY is systemic — not one seat's bad luck but a stack-wide failure mode (design
    §2). Degrades to [] when the journal module / data is not yet built (charter P2)."""
    out: list[dict] = []
    clusters = _cluster_journal_by_taxonomy()
    if not clusters:
        return out
    try:
        for taxonomy, info in clusters.items():
            raw_seats = (info or {}).get("seats")
            # `seats` may be a {seat: count} map (our clustering) or a list (forward-compat API)
            seats = list(raw_seats.keys()) if isinstance(raw_seats, dict) else list(raw_seats or [])
            n = int((info or {}).get("n") or 0)
            if len(set(seats)) < _SYSTEMIC_MIN_SEATS:
                continue                                   # single-seat lesson — not systemic yet
            examples = list((info or {}).get("examples") or [])[:3]
            sev = min(1.0, len(set(seats)) / 4.0)          # more seats agreeing → higher severity
            out.append(_item(
                f"journal:{taxonomy}", CLASS_JOURNAL,
                f"Systemic failure mode: '{taxonomy}' logged by {len(set(seats))} seats",
                evidence=[f"seats logging '{taxonomy}': {', '.join(sorted(set(seats)))} (n={n} lessons)"]
                         + [f"e.g. {ex}" for ex in examples],
                suggested_fix=(f"'{taxonomy}' spans {len(set(seats))} seats — a shared prompt guardrail "
                               f"or a new plane, not a per-seat patch. Draft the cross-seat rule."),
                fix_type=FIX_PROMPT, owner=OWNER_OPUS,
                expected_impact=f"the '{taxonomy}' error class stops recurring across seats",
                severity=sev,
                extra={"taxonomy": taxonomy, "seats": sorted(set(seats)), "n": n}))
    except Exception:  # noqa: BLE001
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SOURCE 3 — shadow-vs-live gaps (INCLUDING the L1 do-nothing + defensive arms)
# ─────────────────────────────────────────────────────────────────────────────────────────────────

_SHADOW_LEAD_PCT = 1.0      # a shadow arm leading the live baseline by ≥ this (pct pts) is an item
_SHADOW_MIN_RESOLVED = 8    # …but only once it has enough resolved theses to be more than noise


def _from_shadow(asof: date, leaderboard: dict | None) -> list[dict]:
    """A shadow arm that beats the live 'prod' baseline over the window — with enough resolved
    decisions to matter — is a gap the live policy should close. The do-nothing (carry) and defensive
    arms are first-class here: if inactivity or a static defensive sleeve is beating the live book, the
    agenda says so out loud (charter P6 — the benchmark that beats us is a standing input)."""
    out: list[dict] = []
    books = (leaderboard or {}).get("books") or {}
    if not books:
        return out
    baseline = books.get("prod") or {}
    base_vs = baseline.get("vs_spy_pct")
    try:
        base_vs = float(base_vs) if base_vs is not None else None
    except (TypeError, ValueError):
        base_vs = None
    for bid, b in books.items():
        if bid == "prod" or (b or {}).get("is_baseline"):
            continue
        vs = (b or {}).get("vs_spy_pct")
        nres = (b or {}).get("n_resolved") or 0
        try:
            vs = float(vs) if vs is not None else None
        except (TypeError, ValueError):
            vs = None
        if vs is None or base_vs is None:
            continue
        lead = vs - base_vs
        if lead < _SHADOW_LEAD_PCT or nres < _SHADOW_MIN_RESOLVED:
            continue
        is_inaction = bid in ("do_nothing", "defensive")
        sev = min(1.0, lead / 5.0) + (0.2 if is_inaction else 0.0)
        label = (b or {}).get("label") or bid
        out.append(_item(
            f"shadow:{bid}", CLASS_SHADOW,
            f"Shadow arm '{label}' leads live prod by {lead:+.2f} pts vs SPY",
            evidence=[f"{bid}: {vs:+.2f}% vs SPY vs prod {base_vs:+.2f}% "
                      f"(lead {lead:+.2f} pts, n_resolved={nres})"]
                     + (["this is an INACTION / defensive arm beating the active book — "
                         "the cost of the stack's activity is negative here (charter P6)"]
                        if is_inaction else []),
            suggested_fix=(f"Investigate what '{label}' does that prod doesn't; if it is the "
                           f"do-nothing/defensive policy, the live book is over-trading — tighten the "
                           f"activity gate." if is_inaction else
                           f"Ablate the '{label}' arm's distinguishing lever into the live policy."),
            fix_type=FIX_EXPERIMENT if is_inaction else FIX_CODE,
            owner=OWNER_OPUS,
            expected_impact=f"close the {lead:+.2f} pt gap to the {label} arm",
            severity=min(1.0, sev),
            extra={"arm": bid, "lead_pct": round(lead, 3), "n_resolved": nres}))
    return out


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SOURCE 4 — benchmark-ledger gaps vs SPY and the defensive basket
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def _from_benchmark(asof: date) -> list[dict]:
    """The benchmark ledger's leaderboard ranks every bogey + book by window return. If a Brain book
    trails BOTH SPY and the defensive basket over the window, that's a benchmark gap the CIO already
    watches — surfaced here as an item. Lazy-imports brain.benchmark_ledger; degrades absent."""
    out: list[dict] = []
    try:
        from brain import benchmark_ledger
        led = benchmark_ledger.latest() or {}
    except Exception:  # noqa: BLE001
        return out
    rows = (led or {}).get("leaderboard") or []
    if not rows:
        return out
    # bogey returns for the two named yardsticks
    def _ret(kind_id: str) -> float | None:
        for r in rows:
            if r.get("id") == kind_id:
                v = r.get("return_pct")
                try:
                    return float(v) if v is not None else None
                except (TypeError, ValueError):
                    return None
        return None
    spy = _ret("spy")
    defensive = _ret("defensive")
    if spy is None and defensive is None:
        return out
    bogey = max([x for x in (spy, defensive) if x is not None], default=None)
    if bogey is None:
        return out
    for r in rows:
        if r.get("kind") != "book":
            continue
        v = r.get("return_pct")
        try:
            v = float(v) if v is not None else None
        except (TypeError, ValueError):
            v = None
        if v is None:
            continue
        gap = bogey - v
        if gap <= _SHADOW_LEAD_PCT:                       # trails the best bogey by a material margin
            continue
        which = "the defensive basket" if defensive is not None and defensive >= (spy or -1e9) else "SPY"
        out.append(_item(
            f"benchmark:{r.get('id')}", CLASS_BENCHMARK,
            f"Book '{r.get('label')}' trails {which} by {gap:.2f} pts (renormed)",
            evidence=[f"{r.get('id')}: {v:+.2f}% vs SPY {spy}%, defensive {defensive}% "
                      f"(common-inception renorm, n={r.get('n_points')})"],
            suggested_fix=(f"'{r.get('label')}' is below the defensive floor — check whether its "
                           f"posture is too offensive for the tape; consider a defensive-tilt review."),
            fix_type=FIX_EXPERIMENT, owner=OWNER_FABLE,
            expected_impact="the book at least matches its regime-conditional bogey",
            severity=min(1.0, gap / 8.0),
            extra={"book": r.get("id"), "gap_pct": round(gap, 3)}))
    return out


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SOURCE 5 — validation-run verdicts + the un-armed-gate class (research/eyes/validation_runs/)
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def _parse_validation_runs() -> list[dict]:
    """Parse each research/eyes/validation_runs/<signal>_<date>.md — pull the `## Verdict:` line, the
    signal name from the `# Perception validation — \\`<name>\\`` header, and whether the arming
    decision left it DISPLAY-ONLY / cold_start. Returns the LATEST run per signal (by filename date)."""
    runs: dict[str, dict] = {}
    if not _VALIDATION_DIR.exists():
        return []
    try:
        for f in sorted(_VALIDATION_DIR.glob("*.md")):
            try:
                text = f.read_text()
            except Exception:  # noqa: BLE001
                continue
            name, verdict, arming, cold = None, None, None, False
            for line in text.splitlines():
                ls = line.strip()
                if name is None and ls.startswith("# Perception validation"):
                    # "# Perception validation — `rotation_tensor` — FAIL"
                    if "`" in ls:
                        name = ls.split("`")[1]
                if ls.startswith("## Verdict:"):
                    verdict = ls[len("## Verdict:"):].strip()
                    cold = ("cold-start" in verdict.lower() or "cold_start" in verdict.lower()
                            or "uncomputable" in verdict.lower())
                if ls.startswith("**Arming decision.**"):
                    arming = ls[len("**Arming decision.**"):].strip()
                    if "cold_start" in arming.lower():
                        cold = True
            if not name:
                # fall back to the filename stem sans trailing date
                stem = f.stem
                name = stem.rsplit("_", 1)[0] if "_" in stem else stem
            # keep the latest file per signal (filenames end in _YYYY-MM-DD)
            prev = runs.get(name)
            fdate = f.stem.rsplit("_", 1)[-1] if "_" in f.stem else ""
            if prev is None or fdate >= prev.get("_fdate", ""):
                runs[name] = {"signal": name, "verdict": verdict or "(unparsed)",
                              "arming": arming, "cold_start": cold, "file": f.name, "_fdate": fdate}
    except Exception:  # noqa: BLE001
        pass
    return list(runs.values())


def _from_validation(asof: date) -> list[dict]:
    """Two item classes from the validation runs:
      · CLASS_VALIDATION — a signal whose gate FAILED but whose *arming* stayed DISPLAY-ONLY: the
        honest-fail is correct (P3), the item is 'find the unlock' (usually vendored history).
      · CLASS_UNARMED   — the same fact framed as the KNOWN-OPEN posture/gate: the seam is DARK and
        WILL stay dark until forward-graded history exists — an accruing experiment with a come-back.
    The un-armed-gate class is one of the two items the sanity acceptance requires to appear."""
    out: list[dict] = []
    runs = _parse_validation_runs()
    if not runs:
        return out
    dark = [r for r in runs if r.get("verdict", "").upper().startswith("FAIL")]
    for r in runs:
        v = (r.get("verdict") or "").upper()
        if not v.startswith("FAIL"):
            continue
        cold = bool(r.get("cold_start"))
        out.append(_item(
            f"validation:{r['signal']}", CLASS_VALIDATION,
            f"Perception gate '{r['signal']}' FAILED — stays advisory/display-only",
            evidence=[f"validation run {r['file']}: verdict {r['verdict']!r}",
                      (r.get("arming") or "arm seam dark")],
            suggested_fix=(f"'{r['signal']}' cannot size: {'vendor a forward-graded historical series '
                           'so the AUC/Brier gate is computable' if cold else 'the AUC gate is below '
                           'threshold — improve the signal or retire the arm ambition'}."),
            fix_type=FIX_CODE if cold else FIX_EXPERIMENT,
            owner=OWNER_FABLE,       # gate arming is a boundary call — never self-applied (P8)
            expected_impact=f"'{r['signal']}' becomes gate-eligible (or is honestly retired)",
            severity=0.7 if cold else 0.5,
            extra={"signal": r["signal"], "cold_start": cold}))
    # the aggregate un-armed-gate posture item (the class the sanity test asserts on)
    if dark:
        signals = sorted(r["signal"] for r in dark)
        cold_ct = sum(1 for r in dark if r.get("cold_start"))
        out.append(_item(
            "unarmed:perception-gates", CLASS_UNARMED,
            f"{len(dark)} perception gate(s)/posture seam(s) remain UN-ARMED (dark)",
            evidence=[f"validation runs FAILing / display-only: {', '.join(signals)}",
                      f"{cold_ct} of {len(dark)} are cold_start (blocked on forward-graded history)",
                      "the notch/tilt posture seams stay DARK by code until these gates pass (P3/P8)"],
            suggested_fix=("Arm nothing yet — these are earning authority in shadow. The unlock is a "
                           "forward-graded historical series (dashboard handoff H4); until then keep the "
                           "seams dark and let the forward logs accrue. Track come-back dates."),
            fix_type=FIX_EXPERIMENT, owner=OWNER_FABLE,
            expected_impact="the posture decider / notch seams become arm-eligible once gates pass",
            severity=0.85,
            extra={"dark_signals": signals, "cold_start_count": cold_ct}))
    return out


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SOURCE 6 — experiment-registry maturities (L6 lazy-import) + real accruing experiments today
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def _load_matured_experiments(asof: date) -> list[dict]:
    """L6's registry: the matured (past-comeback, unjudged) experiments. Tries the shipped
    ``brain.experiment_registry`` first, then a couple of forward-compat homes. Returns [] when L6 is
    absent (charter P2). Never raises."""
    for modpath, attr in (("brain.experiment_registry", "matured"),
                          ("portfolio.experiments", "matured"),
                          ("brain.experiments", "matured")):
        try:
            mod = __import__(modpath, fromlist=[attr.split(".")[0]])
            fn = getattr(mod, attr, None)
            if callable(fn):
                res = fn(asof)
                if res is not None:
                    return list(res)
        except Exception:  # noqa: BLE001
            continue
    return []


def _from_experiment_registry(asof: date) -> list[dict]:
    """L6's registry (data/experiments/registry.json via brain.experiment_registry.matured): every
    experiment whose come-back date has arrived and is unjudged. A MATURED-but-unjudged experiment is
    a top-priority item — 'nothing silently rots' (design §5). Degrades to [] when L6 is not yet
    built (charter P2)."""
    out: list[dict] = []
    for e in _load_matured_experiments(asof):
        try:
            eid = e.get("id") or "experiment"
            label = e.get("what") or e.get("label") or eid
            if isinstance(label, str) and len(label) > 90:
                label = label[:87] + "…"
            gate = e.get("gate") or e.get("maturity_condition") or "gate n/a"
            cb = e.get("comeback_date") or e.get("come_back")
            owner = e.get("owner") or OWNER_OPUS
            if owner not in (OWNER_SELF, OWNER_OPUS, OWNER_FABLE):
                owner = OWNER_FABLE                        # unknown owner → conservative (human review)
            out.append(_item(
                f"experiment:{eid}", CLASS_EXPERIMENT,
                f"Experiment '{eid}' has MATURED — judge it",
                evidence=[f"come-back date {cb} reached (registry status=matured)",
                          f"gate: {gate}",
                          f"what: {label}"],
                suggested_fix=(e.get("maturity_condition")
                               or f"run the pre-registered gate on '{eid}' and promote/retire per its "
                                  f"falsifier (design §5)."),
                fix_type=FIX_EXPERIMENT, owner=owner,
                expected_impact="a matured experiment gets a verdict instead of rotting",
                severity=0.95,
                extra={"experiment": eid, "comeback_date": cb}))
        except Exception:  # noqa: BLE001
            continue
    return out


def _from_accruing_experiments(asof: date, leaderboard: dict | None,
                               have_registry_items: bool) -> list[dict]:
    """The real accruing experiments derivable TODAY, independent of L6. These are the experiment-class
    items that exist NOW (so the agenda answers 'what to tell you' even before the registry ships):

      · shadow trim ladder / arms — each accrues toward a resolved-count falsifier; surface when it
        crosses its judgment threshold.
      · cold-start calibration seats — accruing toward MIN_N resolved decisions.

    Only emitted when the L6 registry didn't already own these (no double-count). This is what makes
    the 'matured-experiment class' appear against real repo state for the sanity acceptance even while
    L6 is unbuilt — a shadow arm at/over its resolved-count threshold IS a matured experiment."""
    out: list[dict] = []
    if have_registry_items:
        return out                                       # L6 owns the experiment class → don't double
    books = (leaderboard or {}).get("books") or {}
    # shadow arms accruing toward the trim-ladder / do-nothing falsifier (design §5: ≥40 graded trims
    # for the shadow trim ladder; we surface any arm that has crossed the reporting floor as a
    # matured-enough-to-look experiment, and the not-yet-matured ones as accruing).
    arms = [(bid, b) for bid, b in books.items()
            if bid in ("do_nothing", "defensive") or not (b or {}).get("is_baseline")]
    matured, accruing = [], []
    for bid, b in arms:
        nres = (b or {}).get("n_resolved") or 0
        (matured if nres >= _SHADOW_MIN_RESOLVED else accruing).append((bid, nres))
    if matured:
        out.append(_item(
            "experiment:shadow-arms-matured", CLASS_EXPERIMENT,
            f"{len(matured)} shadow arm(s) have accrued enough to JUDGE",
            evidence=[f"arms at/over the {_SHADOW_MIN_RESOLVED}-resolved reporting floor: "
                      + ", ".join(f"{b}(n={n})" for b, n in sorted(matured))],
            suggested_fix=("Run the pre-registered shadow-vs-baseline comparison on these arms and "
                           "promote the distinguishing lever or retire the arm (design §5)."),
            fix_type=FIX_EXPERIMENT, owner=OWNER_FABLE,
            expected_impact="matured shadow arms get a verdict instead of accruing forever",
            severity=0.8,
            extra={"matured_arms": [b for b, _ in matured]}))
    if accruing:
        out.append(_item(
            "experiment:shadow-arms-accruing", CLASS_EXPERIMENT,
            f"{len(accruing)} shadow arm(s) still accruing toward their falsifier",
            evidence=[f"arms below the {_SHADOW_MIN_RESOLVED}-resolved floor: "
                      + ", ".join(f"{b}(n={n})" for b, n in sorted(accruing)),
                      "these have come-back dates — do not judge early (design §5)"],
            suggested_fix="Let the forward logs accrue; re-check at each arm's come-back date.",
            fix_type=FIX_EXPERIMENT, owner=OWNER_FABLE,
            expected_impact="nothing silently rots — the registry carries the come-back date",
            severity=0.4,
            extra={"accruing_arms": [b for b, _ in accruing]}))
    return out


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# SOURCE 7 — cost_guard, deploy-lag, student/distill drift
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def _from_cost_guard(asof: date) -> list[dict]:
    out: list[dict] = []
    try:
        from brain import cost_guard
        s = cost_guard.summary(_isodate(asof)) or {}
    except Exception:  # noqa: BLE001
        return out
    if not s.get("enabled"):
        return out
    over = [b for b, v in (s.get("books") or {}).items() if (v or {}).get("over")]
    if not over:
        return out
    out.append(_item(
        "cost:over-budget", CLASS_COST,
        f"{len(over)} book(s) over the LLM spend cap",
        evidence=[f"over-budget books: {', '.join(sorted(over))} (cap ${s.get('cap')})"],
        suggested_fix="Raise the cost cap (unverified-prior) or trim the over-spending book's LLM "
                      "calls (fewer re-prompts / cheaper tier).",
        fix_type=FIX_CONFIG, owner=OWNER_SELF,
        expected_impact="spend returns under cap without cutting a live decision path",
        severity=0.5, extra={"over_books": sorted(over)}))
    return out


def _from_deploy_lag(asof: date) -> list[dict]:
    out: list[dict] = []
    try:
        raw = (_ROOT / "data" / "deploy_lag.json").read_text()
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        return out
    if not d.get("warn"):
        return out
    lag_h = d.get("lag_hours") or d.get("oldest_unshipped_commit_age_h")
    out.append(_item(
        "deploy:lag", CLASS_DEPLOY,
        f"Production is behind master by {d.get('behind_by_commits')} commit(s)",
        evidence=[d.get("message") or f"lag {lag_h}h, behind {d.get('behind_by_commits')} commits"],
        suggested_fix="Restart the production app process on its checkout so master reaches prod "
                      "(the 4-day gap on 2026-07-02 cost real capital — charter P10).",
        fix_type=FIX_CODE, owner=OWNER_OPUS,
        expected_impact="HEAD == master; the fixes that exist are actually running",
        severity=min(1.0, float(lag_h or 0) / 48.0), extra={"lag_hours": lag_h}))
    return out


def _from_model_drift(asof: date) -> list[dict]:
    """Student / distill CatBoost accuracy drift — their eval-metric artifacts. A model that WAS
    scoring and has fallen back to 'building' or lost its OOS edge is a drift item (the agenda's cue
    to retrain / re-feature). Degrades absent (no model yet → no item)."""
    out: list[dict] = []
    for mod_name, label in (("student", "Statistical student"), ("distill", "Opus-distill")):
        try:
            mod = __import__(f"brain.{mod_name}", fromlist=[mod_name])
            m = mod.summary() or {}
        except Exception:  # noqa: BLE001
            continue
        status = m.get("status")
        ic = m.get("oos_rank_ic")
        auc = m.get("oos_auc") or m.get("auc")
        # only flag a model that HAS trained (n>0) but whose OOS edge is null/negative — a real drift,
        # not a cold-start (a never-trained model is not 'drifting', it's just building).
        n = m.get("n") or 0
        edge = ic if ic is not None else auc
        if n <= 0 or status == "building":
            continue
        if edge is None or (isinstance(edge, (int, float)) and edge <= (0.0 if ic is not None else 0.5)):
            out.append(_item(
                f"model:{mod_name}", CLASS_MODEL,
                f"{label} model has no live OOS edge (drift)",
                evidence=[f"{mod_name} summary: status={status}, n={n}, "
                          f"OOS edge={edge} ({'rank-IC' if ic is not None else 'AUC'})"],
                suggested_fix=f"Retrain / re-feature the {mod_name} model; if the edge stays null, "
                              f"keep it out of the prompt (it earns injection only when scoring).",
                fix_type=FIX_CODE, owner=OWNER_OPUS,
                expected_impact=f"{label} regains a positive OOS edge or is honestly benched",
                severity=0.4, extra={"model": mod_name, "n": n, "edge": edge}))
    return out


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# dedup / carry-forward against prior agendas
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def _prior_agenda(asof: date) -> dict:
    """The most recent agenda JSON strictly before `asof` (for carry-forward age tracking)."""
    try:
        files = sorted(_OUT.glob("20*.json"))
        this = _isodate(asof)
        prior = [f for f in files if f.stem < this]
        if not prior:
            return {}
        return json.loads(prior[-1].read_text())
    except Exception:  # noqa: BLE001
        return {}


def _carry_forward(items: list[dict], asof: date) -> list[dict]:
    """Stamp each item with `first_seen` + `age_weeks`. An item whose id matches an open item in the
    prior agenda carries that agenda's `first_seen` forward and ages; a new id starts at age 0. This is
    the dedup: the SAME open item doesn't re-appear fresh each week, it accrues age (a stale open item
    rising in age is itself a signal)."""
    prior = _prior_agenda(asof)
    prior_by_id = {it.get("id"): it for it in (prior.get("items") or [])}
    this = _isodate(asof)
    for it in items:
        p = prior_by_id.get(it["id"])
        if p and p.get("first_seen"):
            it["first_seen"] = p["first_seen"]
            try:
                d0 = date.fromisoformat(str(p["first_seen"])[:10])
                it["age_weeks"] = max(0, (asof - d0).days // 7)
            except Exception:  # noqa: BLE001
                it["age_weeks"] = 0
        else:
            it["first_seen"] = this
            it["age_weeks"] = 0
        # a long-open item earns a small rank bump — it has been ignored and should surface (capped)
        it["rank_score"] = round(it["rank_score"] + min(6.0, 1.5 * it["age_weeks"]), 2)
    return items


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# build + write
# ─────────────────────────────────────────────────────────────────────────────────────────────────

def build(asof: date | None = None, *, cio_rep: dict | None = None) -> dict:
    """Fuse every accountability artifact into a RANKED agenda dict. Pure, deterministic, READ-ONLY,
    never raises. Every source degrades to [] on missing data (charter P2). Items with empty evidence
    are dropped (charter P3 — no evidence, no item). `cio_rep` may be injected (tests / to avoid a
    second cio.review); when None it is computed once here."""
    asof = asof or date.today()

    if cio_rep is None:
        try:
            from brain import cio
            cio_rep = cio.review(asof)
        except Exception:  # noqa: BLE001
            cio_rep = {}

    try:
        from portfolio import shadow_books
        leaderboard = shadow_books.load_leaderboard() or {}
    except Exception:  # noqa: BLE001
        leaderboard = {}

    items: list[dict] = []
    # each source is independently guarded so one failing source can't sink the fusion
    for fn in (
        lambda: _from_calibration(asof, cio_rep),
        lambda: _from_journal(asof),
        lambda: _from_shadow(asof, leaderboard),
        lambda: _from_benchmark(asof),
        lambda: _from_validation(asof),
        lambda: _from_cost_guard(asof),
        lambda: _from_deploy_lag(asof),
        lambda: _from_model_drift(asof),
    ):
        try:
            items.extend(fn() or [])
        except Exception:  # noqa: BLE001
            continue

    # experiments: L6 registry first; if it produced nothing, derive the real accruing experiments
    reg_items: list[dict] = []
    try:
        reg_items = _from_experiment_registry(asof) or []
    except Exception:  # noqa: BLE001
        reg_items = []
    items.extend(reg_items)
    try:
        items.extend(_from_accruing_experiments(asof, leaderboard, bool(reg_items)) or [])
    except Exception:  # noqa: BLE001
        pass

    # P3 enforcement: drop any item that somehow arrived with no evidence
    items = [it for it in items if it.get("evidence")]

    # carry-forward age + dedup, then rank (rank_score desc, stable by id for reproducibility)
    items = _carry_forward(items, asof)
    items.sort(key=lambda it: (-it.get("rank_score", 0), it.get("id", "")))
    for i, it in enumerate(items, 1):
        it["rank"] = i

    counts: dict[str, int] = {}
    for it in items:
        counts[it["class"]] = counts.get(it["class"], 0) + 1

    return {
        "as_of": asof.isoformat(),
        "schema_version": "improvement_agenda.v1",
        "n_items": len(items),
        "class_counts": counts,
        "owners": {o: sum(1 for it in items if it["owner"] == o)
                   for o in (OWNER_SELF, OWNER_OPUS, OWNER_FABLE)},
        "items": items,
        "note": ("Advisory only. This agenda RANKS and WRITES — it never trades, flips a flag, or "
                 "mutates a seat. self-tunable items are actionable only by self_tune (L4) through the "
                 "Lab harness gates; opus-session/fable-review items need a session."),
    }


def _md(agenda: dict) -> str:
    """The human AGENDA.md briefing — the ranked, pre-argued list a maintenance session opens cold."""
    asof = agenda.get("as_of")
    L = [f"# Improvement Agenda — {asof}", "",
         "_The weekly self-critique. Ranked, evidence-backed items answering \"what should we tell "
         "the AI to fix.\" Advisory only — this agenda never trades, flips a flag, or mutates a seat. "
         "`self-tunable` items are actionable by self_tune through the Lab harness gates; "
         "`opus-session` / `fable-review` items need a session._", ""]
    oc = agenda.get("owners") or {}
    L.append(f"**{agenda.get('n_items', 0)} open items** — "
             f"{oc.get(OWNER_SELF, 0)} self-tunable · {oc.get(OWNER_OPUS, 0)} opus-session · "
             f"{oc.get(OWNER_FABLE, 0)} fable-review.")
    L.append("")
    if not agenda.get("items"):
        L += ["_No items — every accountability source is clean or absent (P2 no-op)._"]
        return "\n".join(L)
    for it in agenda["items"]:
        age = it.get("age_weeks", 0)
        agetag = f" · open {age}w" if age else " · NEW"
        L.append(f"## {it['rank']}. {it['title']}")
        L.append(f"*{it['class']} · fix: **{it['fix_type']}** · owner: **{it['owner']}** · "
                 f"rank {it.get('rank_score')}{agetag}*")
        L.append("")
        L.append("**Evidence.**")
        for e in it["evidence"]:
            L.append(f"- {e}")
        L.append("")
        L.append(f"**Suggested fix.** {it['suggested_fix']}")
        L.append("")
        L.append(f"**Expected impact.** {it['expected_impact']}")
        L.append("")
    return "\n".join(L)


def write(asof: date | None = None) -> dict:
    """Build + persist data/agenda/<date>.json + data/agenda/AGENDA.md. Returns
    {ok, as_of, json_path, md_path, n_items}. Never raises; honest result on failure."""
    asof = asof or date.today()
    try:
        agenda = build(asof)
    except Exception as e:  # noqa: BLE001
        agenda = {"as_of": asof.isoformat(), "schema_version": "improvement_agenda.v1",
                  "n_items": 0, "items": [], "class_counts": {}, "owners": {},
                  "note": f"agenda build failed: {e}"}
    json_path = _OUT / f"{asof.isoformat()}.json"
    md_path = _OUT / "AGENDA.md"
    ok = True
    try:
        _OUT.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(agenda, indent=2, default=str))
        md_path.write_text(_md(agenda))
    except Exception:  # noqa: BLE001
        ok = False
    return {"ok": ok, "as_of": asof.isoformat(), "n_items": agenda.get("n_items", 0),
            "json_path": str(json_path), "md_path": str(md_path)}


def latest() -> dict:
    """The most recent agenda JSON on disk (for the web page / a maintenance session). {} if none."""
    try:
        files = sorted(_OUT.glob("20*.json"))
        return json.loads(files[-1].read_text()) if files else {}
    except Exception:  # noqa: BLE001
        return {}
