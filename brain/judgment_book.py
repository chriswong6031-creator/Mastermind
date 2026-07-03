"""JUDGMENT BOOK — orchestrates the Flagship deep-reasoning thematic BUY layer.

This is section (C) of the build: it stitches the two seats together and reshapes the
engine's confirmed conviction list into the PM's target book, BUT keeps the EXACT
list-of-dicts schema ``portfolio.conviction.build`` emits — so the unchanged downstream
``bot/phase2.py`` loop (DecisionDoc build, caps, detectors, persist, paper rebalance + mark,
shadow replay) processes the judgment book IDENTICALLY to the engine book. The judgment book
therefore stays rebalanced, marked, published, and gradable just like the engine path.

Pipeline (flag-gated, default OFF — see ``bot/phase2.py`` injection point):
  1. MACRO STRATEGIST (``brain/strategist.py``) — top-down confirmed-leadership themes + backdrop.
  2. PM-CONVICTION (``brain/pm_conviction.py``) — armed Opus PM builds the Flagship target book,
     seeded with engine candidates + rejected pool + Strategist themes + FORGE summaries.
  3. For EACH PM holding: run the existing blind SENTINEL + subtract-only NEXUS pass
     (``brain/committee.assess``). A novel PM-added name (no engine decision matrix) gets a
     synthetic ``breakdown={"confirmed": True}`` so NEXUS treats it as a confirmed buy that
     SENTINEL can still veto — "judgment leads, adversary checks". NEXUS is subtract-only: it
     can ``trim``/``drop`` but never escalate.
  4. Re-apply the ``name_cap`` clamp (NEXUS-style concentration / no-leverage discipline).
  5. Re-emit shadow inputs for each PM name so forward grading stays intact.

Additive + reversible: if either seat is unavailable / the PM did not submit, ``build`` returns
the input ``sized`` UNCHANGED → the engine path is byte-identical. Never raises.
"""
from __future__ import annotations

import os

# ── default per-name cap if the caller does not pass one (matches config/caps.name_cap) ──
_DEFAULT_NAME_CAP = 0.08


def _enabled() -> bool:
    return os.environ.get("MASTERMIND_FLAGSHIP_JUDGMENT", "0").strip().lower() \
        in ("1", "true", "yes", "on")


def _gate_officer_enabled() -> bool:
    """The portfolio-level GATE OFFICER veto over the PM's proposed book runs ONLY when explicitly
    enabled. Default OFF — so the judgment book is BYTE-IDENTICAL to today until the user opts in.
    Enable with env MASTERMIND_GATE_OFFICER in {1, true, yes, on}; anything else is OFF.
    (Mirrors the MASTERMIND_FLAGSHIP_JUDGMENT env-flag pattern.)"""
    return os.environ.get("MASTERMIND_GATE_OFFICER", "0").strip().lower() \
        in ("1", "true", "yes", "on")


def _macro_risk_enabled() -> bool:
    """The top-down MACRO RISK OFFICER teeth (subtract-only gross cap + cracking-chain trim + add-block
    on the proposed book, bound to the deterministic risk state) run ONLY when explicitly enabled.
    Default OFF — so the judgment book is BYTE-IDENTICAL until the user opts in. Enable with env
    MASTERMIND_MACRO_RISK in {1, true, yes, on}. (Mirrors the MASTERMIND_GATE_OFFICER pattern.)"""
    return os.environ.get("MASTERMIND_MACRO_RISK", "0").strip().lower() \
        in ("1", "true", "yes", "on")


def _shadow_entry(asof: str, ticker: str, weight: float, conviction, thesis: str,
                  confluence: float) -> dict:
    """A self-contained shadow-book decision record for a PM name, mirroring the schema
    ``bot/phase2._emit_shadow`` writes so the forward shadow books can replay the judgment
    policy offline (no LLM). Entry-technical fields are pulled defensively."""
    tech = {}
    try:
        from portfolio import lenses as lenses_mod
        _sd = lenses_mod._load(f"site/stockdata/{ticker}.json") or {}
        _g = lenses_mod._g
        tech = {
            "pct_vs_200dma": _g(_sd, "tech.pct_vs_200dma"),
            "rs": _g(_sd, "momentum.alpha.rs"),
            "urgency": _g(_sd, "entry_signal.urgency"),
            "eq_grade": (_g(_sd, "conviction.ext.grade")
                         or _g(_sd, "conviction.extension.grade")),
            "parabolic": bool(_g(_sd, "conviction.ext.parabolic")
                              or _g(_sd, "conviction.extension.parabolic")),
        }
    except Exception:  # noqa: BLE001
        tech = {"pct_vs_200dma": None, "rs": None, "urgency": None,
                "eq_grade": None, "parabolic": False}
    return {
        "ticker": ticker, "confluence": confluence,
        "is_new": True, "retained": False,
        "forge_confirmed": True,
        "engine_score": None, "research_score": None,
        "combined": None, "viability": None,
        "size_mult": None, "base_weight": weight,
        "name_cap": None, "weight_forge": round(float(weight or 0.0), 4),
        "weight_prod": round(float(weight or 0.0), 4),
        "committee": None, "sentinel": None, "price": None,
        "raw_prob_correct": round(0.55 + min(0.15, float(confluence or 0.0) * 0.4), 2),
        "horizon_d": 21, "thesis_id": f"{asof}-{ticker}-conv",
        "source": "pm_judgment",
        "extension": tech["pct_vs_200dma"], "pct_vs_200dma": tech["pct_vs_200dma"],
        "rs": tech["rs"], "urgency": tech["urgency"],
        "eq_grade": tech["eq_grade"], "parabolic": tech["parabolic"],
    }


def build(sized: list[dict], rejected: list[dict], *, regime: dict | None, asof: str,
          gate_info: dict | None, shadow_inputs: list | None,
          portfolio_ctx: dict | None = None, name_cap: float | None = None,
          directive: str | None = None,
          leadership: list[dict] | None = None) -> list[dict]:
    """Reshape the engine's confirmed conviction list (``sized``) into the PM's target book,
    checked by SENTINEL + NEXUS and clamped by ``name_cap``. Returns the SAME schema
    ``conviction.build`` emits, so the downstream loop is unchanged.

    W4 B1 — the LEADERSHIP PIPE (kills the placebo hole). ``leadership`` is the engine's
    sleeve-tagged leadership legs (the 40-60% NAV sleeve). Before W4 the PM saw ONLY the conviction
    sleeve, so arming the judgment layer was a placebo — the leadership sleeve was structurally
    untouchable. Now the PM sees the full sleeve-tagged book. Its authority over a leadership leg is
    strictly DROP (→ its budget goes to cash or a defensive pick, handled by omission) or KEEP (at
    the engine's equal weight) — NEVER re-weight a surviving leg. That rule is enforced
    DETERMINISTICALLY here (``_clamp_leadership_authority``), not just by prompt language: any
    surviving leadership leg the PM re-weighted is RESTORED to its engine weight and logged
    ``authority_clamped``.

    Degrade-safe: returns the input ``sized`` UNCHANGED whenever the flag is OFF, either seat
    is unavailable, or the PM did not produce a book → the engine path stays byte-identical (the
    leadership legs, which arrive separately in ``book`` at the call site, pass through untouched).
    Never raises."""
    if not _enabled():
        return sized

    gate_info = gate_info or {}
    portfolio_ctx = portfolio_ctx or {}
    rejected = list(rejected or [])
    leadership = list(leadership or [])
    cap = float(name_cap) if name_cap is not None else _DEFAULT_NAME_CAP
    # engine weight per leadership ticker (the equal weight the PM may KEEP but never re-weight).
    _lead_engine_w = {str(c.get("ticker") or "").upper().strip(): c.get("weight")
                      for c in leadership if c.get("ticker")}

    try:
        from brain import strategist, pm_conviction, committee
        from portfolio import lenses as lenses_mod
    except Exception:  # noqa: BLE001
        return sized

    # ── WATCHLIST RE-REVIEW (additive, never-raises) ───────────────────────────────────────────
    # Before the desk runs, re-review every parked name: promote the ones whose withhold reason has
    # CLEARED back into the candidate pool the PM/Strategist see (so the desk reconsiders them this
    # cycle), age the rest, and expire the stale ones (those just drop). The predicate re-runs the
    # EXACT L3 timing check (watchlist.timing_withhold on the name's entry-tech fields) so a name is
    # promoted iff its entry technicals are no longer poor. Best-effort: any failure leaves the
    # candidate pool untouched, so the byte-identical guarantee (desk OFF ⇒ no review) is preserved
    # — this whole block is inside `_enabled()`.
    try:
        from portfolio import watchlist as _wl

        def _still_withheld(ticker: str):
            try:
                from bot.phase2 import _entry_tech_fields
                return _wl.timing_withhold(_entry_tech_fields(ticker))
            except Exception:  # noqa: BLE001 — a predicate failure ages the name (keeps it parked)
                return "re-check unavailable"

        _rev = _wl.review(asof, still_withheld=_still_withheld)
        _existing = {str(c.get("ticker") or "").upper().strip()
                     for c in (sized or [])} | {str(r.get("ticker") or "").upper().strip()
                                                 for r in rejected}
        for _pc in _wl.promote_candidates(asof):
            t = str(_pc.get("ticker") or "").upper().strip()
            if not t or t in _existing:
                continue
            # re-surface as a candidate the PM may champion (it re-enters at the Strategist/candidacy
            # stage, skipping re-sourcing — the thesis is already research-confirmed, §3.6). We feed
            # it through the `rejected` pool (the PM's "names you MAY champion" seed) so the desk
            # reconsiders it WITHOUT us self-authorizing a buy.
            rejected.append({
                "ticker": t,
                "confluence": (float(_pc["combined"]) / 100.0
                               if isinstance(_pc.get("combined"), (int, float)) else None),
                "combined": _pc.get("combined"),
                "vetoes": [],
                "bear": [],
                "source": "watchlist_promote",
                "thesis": _pc.get("thesis"),
            })
            _existing.add(t)
    except Exception:  # noqa: BLE001 — the re-review is additive; never break the build
        pass

    # NIGHTLY COST TRIPWIRE — the Flagship judgment path is the desk's most expensive (the armed
    # Opus PM + a per-name committee loop). If this book has already hit the configured per-night
    # USD cap, SKIP the whole judgment path and fall back to the engine ``sized`` book. OFF by
    # default (cap <= 0 → over_budget always False) so this is a no-op and the engine path is
    # byte-identical to today.
    try:
        from brain import cost_guard
    except Exception:  # noqa: BLE001
        cost_guard = None
    if cost_guard is not None and cost_guard.over_budget("flagship", asof):
        return sized

    # (A) MACRO STRATEGIST — top-down confirmed-leadership themes (best-effort; None on failure)
    try:
        strat = strategist.run(asof, regime)
    except Exception:  # noqa: BLE001
        strat = None

    # (A2) MACRO RISK OFFICER — the top-down DEFENSE state. Computed ONCE here (flag-gated) so the PM
    # sees it as context (the advisory defensive tilt + the hard gross cap / add-block) AND the SAME
    # deterministic state binds the subtract-only teeth applied to the proposed book below. Best-effort.
    macro_rs = None
    if _macro_risk_enabled():
        try:
            from brain import macro_risk as _mr
            macro_rs = _mr.run(asof, regime)
            portfolio_ctx = {**portfolio_ctx, "macro_risk": {
                k: macro_rs.get(k) for k in ("state", "fragility", "gross_cap", "allow_adds",
                                             "drivers", "defensive_tilt")}}
        except Exception:  # noqa: BLE001 — additive; never break the build
            macro_rs = None

    # (A3) DEFENSIVE CANDIDATES — the ONE canonical generator (portfolio/defensive_candidates.py).
    # Fed to the PM as its risk-off rotation ammunition (the champion pool). Best-effort; an empty
    # pool is legal ("no defensive candidates today") — the PM simply holds cash on a drop.
    defensive = []
    try:
        from portfolio import defensive_candidates as _dc
        _rs_for_dc = (portfolio_ctx.get("macro_risk") if isinstance(portfolio_ctx, dict) else None)
        defensive = _dc.candidates(_rs_for_dc) or []
    except Exception:  # noqa: BLE001 — additive; a broken generator contributes nothing
        defensive = []

    # (B0) JOURNAL AUTO-DRAFT (W-L / L2) — draft any newly-resolved PM calls BEFORE the build so the
    # seat's prompt carries its fresh JOURNAL DUTY block (self_mirror injects it). Deterministic, no
    # LLM; best-effort — a failure leaves the duty block empty (P2 no-op), never blocks the build.
    try:
        from brain import journal as _journal
        _journal.draft_resolutions("pm", _journal_asof(asof))
    except Exception:  # noqa: BLE001 — the journal is additive; never break the build
        pass

    # (B) PM-CONVICTION — the armed Opus PM builds the Flagship target book. Now piped the FULL
    # sleeve-tagged book: the conviction candidates (sized) + the leadership legs + the defensive pool.
    try:
        book = pm_conviction.build_book(sized, rejected, regime=regime, asof=asof,
                                        strategist=strat, gate_info=gate_info,
                                        portfolio_ctx=portfolio_ctx, directive=directive,
                                        leadership=leadership, defensive=defensive)
    except Exception:  # noqa: BLE001
        book = None
    if not book or not book.get("ran") or not book.get("holdings"):
        # PM did not produce a usable book → degrade to the engine path untouched.
        return sized

    # NIGHTLY COST TRIPWIRE (re-check) — the armed PM seat above just recorded its cost, so the
    # book may now be over budget. The per-name committee loop below is the next expensive Opus
    # block; if we're over, fall back to the engine ``sized`` path rather than spend it. No-op when
    # the cap is OFF (default) → byte-identical.
    if cost_guard is not None and cost_guard.over_budget("flagship", asof):
        return sized

    # (C) per-name SENTINEL + subtract-only NEXUS check, then name_cap clamp.
    out: list[dict] = []
    for h in book["holdings"]:
        t = str(h.get("ticker") or "").upper().strip()
        if not t:
            continue
        try:
            full = lenses_mod.full(t, "name") or {}
        except Exception:  # noqa: BLE001
            full = {}
        # PM-added novel name (no engine decision matrix) → synthetic confirmed breakdown so
        # NEXUS treats it as a confirmed buy SENTINEL can still veto.
        bd = (gate_info.get(t, {}) or {}).get("breakdown") or {"confirmed": True}
        try:
            cm = committee.assess(t, asof, engine_full=full, breakdown=bd,
                                  regime=regime or {}, portfolio_ctx=portfolio_ctx)
        except Exception:  # noqa: BLE001
            cm = {"action": "confirm", "scale": 1.0, "lean": "add",
                  "rationale": "committee unavailable — PM decision stands.",
                  "sentinel_stance": None, "sentinel": None}
        action = cm.get("action")
        if action == "drop":
            continue
        try:
            base_w = float(h.get("weight") or 0.0)
        except (TypeError, ValueError):
            base_w = 0.0
        w = base_w * (float(cm.get("scale", 1.0)) if action == "trim" else 1.0)
        w = round(min(cap, w), 4)
        if w <= 0:
            continue

        syn = (full.get("synthesis") or {})
        confluence = syn.get("confluence") or 0.0
        # sleeve: a leadership ticker the PM KEPT stays 'leadership' (the authority clamp then holds
        # it at the engine's equal weight); everything else is a conviction leg.
        _sleeve = "leadership" if t in _lead_engine_w else (h.get("sleeve") or "conviction")
        out.append({
            "ticker": t,
            "weight": w,
            "confluence": confluence,
            "bull": syn.get("bull") or "",
            "bear": syn.get("bear") or "",
            "divergences": (syn.get("divergences") or []),
            "retained": False,
            "size_stage": None,
            "sleeve": _sleeve,
            "research": {"summary": h.get("thesis"), "confirmed": True},
            "committee": {k: cm.get(k) for k in
                          ("action", "scale", "lean", "rationale", "sentinel_stance")},
            "judgment": {"source": "pm", "thesis": h.get("thesis"),
                         "conviction": h.get("conviction")},
        })

    if not out:
        # the committee dropped every PM name → degrade to the engine path (don't publish empty).
        return sized

    # (D) GATE OFFICER — portfolio-level veto on the WHOLE proposed book (separation of powers).
    # Subtract-only: drop vetoed/withheld names (→ watchlist), trim oversized ones; never adds.
    # Flag-gated (MASTERMIND_GATE_OFFICER, default OFF) so this block is a no-op until armed.
    # Additive + reversible: any failure leaves `out` untouched. The invariant (can never inject a
    # name) holds structurally — apply_gate walks `out`, not the decisions list.
    if _gate_officer_enabled():
        try:
            from brain import gate_officer as _go
            from portfolio import watchlist as _wl
            _gres = _go.gate_assess(out, asof, regime=regime or {}, portfolio_ctx=portfolio_ctx)
            _kept = _go.apply_gate(out, _gres.get("decisions", []), asof=asof, watchlist=_wl)
            if _kept:
                out = _kept
            else:
                # the Gate Officer vetoed every PM name → degrade to the engine path untouched.
                return sized
        except Exception:  # noqa: BLE001 — additive; never break the build
            pass

    # (E) MACRO RISK OFFICER teeth — the DEFENSE the desk lacked on 2026-06-23. Subtract-only, bound to
    # the deterministic risk state (no LLM dependence): in caution/risk-off it caps the proposed book's
    # gross to the (driver-tightened) gross cap, trims an over-concentrated CRACKING fragility chain back
    # under cap, and HARD-STOPS net-new adds into a cracking chain (regardless of conviction). Flag-gated
    # (MASTERMIND_MACRO_RISK, default OFF) → no-op when off. apply_risk_state walks `out`, never the
    # decisions, so it can only de-risk — never inject. Any failure leaves `out` untouched.
    if _macro_risk_enabled() and macro_rs and macro_rs.get("state") != "risk_on":
        try:
            from brain import macro_risk as _mr
            from portfolio import fragility_chain as _fc
            _frag = _fc.assess_book(out, macro_rs)
            _held = set(portfolio_ctx.get("held_conviction") or [])
            _capped = _mr.apply_risk_state(out, macro_rs, fragility=_frag, held=_held)
            if _capped:
                out = _capped
            else:
                # the macro cap emptied the proposed book → don't publish empty; engine path stands.
                return sized
        except Exception:  # noqa: BLE001 — additive; never break the build
            pass

    # (F) LEADERSHIP AUTHORITY CLAMP (deterministic, NOT prompt-only) — the PM's authority over a
    # leadership leg is DROP or KEEP-at-engine-weight; it may NEVER re-weight a surviving leadership
    # leg (equal-weight rank-IC≈0 is validated doctrine). Any surviving leadership leg whose weight
    # drifted from the engine's is RESTORED here and logged 'authority_clamped'. A DROP needs no
    # action — the PM omitted the leg, and its budget goes to cash or a defensive pick by construction
    # (the freed weight is never redistributed onto conviction survivors; the downstream book caps +
    # the no-leverage gross guarantee it can only become cash or an explicit defensive holding). This
    # runs AFTER every subtract-only pass so it clamps the FINAL surviving set. Never raises.
    if _lead_engine_w:
        _clamp_leadership_authority(out, _lead_engine_w)

    # re-emit shadow inputs so the judgment policy stays replayable offline / forward-gradable.
    if shadow_inputs is not None:
        for entry in out:
            try:
                shadow_inputs.append(_shadow_entry(
                    asof, entry["ticker"], entry["weight"],
                    entry["judgment"].get("conviction"), entry["research"].get("summary") or "",
                    float(entry.get("confluence") or 0.0)))
            except Exception:  # noqa: BLE001 — shadow emission never blocks the build
                pass

    # (G) THREE-QUESTIONS DUTY — every not_holding_should rotation CALL emits a shadow entry + a 21
    # trading-day rel_return falsifier into the EXISTING thesis/shadow machinery, so a rotation the PM
    # NAMES but does not trade is still Brier-graded (kills "cash generates zero grading rows"). A
    # submission missing the three fields is still accepted; we log 'three_questions_incomplete' and
    # emit nothing (never reject a decision for schema growth — add-only). Best-effort; never raises.
    try:
        _emit_three_questions(book, asof, shadow_inputs)
    except Exception:  # noqa: BLE001 — the duty is additive; never break the build
        pass

    # (H) JOURNAL DUTY (W-L / L2) — record the PM's conscious lessons for its badly-graded drafts
    # (charter P6). Additive: an incomplete lesson is ACCEPTED + logged 'journal_incomplete', never
    # rejected (mirrors three_questions_incomplete). Best-effort; never raises.
    try:
        _record_journal_lessons(book, asof)
    except Exception:  # noqa: BLE001 — the duty is additive; never break the build
        pass

    return out


def _clamp_leadership_authority(out: list[dict], lead_engine_w: dict) -> None:
    """Restore any surviving leadership leg the PM re-weighted back to the engine's equal weight.

    In place. The PM may DROP a leadership leg (omission → its budget becomes cash/defensive) but may
    NEVER re-weight a surviving one. We compare each surviving leadership-sleeve row's weight to the
    engine weight it arrived with; a drift beyond a rounding epsilon is clamped back and the row is
    tagged ``authority_clamped`` with the from/to for the runlog. Names not in ``lead_engine_w`` are
    untouched (a conviction leg, or a defensive pick, is freely PM-weighted). Never raises."""
    for row in out:
        try:
            if str(row.get("sleeve") or "") != "leadership":
                continue
            tk = str(row.get("ticker") or "").upper().strip()
            eng = lead_engine_w.get(tk)
            if eng is None:
                continue
            try:
                eng_w = round(float(eng), 4)
            except (TypeError, ValueError):
                continue
            cur = round(float(row.get("weight") or 0.0), 4)
            if abs(cur - eng_w) > 1e-4:
                row["authority_clamped"] = {"from": cur, "to": eng_w,
                                            "reason": "leadership_reweight_forbidden"}
                row["weight"] = eng_w
        except Exception:  # noqa: BLE001 — a single bad row never breaks the clamp
            continue


def _emit_three_questions(book: dict, asof: str, shadow_inputs: list | None) -> None:
    """Turn the PM's not_holding_should rotation calls into Brier-gradable theses + shadow entries.

    Each not_holding_should entry becomes a directional (lean='add') DecisionDoc whose engine-derived
    rel_return falsifier resolves 21 trading days out, appended to the thesis ledger (source
    'pm_judgment' so brain.calibration._is_pm_thesis grades it), plus a shadow entry mirroring the
    conviction-name shadow schema (weight 0 — a CALL, not a position). own_more/own_less are recorded
    on the shadow feed as context but are not independent falsifiable positions here.

    Missing three-question fields → log 'three_questions_incomplete' and emit nothing (add-only:
    schema growth never rejects a decision). Best-effort; never raises."""
    if not isinstance(book, dict):
        return
    nhs = book.get("not_holding_should") or []
    have_all = all(isinstance(book.get(k), list) and book.get(k) for k in
                   ("own_more", "own_less", "not_holding_should"))
    if not have_all:
        # incomplete is acceptable (add-only) — surface it in a runlog-friendly note, emit nothing new.
        try:
            from bot.phase2 import _rl_log  # noqa: F401 — only used if importable
        except Exception:  # noqa: BLE001
            _rl_log = None  # type: ignore
        if _rl_log is not None:
            try:
                _rl_log("flagship_judgment", "decision", "three_questions_incomplete",
                        f"own_more={len(book.get('own_more') or [])} "
                        f"own_less={len(book.get('own_less') or [])} "
                        f"not_holding_should={len(nhs)}")
            except Exception:  # noqa: BLE001
                pass
        if not nhs:
            return

    try:
        from brain.decision import DecisionDoc
        from brain import ledger as _ledger
    except Exception:  # noqa: BLE001
        DecisionDoc = None  # type: ignore
        _ledger = None      # type: ignore

    for call in nhs:
        if not isinstance(call, dict):
            continue
        tk = str(call.get("ticker") or "").upper().strip()
        if not tk:
            continue
        why = str(call.get("why_now") or "")[:300]
        prob = call.get("probability")
        try:
            pc = float(prob) if prob is not None else 0.55
        except (TypeError, ValueError):
            pc = 0.55
        pc = max(0.50, min(0.85, pc))
        # a shadow entry (weight 0 — this is a CALL, not a held position) so the shadow replay + the
        # forward grader see the rotation call. Reuses the same schema as conviction-name shadows.
        if shadow_inputs is not None:
            try:
                se = _shadow_entry(asof, tk, 0.0, "rotation_call", why, 0.0)
                se["source"] = "pm_judgment"
                se["kind"] = "not_holding_should"
                se["thesis_id"] = f"{asof}-{tk}-rotcall"
                se["raw_prob_correct"] = round(pc, 2)
                shadow_inputs.append(se)
            except Exception:  # noqa: BLE001
                pass
        # a gradable thesis with a 21-trading-day rel_return falsifier (lean='add' → the engine
        # derives the directional check; the PM never writes its own escape hatch).
        if DecisionDoc is not None and _ledger is not None:
            try:
                doc = DecisionDoc(
                    id=f"{asof}-{tk}-rotcall", subject=tk, lean="add", conviction="medium",
                    prob_correct=pc, raw_prob_correct=round(pc, 2),
                    horizon_d=21, state_asof=str(asof)[:10], sleeve="conviction", order_layer=1,
                    thesis=f"Rotation CALL (not held): {why or 'regime-driven defensive/rotation call'}",
                    evidence=["source=pm_judgment", "kind=not_holding_should",
                              f"probability={pc}"],
                    dissent="Graded 21 trading days forward as a rel_return call even absent a trade.",
                    entry_levels={"ticker": tk},
                ).finalize()
                d = doc.to_json()
                d["source"] = "pm_judgment"
                _ledger.append(d)
            except Exception:  # noqa: BLE001
                pass


def _journal_asof(asof):
    """Coerce the build's ``asof`` (str or date) to a date for the journal. None on failure."""
    try:
        from datetime import date as _date
        if hasattr(asof, "isoformat") and not isinstance(asof, str):
            return asof
        return _date.fromisoformat(str(asof)[:10]) if asof else None
    except Exception:  # noqa: BLE001
        return None


def _record_journal_lessons(book: dict, asof) -> None:
    """Record the PM seat's conscious JOURNAL lessons for its badly-graded drafts (W-L / L2, P6).

    ``book['journal_lessons']`` is the seat's list of lesson completions (each references a draft_id
    from the JOURNAL DUTY block it was shown). We hand them to ``brain.journal.complete`` which
    records the valid ones, logs 'journal_incomplete' for any that omit their required fields (never
    rejecting — add-only, the three_questions_incomplete posture), and recomputes the seat's pins so a
    newly-adopted rule can earn a pin. Best-effort; never raises."""
    if not isinstance(book, dict):
        return
    lessons = book.get("journal_lessons")
    try:
        from brain import journal
    except Exception:  # noqa: BLE001
        return
    # even with NO completions, surface the incomplete duty (mirrors three_questions_incomplete): if
    # the seat had pending bad drafts and returned none, log it — add-only, emits nothing else.
    if not lessons:
        try:
            pend = journal.pending_for("pm")
            if pend:
                journal._log_incomplete("pm", f"{len(pend)} pending", "no_lessons_submitted")
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        journal.complete("pm", lessons, _journal_asof(asof))
    except Exception:  # noqa: BLE001
        pass
