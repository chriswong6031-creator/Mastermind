"""Phase 2 — the gated daily loop + a multi-name 3-sleeve paper book.

gate (material-change) -> assemble state -> LLM-optional adjudication -> build the
Leadership sleeve (mechanical, top-RS sectors, trend-gated) + the Conviction sleeve
(single names through the confluence scorecard) -> cross-sleeve firebreaks -> detectors
-> ledger + Brier scorer -> persist (SQLite/Postgres) -> bridge JSON.

The doctrine's whole point shows up here: the book is PRESENT in the leader mechanically
(SMH via the leadership sleeve) without CHASING the single name (NVDA stays a gated watch
in the conviction sleeve until breadth/flow/volume/catalyst confirm).
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

import bot  # noqa: F401

from brain import gate, panel, ledger, scorer
from brain.decision import DecisionDoc
from portfolio.sleeves import enforce_book_caps, no_rotation_capacity, binding_cash
from brain import detectors
from bridge.build_portfolio import write
from data_layer import store
from bot.doctrine_config import load_doctrine
from portfolio import position_log, thesis as thesis_mod
from portfolio import lenses as lenses_mod

_V = Path(__file__).resolve().parent.parent / "vendor" / "macro"
_LEADER_NAME = {"SMH": ("NVDA", "ai_semiconductors")}   # sector ETF -> its order-1 single name


def _j(rel):
    return json.loads((_V / rel).read_text())


def _rl_log(run_id, step_type, title, detail, **kw):
    """Safe log_step wrapper — never raises."""
    try:
        if run_id is None:
            return
        from brain import runlog
        runlog.log_step(run_id, step_type, title, detail, **kw)
    except Exception:
        pass


def _append_nw_context_audit(root: Path, run_id: str, audit_row: dict) -> None:
    """Append one nw_context_audit row to the persistent JSONL sidecar.

    Extracted from the W-NW.1 seam so it can be unit-tested directly (MAJOR-3).
    Append-only; best-effort — IOErrors and all other exceptions are swallowed internally,
    matching the never-raise contract of _rl_log.  The call site also wraps in try/except
    for defence-in-depth, but this function is safe to call bare.

    Args:
        root:      repo root Path (used to locate data/brain/nw_context_audit.jsonl).
        run_id:    current run identifier string.
        audit_row: dict with keys {status, asof, age_days, n_candidates} as returned by
                   neural_web_context.audit_row(); a 'ts' field is injected here.
    """
    try:
        import json as _json
        audit_path = root / "data" / "brain" / "nw_context_audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_id": run_id,
            "status": audit_row.get("status"),
            "asof": audit_row.get("asof"),
            "age_days": audit_row.get("age_days"),
            "n_candidates": audit_row.get("n_candidates"),
            "market_lobe_stale": audit_row.get("market_lobe_stale"),
            "fresh_lobes": audit_row.get("fresh_lobes"),
            "stale_lobes": audit_row.get("stale_lobes"),
        }
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(row, default=str) + "\n")
    except Exception:
        pass  # best-effort append; never raise


def _append_treasury_context_audit(root: Path, run_id: str, audit_row: dict) -> None:
    """Append one treasury_context_audit row to the persistent JSONL sidecar.

    Mirrors ``_append_nw_context_audit`` (W-TSY.1 seam) so it can be unit-tested directly.
    Append-only; best-effort — IOErrors and all other exceptions are swallowed internally,
    matching the never-raise contract of _rl_log.  The call site also wraps in try/except
    for defence-in-depth, but this function is safe to call bare.

    Args:
        root:      repo root Path (used to locate data/brain/treasury_context_audit.jsonl).
        run_id:    current run identifier string.
        audit_row: dict with keys {status, asof, age_days, n_events, headline_state} as
                   returned by treasury_context.audit_row(); a 'ts' field is injected here.
    """
    try:
        import json as _json
        audit_path = root / "data" / "brain" / "treasury_context_audit.jsonl"
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "run_id": run_id,
            "status": audit_row.get("status"),
            "asof": audit_row.get("asof"),
            "age_days": audit_row.get("age_days"),
            "n_events": audit_row.get("n_events"),
            "headline_state": audit_row.get("headline_state"),
        }
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(row, default=str) + "\n")
    except Exception:
        pass  # best-effort append; never raise


def _market_view_inputs(
    regime: dict,
    asof: str,
    *,
    holdings: list[dict] | None = None,
) -> dict[str, dict | None]:
    """Compute the three previously-unwired Market View planes, independently and fail-soft.

    The outputs remain advisory under their existing contracts.  Reusing the regime-embedded
    liquidity-quality block is preferred because it preserves the Macro build's exact as-of and
    composition; the local classifier is only a fallback.
    """
    out: dict[str, dict | None] = {
        "distribution_tells": None,
        "liquidity_quality": None,
        "regime_nowcast": None,
    }
    try:
        from portfolio import distribution_tells as _dt

        held = holdings if holdings is not None else position_log.open_positions()
        def _asof_prices(ticker: str):
            series = _dt._default_series_fn(ticker)
            if series is None:
                return None
            try:
                return series[series.index <= asof]
            except Exception:  # noqa: BLE001
                return series

        distribution = _dt.score(held, prices_fn=_asof_prices)
        if isinstance(distribution, dict):
            distribution["asof"] = asof
            out["distribution_tells"] = distribution
    except Exception:  # noqa: BLE001 — one missing plane never blocks perception
        pass

    embedded_lq = regime.get("liquidity_quality")
    embedded_lq_asof = (
        str(embedded_lq.get("asof") or "")[:10]
        if isinstance(embedded_lq, dict)
        else ""
    )
    if (
        isinstance(embedded_lq, dict)
        and embedded_lq.get("label")
        and (not embedded_lq_asof or embedded_lq_asof <= str(asof)[:10])
    ):
        out["liquidity_quality"] = embedded_lq
    else:
        try:
            from brain import liquidity_quality as _lq

            def _asof_liquidity(name: str):
                series = _lq.series_from_frames(name)
                if series is None:
                    return None
                try:
                    return series[series.index <= asof]
                except Exception:  # noqa: BLE001
                    return series

            classified = _lq.classify(_asof_liquidity)
            classified_asof = str((classified or {}).get("asof") or "")[:10]
            if not classified_asof or classified_asof <= str(asof)[:10]:
                out["liquidity_quality"] = classified
        except Exception:  # noqa: BLE001
            pass

    try:
        from brain import regime_nowcast as _nc

        # Preserve the existing shrink-source semantics: the price-action nowcast evaluates the
        # hard regime label, while the separate risk-state plane remains independently visible.
        out["regime_nowcast"] = _nc.nowcast(
            quad=regime.get("quad"),
            quad_name=regime.get("quad_name"),
            asof=asof,
        )
    except Exception:  # noqa: BLE001
        pass
    return out


def _conv_theme_id(t: str) -> str:
    """A REAL per-name theme key for the cross-sleeve theme cap. Every conviction name used to share
    the literal 'conviction', so their summed weight tripped the 0.25 book theme-cap every run and
    silently haircut the whole sleeve. Use the name's basket slug when present, else a name-unique
    key — so UNRELATED names are never capped as one cohort (the per-name cap already bounds single
    names), while names that DO share a real basket still cap together correctly."""
    try:
        d = lenses_mod._load(f"site/stockdata/{t}.json") or {}
        mem = d.get("baskets_membership")
        if isinstance(mem, list) and mem:
            first = mem[0]
            slug = first if isinstance(first, str) else (first.get("slug") or first.get("id"))
            if slug:
                return f"theme:{slug}"
    except Exception:
        pass
    return f"name:{t.upper()}"


def _sector_phase_at_entry(ticker: str, cycles: dict | None) -> str | None:
    """The name's sector-cycle PHASE at decision time (E3 learning field). Fail-soft None.

    Resolves the name → its sector ETF (divergence_clue._default_sector_etf mirrors
    conviction._sector_of) and reads that ETF's phase from the pre-computed ``cycles`` map
    (regime_frame.cycles(), the SOLE stale-gated sector-cycle reader). An unmapped / un-sectored /
    stale-empty read degrades to None — a name simply gets no phase, never a fabricated one.
    Pure given ``cycles``; never raises. Observability-only — reads, never sizes."""
    try:
        if not cycles:
            return None
        from brain import divergence_clue as _dc
        etf = _dc._default_sector_etf(ticker)
        if not etf:
            return None
        row = cycles.get(str(etf).upper()) if isinstance(cycles, dict) else None
        if isinstance(row, dict):
            ph = row.get("phase")
            return str(ph) if ph is not None else None
        return None
    except Exception:  # noqa: BLE001 — a learning field is additive; never break the record
        return None


def _decision_time_learning_fields(ticker: str, *, cycles: dict | None,
                                   market_plane: dict | None, synthesis: dict | None) -> dict:
    """Assemble the E3 DECISION-TIME learning fields for ``ticker`` (all nullable/additive).

    Every field is derivable from data ALREADY gathered in the build and is FAIL-SOFT — a name
    with none of these just gets None fields. Nothing here sizes, gates, or changes a decision;
    these fields are merged into signal_history.make_record's ``extra=`` purely as learnable
    substrate joined to realized outcomes later. Never raises.

    Fields
    ------
    sector_phase_at_entry     : the name's sector-cycle phase (regime_frame.cycles), None on miss.
    divergence_from_sector    : the pattern of a divergence/standout clue present for the name
                                (from the engine synthesis divergences), else None.
    nw_bottom_state           : NW candidate bottom_state, None on miss.
    nw_conflicts              : NW graph_conflicts count (int), None on miss.
    nw_verdict                : NW market-plane verdict label, None on miss.
    nw_contradiction_count    : NW market-plane contradiction_count (int), None on miss.
    safe_haven_diverger       : the NW 'clean-in-conflicted' safe-haven tell (bool), None if undeterminable.
    """
    out: dict = {
        "sector_phase_at_entry": None,
        "divergence_from_sector": None,
        "nw_bottom_state": None,
        "nw_conflicts": None,
        "nw_verdict": None,
        "nw_contradiction_count": None,
        "safe_haven_diverger": None,
    }
    try:
        out["sector_phase_at_entry"] = _sector_phase_at_entry(ticker, cycles)
    except Exception:  # noqa: BLE001
        pass
    # divergence_from_sector — take the first divergence pattern the engine synthesis carries for the
    # name (a divergence-clue / standout tell already surfaced upstream); None when the name diverges
    # from nothing. Kept absent-safe: a str pattern or a {pattern:...} dict both resolve.
    try:
        divs = (synthesis or {}).get("divergences") or []
        if divs:
            first = divs[0]
            patt = first.get("pattern") if isinstance(first, dict) else first
            out["divergence_from_sector"] = str(patt) if patt is not None else None
    except Exception:  # noqa: BLE001
        pass
    # NW per-name + market context — fail-soft, all None on absence.
    try:
        from brain import neural_web_context as _nwc
        _cand = _nwc.candidate(ticker) or {}
        _bottom = _cand.get("bottom") or {}
        if isinstance(_bottom, dict):
            out["nw_bottom_state"] = _bottom.get("bottom_state") or _bottom.get("state")
        _conf = _cand.get("graph_conflicts")
        if isinstance(_conf, list):
            out["nw_conflicts"] = len(_conf)
        mp = market_plane or {}
        _verdict = (mp.get("verdict") or {}) if isinstance(mp, dict) else {}
        if isinstance(_verdict, dict):
            out["nw_verdict"] = _verdict.get("label_en") or _verdict.get("verdict")
        _cc = mp.get("contradiction_count") if isinstance(mp, dict) else None
        if _cc is not None:
            try:
                out["nw_contradiction_count"] = int(_cc)
            except (TypeError, ValueError):
                out["nw_contradiction_count"] = None
        # safe_haven_diverger — the typed 'clean in a conflicted tape' tell. decision_signals is the
        # single chokepoint (fail-soft inert when its flag is off / row absent); we only READ the
        # boolean, we never act on it. None when the signal path is inert / undeterminable.
        try:
            _sig = _nwc.decision_signals(ticker) or {}
            if not _sig.get("inert", True):
                out["safe_haven_diverger"] = bool(_sig.get("clean_in_conflicted"))
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 — NW context is additive; never break the record
        pass
    return out


def _provenance_rows(*, book, gate_info, shadow_inputs, research_held, rejected,
                     cycles=None) -> list[dict]:
    """Assemble the E2 replayable decision-provenance rows from data ALREADY computed in the build.

    ONE row per evaluated candidate (the confirmed book names, the research-held/committee/timing
    withholds, and the conviction rejects), each stamping the stage-verdict chain, the intake
    sources/vetoes, the seats that ran, the sector phase at entry, the NW context block, and the
    final {action, weight}. Pure + fail-soft: it only READS the finalized decision state and returns
    a list of decision_provenance.row(...) dicts. It sizes nothing and changes no book/verdict — the
    shadow-book record it reads from stays byte-identical (this never mutates ``shadow_inputs``).
    Never raises; a bad candidate degrades to a minimal row or is skipped.

    Row-source map (all already-computed):
      * shadow_inputs (by ticker) → forge_confirmed, committee, sentinel, nw_context, weight_prod;
      * gate_info[t]              → intake vetoes + research breakdown (engine/research/combined);
      * book (by ticker)          → final {action=verdict, weight, sleeve} for confirmed names;
      * research_held / rejected  → the withheld / vetoed names' reason + final action.
    """
    from brain import decision_provenance as _dp

    shadow_by_tkr: dict[str, dict] = {}
    for si in (shadow_inputs or []):
        if isinstance(si, dict) and si.get("ticker"):
            shadow_by_tkr.setdefault(str(si["ticker"]).upper(), si)
    book_by_tkr: dict[str, dict] = {}
    for p in (book or []):
        tk = str(p.get("ticker") or "").upper()
        if tk:
            book_by_tkr.setdefault(tk, p)

    def _one(ticker, *, final_action, final_weight, reason=None):
        tk = str(ticker or "").upper()
        gi = (gate_info or {}).get(ticker) or (gate_info or {}).get(tk) or {}
        si = shadow_by_tkr.get(tk, {})
        _full = gi.get("full") or {}
        _syn = _full.get("synthesis") or {}
        _bd = gi.get("breakdown") or {}
        # intake sources / provenance — the engine's veto surface + confluence (what the name carried in)
        sources = {
            "vetoes": _syn.get("vetoes") or [],
            "confluence": _syn.get("confluence"),
            "is_new": si.get("is_new"),
            "retained": si.get("retained"),
        }
        # the stage-verdict chain (all already decided upstream; None where a stage didn't run)
        stage_verdicts = {
            "forge_confirmed": si.get("forge_confirmed"),
            "gate": _bd.get("confirmed"),
            "engine_score": _bd.get("engine_score"),
            "research_score": _bd.get("research_score"),
            "combined": _bd.get("combined"),
            "committee": (si.get("committee") or {}).get("action") if isinstance(si.get("committee"), dict) else None,
            "timing": ("withheld" if (reason and "timing" in str(reason)) else None),
            "action": final_action,
            "reason": reason,
        }
        # the seats that ran (committee/sentinel presence is the observable seat record)
        seats = []
        if si.get("committee"):
            seats.append("committee")
        if si.get("sentinel"):
            seats.append("sentinel")
        return _dp.row(
            ticker,
            sources=sources,
            stage_verdicts=stage_verdicts,
            seats=seats or None,
            sector_phase=_sector_phase_at_entry(ticker, cycles),
            nw=si.get("nw_context"),
            final={"action": final_action, "weight": final_weight},
        )

    rows: list[dict] = []
    seen: set[str] = set()
    try:
        # confirmed book names → their final verdict + weight
        for p in (book or []):
            tk = str(p.get("ticker") or "").upper()
            if not tk or tk in seen:
                continue
            seen.add(tk)
            rows.append(_one(p.get("ticker"), final_action=p.get("verdict"),
                             final_weight=p.get("weight")))
        # research-held / committee-drop / timing-withheld names (in-book=False, weight 0)
        for h in (research_held or []):
            tk = str(h.get("ticker") or "").upper()
            if not tk or tk in seen:
                continue
            seen.add(tk)
            rows.append(_one(h.get("ticker"), final_action="held", final_weight=0.0,
                             reason=h.get("reason")))
        # conviction rejects (the negative space)
        for r in (rejected or []):
            tk = str(r.get("ticker") or "").upper()
            if not tk or tk in seen:
                continue
            seen.add(tk)
            rows.append(_one(r.get("ticker"), final_action="rejected", final_weight=0.0,
                             reason=r.get("reason")))
    except Exception:  # noqa: BLE001 — provenance is observability-only; return what we have
        pass
    return rows


def _freeze_book_list_to_prior(book: list[dict], prior: dict[str, float]) -> list[dict]:
    """Shared shape-handling: freeze a list-of-dicts book to prior weights.

    Used by both ``_firm_clamp_freeze_flagship`` (exception arm) and
    ``_stale_freeze_flagship`` (stale-anchor arm) so the 40-line list→map→freeze→rebuild
    logic lives in exactly one place.  Never raises (guardrail helpers must be safe).

    Returns a list-of-dicts book where every weight satisfies Charter P2 invariants:
      frozen[name] <= prior[name]; no new names; gross never increases.
    """
    from portfolio.freeze import freeze_to_prior as _ftp
    try:
        target_map = {str(p.get("ticker") or "").upper().strip(): float(p.get("weight") or 0.0)
                      for p in book if p.get("ticker")}
    except Exception:  # noqa: BLE001
        target_map = {}
    frozen_map = _ftp(target_map, prior)
    # Rebuild a list-of-dicts book preserving all non-weight fields from original rows
    tk_to_rows: dict[str, list[dict]] = {}
    for p in book:
        tk = str(p.get("ticker") or "").upper().strip()
        if tk:
            tk_to_rows.setdefault(tk, []).append(p)
    out_rows: list[dict] = []
    seen: set[str] = set()
    for fk, fw in frozen_map.items():
        ku = str(fk or "").upper().strip()
        if not ku or ku in seen:
            continue
        seen.add(ku)
        if ku in tk_to_rows:
            rows = tk_to_rows[ku]
            # distribute frozen weight pro-rata across duplicate rows for the same ticker
            orig_total = sum(float(r.get("weight") or 0.0) for r in rows)
            for r in rows:
                orig_w = float(r.get("weight") or 0.0)
                share = (orig_w / orig_total) if orig_total > 0 else (1.0 / len(rows))
                out_rows.append({**r, "weight": round(fw * share, 4)})
        else:
            # prior-only name: no built row exists; inject a minimal hold row
            out_rows.append({"ticker": fk, "weight": round(fw, 4),
                             "sleeve": "prior", "_sentinel_hold": True})
    return out_rows


def _firm_clamp_freeze_flagship(book: list[dict], exc: Exception,
                                run_id: object = None) -> list[dict]:
    """Exception-arm for the flagship firm-clamp block (Charter P2).

    Called when ``firm_exposure.clamp_book`` raises inside ``run_flagship``.  Returns the
    book frozen to the prior published state: no new adds, no increases vs. the prior book,
    held names retained at ``min(target, prior)`` weight.

    Prior weights come from two sources (in priority order):
      1. ``firm_exposure.published_weights("flagship")`` — the last-published latest.json
         (exact weights, best source).
      2. ``position_log.open_positions()`` — the position ledger (``current_weight`` field),
         used as a cross-check / fallback when the published file is absent.

    The downstream consumer (phase2 rebalance) treats absent names as liquidate-to-zero, so
    prior-only names are RETAINED in the output (freeze = do-not-trade, not liquidate).

    Never raises (guardrail helpers must be unconditionally safe).
    """
    # Prior weights from published latest.json
    prior: dict[str, float] = {}
    try:
        from portfolio import firm_exposure as _firm_exp
        prior = _firm_exp.published_weights("flagship")
    except Exception:  # noqa: BLE001
        pass
    # Fallback: position_log.open_positions
    if not prior:
        try:
            from portfolio import position_log as _pl
            for hp in (_pl.open_positions() or []):
                tk = str(hp.get("ticker") or "").upper().strip()
                if tk:
                    w = hp.get("current_weight") or 0.0
                    try:
                        prior[tk] = float(w)
                    except (TypeError, ValueError):
                        prior[tk] = 0.0
        except Exception:  # noqa: BLE001
            pass
    out_rows = _freeze_book_list_to_prior(book, prior)
    # Log + emit guardrail event
    try:
        _rl_log(run_id, "decision", "firm cap clamp error", f"{exc!r}"[:160])
    except Exception:  # noqa: BLE001
        pass
    try:
        from control_plane.guardrail import GuardrailResult, Severity
        GuardrailResult.failed(
            "firm_clamp",
            Severity.FREEZE,
            detail=f"clamp_book raised: {exc!r}"[:200],
            action_taken="frozen to prior book (no new adds, no weight increases)",
        ).log(job="phase2_flagship", book="flagship")
    except Exception:  # noqa: BLE001
        pass
    return out_rows


def _stale_freeze_flagship(book: list[dict], reasons: list[str],
                            run_id: object = None) -> list[dict]:
    """Stale-anchor arm for the flagship book (Charter P2, MW3 R3).

    Called BEFORE ledger/store/rebalance/publish when the macro_refresh result signals
    that a FREEZE-class anchor is stale beyond its contract budget.  Returns the book
    frozen to the prior published state: no new adds, no weight increases vs. the prior
    book.  De-risk (target < prior) passes through (min semantics).

    Prior weights come from ``firm_exposure.published_weights("flagship")``, with a
    fallback to ``position_log.open_positions()`` (same priority as the firm-clamp arm).

    FLAGSHIP-ONLY: autonomous / etf / heavyweight books read the same macro artifacts but
    their anchor-freeze wiring is a separate reviewed change.  This function must NEVER be
    called for those books.

    Never raises (guardrail helpers must be unconditionally safe).
    """
    # Prior weights from published latest.json
    prior: dict[str, float] = {}
    try:
        from portfolio import firm_exposure as _firm_exp
        prior = _firm_exp.published_weights("flagship")
    except Exception:  # noqa: BLE001
        pass
    if not prior:
        try:
            from portfolio import position_log as _pl
            for hp in (_pl.open_positions() or []):
                tk = str(hp.get("ticker") or "").upper().strip()
                if tk:
                    w = hp.get("current_weight") or 0.0
                    try:
                        prior[tk] = float(w)
                    except (TypeError, ValueError):
                        prior[tk] = 0.0
        except Exception:  # noqa: BLE001
            pass
    out_rows = _freeze_book_list_to_prior(book, prior)
    # Log + emit guardrail event
    try:
        _rl_log(run_id, "decision", "stale anchor FREEZE applied",
                f"reasons={reasons}"[:200])
    except Exception:  # noqa: BLE001
        pass
    try:
        from control_plane.guardrail import GuardrailResult, Severity
        GuardrailResult.failed(
            "stale_anchor",
            Severity.FREEZE,
            detail=f"Stale FREEZE-class anchor(s): {reasons}"[:200],
            action_taken="frozen to prior book (no new adds, no weight increases)",
            extra={"freeze_reasons": reasons},
        ).log(job="phase2_flagship", book="flagship")
    except Exception:  # noqa: BLE001
        pass
    return out_rows


_STANDOUT_ROWS: dict[str, dict] | None = None      # ticker -> buy row, indexed once per build
_STANDOUT_ROWS_ASOF: str | None = None             # the file as_of the index was built from


def _published_entry_signal(ticker: str) -> dict:
    """The dashboard's PUBLISHED per-name entry_signal (stop / buy_zone / entry_grade / chase_above)
    for a standout BUY name (P-NEW-3). The source of truth is the us_standouts buy rows, NOT the
    per-name stockdata JSON — the risk levels live on the standouts row. Every field is None-on-miss
    (invariant: absent data must never fabricate a stop — a name with no published entry_signal simply
    carries no stop and is bought at price, exactly like today). Buy rows are indexed once per build.

    Returns {stop, buy_zone, entry_grade, chase_above} — all None when the ticker is absent from the
    board or the file/field is missing."""
    global _STANDOUT_ROWS, _STANDOUT_ROWS_ASOF
    _empty = {"stop": None, "buy_zone": None, "entry_grade": None, "chase_above": None}
    try:
        d = _j("site/factordata/us_standouts.json") if (
            _V / "site/factordata/us_standouts.json").exists() else None
    except Exception:  # noqa: BLE001
        d = None
    if not isinstance(d, dict):
        return dict(_empty)
    asof = d.get("as_of")
    # rebuild the index if we haven't yet, or the artifact rolled to a new as_of mid-process
    if _STANDOUT_ROWS is None or _STANDOUT_ROWS_ASOF != asof:
        idx: dict[str, dict] = {}
        for r in (d.get("buy") or d.get("standouts") or []):
            if isinstance(r, dict) and r.get("ticker"):
                idx.setdefault(str(r["ticker"]).upper(), r)
        _STANDOUT_ROWS, _STANDOUT_ROWS_ASOF = idx, asof
    row = _STANDOUT_ROWS.get((ticker or "").upper())
    if not isinstance(row, dict):
        return dict(_empty)
    _es = row.get("entry_signal")
    if not isinstance(_es, dict):
        return dict(_empty)
    _g = lenses_mod._g
    return {
        "stop": _g(_es, "stop"),
        "buy_zone": _g(_es, "buy_zone"),          # dict {low, high, pct_from_spot} or None
        "entry_grade": _g(_es, "entry_grade"),
        "chase_above": _g(_es, "chase_above"),
    }


def _entry_tech_fields(ticker: str) -> dict:
    """The entry-technical fields the L3 timing lever needs, pulled defensively from the name's
    published stockdata JSON (the same accessors the D1/D2/D4 block uses). Every field is nullable —
    None whenever the read fails or the field is absent — so a missing snapshot never breaks the
    shadow input. Paths verified against the live schema:
      * tech.pct_vs_200dma                       — extension vs the 200dma (entry stretch)
      * conviction.ext.grade / .extension.grade  — the entry-quality grade (eq_grade)
      * conviction.ext.parabolic / .extension…   — the parabolic flag
      * momentum.alpha.rs                         — the name's relative-strength score
      * entry_signal.urgency                      — entry urgency (now/soon/later)"""
    try:
        _sd = lenses_mod._load(f"site/stockdata/{ticker}.json") or {}
    except Exception:  # noqa: BLE001
        _sd = {}
    _g = lenses_mod._g
    return {
        "pct_vs_200dma": _g(_sd, "tech.pct_vs_200dma"),
        "rs": _g(_sd, "momentum.alpha.rs"),
        "urgency": _g(_sd, "entry_signal.urgency"),
        "eq_grade": (_g(_sd, "conviction.ext.grade")
                     or _g(_sd, "conviction.extension.grade")),
        "parabolic": bool(_g(_sd, "conviction.ext.parabolic")
                          or _g(_sd, "conviction.extension.parabolic")),
    }


def _timing_gate_enabled() -> bool:
    """The L3 entry-timing gate (subtract-only WITHHOLD on poor entry technicals — extended /
    weak-RS / 'avoid' / parabolic / weak-eq → park on the watchlist instead of chasing a bad entry).

    W2.2 ('arm the coded withhold'): the DEFAULT is now ON. This brake was built, tested, and dark
    for weeks while the book kept BUYING extended names at bad entries — the exact failure it was
    written to stop. It is subtract-only (it can only DROP a would-be NEW add, never add/resize/exit
    a held name), so arming it can only make the book more disciplined. It is now OPT-OUT: set env
    MASTERMIND_TIMING_GATE to a falsy value ({0, false, no, off}) to restore the pre-W2 buy path.
    Anything unset or truthy ({1, true, yes, on, or any other non-falsy string}) is ON."""
    return os.environ.get("MASTERMIND_TIMING_GATE", "1").strip().lower() not in ("0", "false", "no", "off", "")


def _is_parabolic_withhold(tech: dict | None) -> bool:
    """True iff a timing withhold is driven by a PARABOLIC entry (the parabolic flag set OR the
    entry-quality grade is literally 'parabolic'). A parabolic entry is a hard-veto class — the
    ε-exploration channel must NOT resurrect it (see the timing-gate block). Reads the SAME
    _entry_tech_fields dict the withhold predicate reads, so the two never diverge. Pure; never
    raises — a malformed/None snapshot returns False (fail-open: only ever a NON-parabolic reason,
    which stays explorable — it can never manufacture a parabolic block out of missing data)."""
    if not isinstance(tech, dict):
        return False
    try:
        if bool(tech.get("parabolic")):
            return True
        return str(tech.get("eq_grade") or "").strip().lower() == "parabolic"
    except Exception:  # noqa: BLE001
        return False


# Where the desk-quorum TECHNICIAN seat writes one row per evaluated NEW name. A durable, agent-
# labelled sidecar so the outcome loop can later grade the seat's verdicts (mirrors the committee
# artifacts + brain.decision_provenance.write jsonl idiom). Only written in shadow/enforce mode.
_PIPELINE_LEDGER = Path(__file__).resolve().parent.parent / "data" / "pipeline" / "ledger.jsonl"


def _pipeline_ledger_write(row: dict) -> None:
    """Append ONE technician-seat row to data/pipeline/ledger.jsonl. FAIL-SOFT: mkdir parents,
    one buffered JSON line, and any error (unwritable dir, un-serializable row) is SWALLOWED — a
    ledger-write failure can NEVER break the build. Mirrors brain.decision_provenance.write. Only
    called in desk-quorum shadow/enforce mode (OFF = never called → zero cost, zero side effect)."""
    try:
        if not isinstance(row, dict):
            return
        _PIPELINE_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, default=str, ensure_ascii=False)
        with _PIPELINE_LEDGER.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 — the pipeline ledger is best-effort; never break the gate
        pass


def _judgment_enabled() -> bool:
    """The Flagship deep-reasoning JUDGMENT layer (MACRO STRATEGIST + PM-CONVICTION seats that
    reshape the engine's confirmed list into the PM's target book) runs ONLY when explicitly
    enabled. Default OFF — so the live Flagship build is BYTE-IDENTICAL to today until the user
    opts in. Enable with env MASTERMIND_FLAGSHIP_JUDGMENT in {1, true, yes, on}; anything else
    is OFF. (Mirrors the MASTERMIND_TIMING_GATE / MASTERMIND_COMMITTEE env-flag pattern.)"""
    return os.environ.get("MASTERMIND_FLAGSHIP_JUDGMENT", "0").strip().lower() in ("1", "true", "yes", "on")


def _risk_officer_enabled() -> bool:
    """The judgment-exit RISK OFFICER over HELD conviction positions (layered ON TOP of the
    mechanical detectors — D5 dead-capital, hard-veto sweep, hysteresis) runs ONLY when explicitly
    enabled. Default OFF — so the live exit path is BYTE-IDENTICAL to today until the user opts in.
    Enable with env MASTERMIND_RISK_OFFICER in {1, true, yes, on}; anything else is OFF.
    (Mirrors the MASTERMIND_FLAGSHIP_JUDGMENT / MASTERMIND_COMMITTEE env-flag pattern.)"""
    return os.environ.get("MASTERMIND_RISK_OFFICER", "0").strip().lower() in ("1", "true", "yes", "on")


def _macro_risk_enabled() -> bool:
    """The top-down MACRO RISK OFFICER gross cap (subtract-only de-risking of the FINAL book down to the
    risk-off gross cap + cracking-chain trim, bound to the deterministic risk state) runs ONLY when
    explicitly enabled. Default OFF — so the live build is BYTE-IDENTICAL until the user opts in. This
    catches the engine path even when the Flagship judgment layer is off. Enable with env
    MASTERMIND_MACRO_RISK in {1, true, yes, on}. (Mirrors the MASTERMIND_RISK_OFFICER pattern.)"""
    return os.environ.get("MASTERMIND_MACRO_RISK", "0").strip().lower() in ("1", "true", "yes", "on")


def _universe_triage_enabled() -> bool:
    """The leadership-selection UNIVERSE-TRIAGE brake (subtract-only: drop a NEW, non-held leadership
    sector whose universe_triage.sector_action == 'reduce', so a Topping/rolling sector can't claim a
    leadership slot on raw RS). Freed budget goes to CASH — a survivor is NEVER up-weighted. Default OFF
    — so the live Flagship build is BYTE-IDENTICAL to today until the user opts in. Enable with env
    MASTERMIND_UNIVERSE_TRIAGE in {1, true, yes, on}; anything else is OFF. Fail-soft: an env read never
    sinks the build. (Mirrors the MASTERMIND_MACRO_RISK / brain.intake._flag_on 0|1 pattern.)"""
    try:
        return os.environ.get("MASTERMIND_UNIVERSE_TRIAGE", "0").strip().lower() in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001 — fail-soft: default OFF on any error
        return False


def _rotation_in_mode() -> str:
    """Return the MASTERMIND_ROTATION_IN ladder value (off|watch|starter), default 'off'. Fail-soft.

    Rotation-in watchlist enrollment is active when the mode is in {'watch','starter'}; any
    unrecognized / empty value degrades to 'off' (inert → the enrollment block is a no-op, so the
    build is byte-identical to today). Mirrors brain.intake._rotation_in_mode verbatim."""
    try:
        # W8 (2026-07-19): default 'watch' — rotation-in enrollment armed (the base-turn positive
        # path, design §2.5); 'starter' sizing stays opt-in. Opt out with MASTERMIND_ROTATION_IN=off.
        raw = os.environ.get("MASTERMIND_ROTATION_IN", "watch").strip().lower()
        return raw if raw in ("off", "watch", "starter") else "off"
    except Exception:  # noqa: BLE001
        return "off"


def _suppress_reduce_sectors(leaders_pre: list[dict], held: set[str], enabled: bool,
                             action_fn) -> tuple[list[dict], set[str]]:
    """Pure leadership-selection brake for the universe-triage suppression (CHANGE 1).

    Returns ``(leaders, reduce_secs)`` where ``reduce_secs`` is the set of NEW (non-held) leader
    tickers whose ``action_fn(ticker) == 'reduce'`` and ``leaders`` is ``leaders_pre`` with those
    dropped. HELD tickers (``ticker.upper() in held``) are EXEMPT — never suppressed. When ``enabled``
    is False, ``reduce_secs`` is empty and ``leaders is leaders_pre`` (the SAME object), guaranteeing
    byte-identical behaviour to the pre-brake path. Fail-soft: any error in ``action_fn`` degrades to
    NO suppression (the whole set is emptied), so a triage fault can only ever leave the book unchanged,
    never partially suppress.

    NOTE the caller must compute the per-leg weight ``lw`` on ``len(leaders_pre)`` (NOT ``len(leaders)``)
    so a suppressed leg's budget goes to CASH and no surviving leg is ever up-weighted (invariant ii)."""
    if not enabled:
        return leaders_pre, set()
    try:
        reduce_secs = {s["ticker"] for s in leaders_pre
                       if s["ticker"].upper() not in held and action_fn(s["ticker"]) == "reduce"}
    except Exception:  # noqa: BLE001 — fail-soft: no suppression on any error
        return leaders_pre, set()
    if not reduce_secs:
        return leaders_pre, set()
    return [s for s in leaders_pre if s["ticker"] not in reduce_secs], reduce_secs


def _is_hard_exit(syn: dict) -> bool:
    """A held name must be EXITED immediately (no hysteresis) on a hard veto (parabolic / Altman /
    cycle-blocked), a confirmed STRUCTURAL downtrend, or size_authority 'blocked'. (A fresh
    falling-knife or a softened sector is NOT a hard exit — a name we own rides through a rough week.)"""
    return (bool(syn.get("vetoes")) or bool(syn.get("price_downtrend"))
            or syn.get("size_authority") == "blocked")


def _build_d5_lots(open_positions: list[dict], prices: dict, sector_pctile: dict,
                   leader_pctile, asof_date) -> list[dict]:
    """Assemble the D5 dead-capital lots from open CONVICTION positions: the time-stop clock
    (time_stop_by), the realized rel-return since entry (current px vs stored entry px), and the RS
    gap to the leading sector (the sector-pctile gap is a robust proxy — the engine carries no
    stable per-name RS pctile). detectors.d5_dead_capital then flags any lot that is elapsed AND
    unresolved AND lagging. (Inputs were never populated before, so D5 could never fire.)"""
    from datetime import date as _date
    lots: list[dict] = []
    for p in open_positions:
        if p.get("sleeve") != "conviction":
            continue
        tsb = p.get("time_stop_by")
        if not tsb:
            continue
        try:
            tsb_d = _date.fromisoformat(tsb) if isinstance(tsb, str) else tsb
        except Exception:
            continue
        t = p["ticker"]
        entry, cur = p.get("entry_price"), prices.get(t)
        rel = (cur / entry - 1.0) if (entry and cur and float(entry) > 0) else 0.0
        sp = sector_pctile.get(t)
        gap = (max(0.0, (leader_pctile - sp) / 100.0)
               if (sp is not None and leader_pctile is not None) else 0.0)
        lots.append({"ticker": t, "id": p.get("thesis_id"), "time_stop_by": tsb_d,
                     "rel_return_since_entry": round(rel, 4), "rs_leader_gap": round(gap, 4)})
    return lots


def run(asof: str | None = None, force: bool = False, research: bool = False,
        *, directive: str | None = None,
        stale_freeze: dict | None = None) -> dict:
    """Run one Flagship build.

    ``directive`` is an optional overnight reconsideration instruction (e.g. from the
    overnight watch loop).  When present it acts as ``force=True`` for the build gate
    (the overnight tape already passed the materiality check before the caller invoked
    us) and is threaded into the judgment layer if armed so the PM sees the live tape
    context.  ``directive=None`` is byte-identical to today — the flag-off invariant
    is preserved: if ``MASTERMIND_FLAGSHIP_JUDGMENT`` is off the directive has no
    effect on the book (the deterministic engine path rebuilds with fresh regime /
    severity inputs, which is itself valuable).

    ``stale_freeze`` is an optional dict from the caller (``bot/daily.py``) carrying
    the result of ``macro_refresh.refresh_and_check()``.  When ``stale_freeze['freeze']``
    is True AND ``MASTERMIND_STALE_FREEZE`` is enabled, the book is frozen to the prior
    published state (no new adds, no weight increases) BEFORE ledger/store/rebalance/
    publish — i.e. at the correct seam.  ``stale_freeze=None`` is a byte-identical no-op
    (the default; preserves backward compatibility for all non-daily callers).

    FLAGSHIP-ONLY: the autonomous/etf/heavyweight books read the same macro artifacts but
    their stale-anchor-freeze wiring is a separate reviewed change.  ``stale_freeze`` must
    NEVER be passed for those books from this function."""
    # —— open run log ——
    _run_id: str | None = None
    try:
        from brain import runlog
        _run_id = runlog.start_run("book", title="phase2 book build")
        _rl_log(_run_id, "book_step", "phase2 start",
                f"asof={asof} force={force} research={research} "
                f"directive={'<set>' if directive else None}")
    except Exception:
        pass

    cfg = load_doctrine()
    regime = _j("data/regime/latest.json")
    asof = asof or regime["date"]
    secrs = sorted(regime["sector_rs"], key=lambda r: r["rank"])
    top_sector = secrs[0]["ticker"]

    _rl_log(_run_id, "book_step", "regime read",
            f"quad={regime.get('quad')} quad_name={regime.get('quad_name')} "
            f"top_sector={top_sector} liquidity={regime.get('liquidity_overlay')}",
            quad=regime.get("quad"), quad_name=regime.get("quad_name"),
            top_sector=top_sector)

    # —— W-NW.1: Neural Web context perception — flag-independent read + audit row ——
    # Fetches context once per run regardless of MASTERMIND_NW_CONTEXT flag (dark-ship §1.7):
    # the reader and audit rows always accrue; only prompt/plane injection is flag-gated below.
    _nw_plane: dict = {}
    try:
        from brain import neural_web_context as _nwc_mod
        _nw_audit = _nwc_mod.audit_row()
        _rl_log(_run_id, "perception", "nw_context",
                f"status={_nw_audit.get('status')} asof={_nw_audit.get('asof')} "
                f"age_days={_nw_audit.get('age_days')} n_candidates={_nw_audit.get('n_candidates')} "
                f"fresh_lobes={_nw_audit.get('fresh_lobes')} "
                f"stale_lobes={_nw_audit.get('stale_lobes')} "
                f"market_lobe_stale={_nw_audit.get('market_lobe_stale')}",
                **_nw_audit)
        # W-M: append to persistent nw_context_audit.jsonl sidecar (FB-R11, FB-R3).
        # Delegates to _append_nw_context_audit (MAJOR-3) so the logic is unit-testable.
        # Never-raise wrapper kept at the call site per docstring contract.
        try:
            _append_nw_context_audit(
                Path(__file__).resolve().parent.parent,
                _run_id,
                _nw_audit,
            )
        except Exception:
            pass  # best-effort append; never raise
        if _nwc_mod.nw_prompts_enabled():
            _nw_plane = _nwc_mod.market_plane()
    except Exception:
        _rl_log(_run_id, "perception", "nw_context", "nw_context unavailable")

    # —— W-TSY.1: Treasury/TGA liquidity context — flag-independent read + audit row ——
    # Mirrors the W-NW.1 block above: observability always accrues regardless of the
    # MASTERMIND_TREASURY_CONTEXT flag (dark-ship); only prompt/payload injection
    # (pm_conviction / strategist) is flag-gated.
    try:
        from brain import treasury_context as _tsy_mod
        _tsy_audit = _tsy_mod.audit_row()
        _rl_log(_run_id, "perception", "treasury_context",
                f"status={_tsy_audit.get('status')} asof={_tsy_audit.get('asof')} "
                f"age_days={_tsy_audit.get('age_days')} n_events={_tsy_audit.get('n_events')} "
                f"headline_state={_tsy_audit.get('headline_state')}",
                **_tsy_audit)
        # Append to the persistent treasury_context_audit.jsonl sidecar.
        # Delegates to _append_treasury_context_audit so the logic is unit-testable.
        # Never-raise wrapper kept at the call site per docstring contract.
        try:
            _append_treasury_context_audit(
                Path(__file__).resolve().parent.parent,
                _run_id,
                _tsy_audit,
            )
        except Exception:
            pass  # best-effort append; never raise
    except Exception:
        _rl_log(_run_id, "perception", "treasury_context", "treasury_context unavailable")

    # —— E0.5: perception runlog step — P5: perception logged BEFORE any position decision ——
    # Assemble + PUBLISH THE one market view (P7: data/market_view/latest.json, all books read the
    # one artifact) before the book is touched.  Read-only enrichment — the wave contract is ZERO
    # behaviour change: nothing downstream sizes off this in W-E.0.  Lazy import so this degrades
    # gracefully if brain/market_view.py (E0.3) is absent; any failure logs "unavailable" and the
    # build proceeds unchanged (a perception organ never blocks the book — masterplan §4).
    try:
        from brain import market_view as _mv_mod
        _mv_inputs = _market_view_inputs(regime, asof)
        # Pass neural_web_out only when flag is ON (dark-ship: OFF → build call byte-identical)
        _nw_out_for_build = _nw_plane if _nw_plane else None
        _pv = _mv_mod.build(
            "us",
            write=True,
            distribution_tells_out=_mv_inputs.get("distribution_tells"),
            liquidity_quality_out=_mv_inputs.get("liquidity_quality"),
            regime_nowcast_out=_mv_inputs.get("regime_nowcast"),
            neural_web_out=_nw_out_for_build,
        )
        # Publish the typed AI-facing contract beside market_view.v1.  This is read-only and
        # preserves every existing authority boundary.
        try:
            from brain import decision_context as _dc

            _dc.build(
                "us",
                regime=regime,
                market_view=_pv,
                neural_web=_nwc_mod.context() if "_nwc_mod" in locals() else {},
                write=True,
                seq=int(_pv.get("seq") or 0),
            )
        except Exception:
            pass
        _pv_brief = (_pv.get("brief") or {}).get("wheres_the_risk", "")
        _pv_conflict = (_pv.get("label_vs_planes") or {}).get("conflict", False)
        _pv_coverage = (_pv.get("assembly") or {}).get("fresh", "?")
        _rl_log(_run_id, "perception", "market_view",
                f"conflict={_pv_conflict} coverage_fresh={_pv_coverage} | {_pv_brief}",
                conflict=_pv_conflict,
                coverage_fresh=_pv_coverage,
                label_vs_planes=_pv.get("label_vs_planes"),
                posture_floor_defense=_pv.get("posture_floor_defense"),
                net_posture_tilt=_pv.get("net_posture_tilt"))
    except Exception:
        _rl_log(_run_id, "perception", "market_view", "perception unavailable")

    con = store.connect()
    sig = gate.state_signature(regime, top_sector)
    # A directive (overnight reconsideration) acts as force=True: the overnight watch loop already
    # verified the tape is material before invoking us, so we should always rebuild.
    _effective_force = force or bool(directive)
    decision = gate.should_run(sig, store.last_run(con),
                               interval_days=int(cfg["scorecard"].get("rebuild_interval_days", 1)),
                               force=_effective_force, asof=asof)
    if not decision["run"]:
        # HARD-EXIT SWEEP — the run gate is keyed on the REGIME signature (quad/risk-band/liquidity/
        # top-sector) and is blind to NAME-level breakdowns. So even on a carried-forward day, sweep
        # the held conviction names and exit any that have gone parabolic / into Altman distress /
        # into a confirmed structural downtrend — otherwise a broken name sits in the book until the
        # regime signature happens to move.
        _swept: list[str] = []
        for _hp in position_log.open_positions():
            if _hp.get("sleeve") != "conviction":
                continue
            try:
                _syn = lenses_mod.full(_hp["ticker"], "name").get("synthesis", {})
            except Exception:
                continue
            if _is_hard_exit(_syn) and position_log.close_position("conviction", _hp["ticker"], asof,
                                                                   reason="hard_exit_sweep",
                                                                   reason_code="hard_veto"):
                _swept.append(_hp["ticker"])
                try:
                    ledger.close(_hp["ticker"], "exited (hard veto/downtrend, gate-closed sweep)")
                except Exception:
                    pass
        if _swept:
            _rl_log(_run_id, "decision", "hard-exit sweep (gate closed)", f"exited={_swept}", exited=_swept)
        store.record_run(con, asof, False,
                         decision["triggers"] + (["hard_exit_sweep"] if _swept else []),
                         sig, datetime.now(timezone.utc).isoformat())
        _rl_log(_run_id, "decision", "gate: carried forward",
                f"triggers={decision['triggers']} swept={_swept}")
        try:
            from brain import runlog
            if _run_id:
                runlog.end_run(_run_id, summary=f"carried forward — no material change"
                               + (f"; hard-exit swept {_swept}" if _swept else ""))
        except Exception:
            pass
        # mark the paper-account NAV even on a carried day (read-only snapshot of held positions at
        # current prices — no trades) so the equity curve advances daily, not only on rebuild days.
        try:
            from portfolio import paper_account as _pa_mark
            _open_tk = {hp["ticker"] for hp in position_log.open_positions()}
            _mp = {}
            for _t in _open_tk | {"SPY"}:
                _px = _pa_mark._current_price(_t)
                if _px and _px > 0:
                    _mp[_t] = _px
            if _mp:
                _pa_mark.mark(_mp, asof)
        except Exception:
            pass
        return {"ran": False, "reason": "carried forward (no material change)",
                "exited": _swept, "positions": store.positions(con, asof), "run_id": _run_id}

    # ———— ARMED Claude research (optional) ————
    research_out = None
    if research:
        from brain import research_desk
        research_out = research_desk.daily_research_and_ingest(asof)

    # ———— LEADERSHIP sleeve ————
    # W2.4 — the leadership BUDGET comes from the ONE budget equation (regime_frame.budget()), not the
    # hardwired midpoint (which ignored regime quality entirely). confidence × transition × flip-margin
    # is consumed EXACTLY ONCE, here; a missing/stale frame degrades to the 0.50 midpoint = today's
    # behaviour (invariant: missing data can only shrink toward neutral, never inflate to the ceiling).
    try:
        from brain import regime_frame as _rf
        _budget_out = _rf.budget("us")
        lead_budget = float(_budget_out["lead_budget"])
        _budget_inputs = _budget_out.get("inputs") or {}
    except Exception:  # noqa: BLE001 — degrade to the doctrine midpoint on any budget-read failure
        _budget_out = None
        _budget_inputs = {}
        lead_budget = sum(cfg["sleeves"]["leadership_target"]) / 2
    _rl_log(_run_id, "book_step", "leadership budget (one equation)",
            f"lead_budget={lead_budget:.4f} confidence={_budget_inputs.get('confidence')} "
            f"transition={_budget_inputs.get('transition_state')} "
            f"flip_margin={_budget_inputs.get('flip_margin')} "
            f"T={_budget_inputs.get('T')} F={_budget_inputs.get('F')}",
            lead_budget=round(lead_budget, 4),
            confidence=_budget_inputs.get("confidence"),
            transition_state=_budget_inputs.get("transition_state"),
            flip_margin=_budget_inputs.get("flip_margin"),
            T=_budget_inputs.get("T"), F=_budget_inputs.get("F"))

    # ———— W-I task 6: ROTATION-EVIDENCE damp (the incident's DETECTION fix) ————
    # Count the disagreement sources that AGREE — {nowcast doubt, liquidity stress/hollow, radar
    # caution, defensive-RS crossover} — and RE-derive the leadership budget with a shrink-only damp
    # (2 agree ×0.9, 3+ ×0.8). SHRINK-ONLY: it can only push lead_budget DOWN toward the 0.40 floor.
    # Every source degrades to ABSENT (non-agreeing) on missing data — on a calm tape n_agree=0, the
    # damp is 1.0, and the budget is byte-identical. The same evidence lifts the DEF_SLEEVE below.
    # (The nowcast is ADVISORY-only per nowcast_validation.md — here it sizes a shrink, not a wired
    # budget-return predictor, which the failed gate explicitly permits.)
    _rotation_evidence = None
    try:
        from brain import regime_frame as _rf_ev
        _regime_label = _rf_ev.frame("us")
        _ev_nowcast = _rf_ev._nowcast_doubt_source(
            quad=_regime_label.get("quad"), quad_name=_regime_label.get("quad_name"))
        _ev_liq = _rf_ev._liquidity_stress_source()
        _ev_radar = _rf_ev._radar_caution_source()
        _ev_rs = _rf_ev._defensive_rs_source()
        _rotation_evidence = _rf_ev.rotation_evidence(
            nowcast_doubt=_ev_nowcast, liquidity_stress=_ev_liq,
            radar_caution=_ev_radar, defensive_rs_cross=_ev_rs)
        _budget_out2 = _rf_ev.budget("us", evidence=_rotation_evidence)
        lead_budget = float(_budget_out2["lead_budget"])
        _budget_inputs = _budget_out2.get("inputs") or _budget_inputs
        _rl_log(_run_id, "book_step", "rotation-evidence damp",
                f"n_agree={_rotation_evidence['n_agree']} D={_budget_inputs.get('D')} "
                f"lead_budget={lead_budget:.4f} sources={_rotation_evidence['sources']}",
                lead_budget=round(lead_budget, 4),
                evidence_n_agree=_rotation_evidence["n_agree"],
                evidence_sources=_rotation_evidence["sources"],
                D=_budget_inputs.get("D"))
    except Exception as _e:  # noqa: BLE001 — evidence is additive; a failure degrades to the un-damped budget
        _rotation_evidence = None
        _rl_log(_run_id, "decision", "rotation-evidence error", f"{_e!r}"[:160])

    # ── E2.2 SUBSUMPTION — the posture read (ONE per build; every consumer below shares it).
    # Flag OFF ⇒ _posture is None and every seam below is byte-identical (the E3 control arm).
    # Note lead_budget needs NO seam here: regime_frame.budget() above IS the shim when armed
    # (single consumption — the runlog's posture_delegated input carries the provenance).
    _posture = None
    try:
        from brain import posture_decider as _pd
        if _pd.posture_flag():
            _posture = _pd.latest()
            if not (isinstance(_posture, dict) and _posture.get("offense_budget") is not None):
                _posture = _pd.decide()
            _rl_log(_run_id, "book_step", "posture (ARMED)",
                    f"class={_posture.get('posture_class')} offense={_posture.get('offense_budget')} "
                    f"floor={_posture.get('defense_floor')} notch={_posture.get('posture_notch_cap')} "
                    f"provenance={_posture.get('shrink_provenance')}",
                    posture_class=_posture.get("posture_class"),
                    shrink_provenance=_posture.get("shrink_provenance"))
    except Exception:  # noqa: BLE001 — P2: posture failure never blocks the build
        _posture = None

    # ── leadership selection (top-RS, trend-gated) + UNIVERSE-TRIAGE suppression (flag-gated) ──
    # _leaders_pre is the ORIGINAL selection. lw is ALWAYS computed on len(_leaders_pre) so that when
    # the triage brake drops a NEW (non-held) 'reduce' sector, the freed budget flows to CASH and NO
    # surviving leg is up-weighted (invariant ii). Flag OFF ⇒ _reduce_secs empty ⇒ leaders is
    # _leaders_pre (SAME object) and lw is identical to today ⇒ BYTE-IDENTICAL (invariant i).
    _leaders_pre = [s for s in secrs[:6] if s.get("above_200d_trend")][:4]
    lw = round(lead_budget / max(1, len(_leaders_pre)), 4)
    _reduce_secs: set[str] = set()
    if _universe_triage_enabled():
        try:
            from brain import universe_triage as _ut
            # HELD leadership tickers are EXEMPT (never suppressed) — a name we already carry rides
            # through a softened sector; only a genuinely NEW leadership add can be triaged out.
            _held_lead_pre = {hp["ticker"].upper() for hp in position_log.open_positions()
                              if hp.get("sleeve") == "leadership"}
            leaders, _reduce_secs = _suppress_reduce_sectors(
                _leaders_pre, _held_lead_pre, True, _ut.sector_action)
        except Exception as _e:  # noqa: BLE001 — fail-soft: no suppression on any error
            leaders, _reduce_secs = _leaders_pre, set()
            _rl_log(_run_id, "decision", "leadership triage error", f"{_e!r}"[:160])
    else:
        leaders = _leaders_pre
    if _reduce_secs:
        # freed budget (len(_leaders_pre) − len(leaders) legs × lw) is NOT redistributed — it lands in
        # cash (gross = Σ book weights; cash = 1 − gross). Observability line for the suppression event.
        _rl_log(_run_id, "decision", "leadership triage-suppressed",
                f"dropped={sorted(_reduce_secs)} kept={[s['ticker'] for s in leaders]} "
                f"weight_each={lw} freed_to_cash={round(lw * len(_reduce_secs), 4)}",
                sleeve="leadership")

    # ── W8 §2.6 LEADERSHIP SECOND PASS (entry + context, subtract-only, held-exempt) ─────────────
    # Top-RS selection is backward-looking, so the mechanical sleeve buys the MOST-extended leaders
    # at their tops (2026-07-14: SMH with its origin complex broken, XLK downstream of it, MTUM
    # mid-unwind, QUAL rolling over). Each genuinely NEW leadership leg must now also clear the
    # ENTRY engine (buyable / unknown) and the CONTEXT gate (not blocked). A failed leg is DROPPED
    # — `lw` stays computed on len(_leaders_pre) so the freed budget lands in CASH (the exact
    # triage-brake invariant), the ETF is PARKED on the watchlist with promotion triggers, and a
    # runlog row says why. HELD legs are exempt (never suppressed — entry is an entry brake).
    # Fail-open: an assessor error keeps the leg (`MASTERMIND_ENTRY_GATE` off ⇒ block inert).
    try:
        from portfolio import conviction as _conv_flag  # _entry_gate_enabled lives there
        if _conv_flag._entry_gate_enabled() and leaders:
            from portfolio import context_gate as _ctxg
            from portfolio import entry_engine as _eeng
            from portfolio import watchlist as _wl8
            try:
                _held_lead_w8 = {hp["ticker"].upper() for hp in position_log.open_positions()
                                 if hp.get("sleeve") == "leadership"}
            except Exception:  # noqa: BLE001
                _held_lead_w8 = set()
            _kept_leaders = []
            for s in leaders:
                _lt = (s.get("ticker") or "").upper()
                if not _lt or _lt in _held_lead_w8:
                    _kept_leaders.append(s)
                    continue
                try:
                    _erep = _eeng.assess(_lt, as_of=asof)
                    _crep = _ctxg.assess(_lt, entry_verdict=_erep.get("verdict"),
                                         is_etf=True, as_of=asof)
                except Exception:  # noqa: BLE001 — fail-open: keep the leg on assessor error
                    _kept_leaders.append(s)
                    continue
                _ebad = (not _erep.get("buyable")) and _erep.get("verdict") != "unknown"
                _cbad = _crep.get("verdict") == "blocked"
                if _ebad or _cbad:
                    _wnotes = (_erep.get("notes") or []) + (_crep.get("reasons") or [])
                    _wreason = (f"leadership entry {_erep.get('verdict')}" if _ebad
                                else "leadership context blocked")
                    _wl8.append(_lt, asof, _wreason + (f" — {_wnotes[0]}" if _wnotes else ""),
                                tech=_entry_tech_fields(_lt), combined=None)
                    _rl_log(_run_id, "decision", f"leadership withheld {_lt}",
                            f"entry={_erep.get('verdict')} context={_crep.get('verdict')} "
                            f"freed_to_cash={lw} notes={'; '.join(_wnotes[:2])[:200]}",
                            ticker=_lt, sleeve="leadership")
                else:
                    _kept_leaders.append(s)
            leaders = _kept_leaders
    except Exception:  # noqa: BLE001 — the second pass is subtract-only; failure changes nothing
        pass
    book = []

    _rl_log(_run_id, "book_step", "leadership sleeve selected",
            f"n_leaders={len(leaders)} weight_each={lw} tickers={[s['ticker'] for s in leaders]}")

    for s in leaders:
        ldr_px = ((_j(f"site/stockdata/{s['ticker']}.json") or {}).get("tech", {}) or {}).get("price") \
            if (_V / f"site/stockdata/{s['ticker']}.json").exists() else None
        book.append({"ticker": s["ticker"], "theme_id": s["ticker"], "sleeve": "leadership",
                     "stage": 2, "weight": lw, "verdict": "hold", "rs_pctile": s["pctile_252d"],
                     "entry_price": ldr_px})
        _rl_log(_run_id, "trade", f"sized {s['ticker']} leadership",
                f"ticker={s['ticker']} weight={lw} rs_pctile={s['pctile_252d']} price={ldr_px}",
                ticker=s["ticker"], sleeve="leadership", weight=lw, verdict="hold")

    # ———— W2.1 LEADERSHIP CAPS (per-leg brake stack, subtract-only) ————
    # Apply the shared apply_leadership_caps() to the just-built leadership legs, RIGHT after they are
    # sized and BEFORE the conviction sleeve is appended: each leg's weight is scaled by
    # MIN(overextension clamp off etf_board.etf_trend pct_vs_200d — outage-independent of stockdata —,
    # cycle multiplier — late_cycle sector NEW legs halved). Freed weight goes to CASH (never
    # redistributed). Degrades to a no-op on missing extension/cycle data (today's behaviour).
    try:
        from portfolio import sleeves as _sleeves
        # held leadership tickers (authoritative for the cycle-halving exemption: a HELD leader in a
        # now-late-cycle sector is NEVER halved — the walk-forward refuted the held-sector veto). A leg
        # NOT in this set is a genuinely NEW leadership addition and IS eligible for the late_cycle brake.
        _held_lead = {hp["ticker"].upper() for hp in position_log.open_positions()
                      if hp.get("sleeve") == "leadership"}
        _lead_cap = _sleeves.apply_leadership_caps(book, held=_held_lead)
        if _lead_cap.get("brakes"):
            _rl_log(_run_id, "book_step", "leadership caps applied",
                    f"freed_to_cash={_lead_cap['freed_to_cash']} brakes={_lead_cap['brakes']}",
                    freed_to_cash=_lead_cap["freed_to_cash"])
            for _b in _lead_cap["brakes"]:
                _rl_log(_run_id, "decision", f"leadership cap {_b['ticker']}",
                        f"reason={_b['reason']} {_b['from']}→{_b['to']} "
                        f"ext_mult={_b['ext_mult']} cycle_mult={_b['cycle_mult']}",
                        ticker=_b["ticker"], sleeve="leadership")
    except Exception as _e:  # noqa: BLE001 — caps are subtract-only; a failure degrades to un-capped legs
        _rl_log(_run_id, "decision", "leadership caps error", f"{_e!r}"[:160])

    # ———— ROTATION-IN watchlist enrollment (flag-gated, default OFF, NON-DISRUPTIVE) ————
    # "Hold names through unconfirmed turns": each build, EARLY/TURNING (unconfirmed) rotation calls
    # PARK their member names on the Flagship watchlist STATE for future review/promotion. This block
    # does NOT touch `book`, sizing, or any trading state — it only writes watchlist_state rows
    # (append_rotation is idempotent per call_id and NEVER raises), so it is inherently non-disruptive.
    # Gate: enroll only when MASTERMIND_ROTATION_IN ∈ {watch, starter}; OFF (default) ⇒ this is a pure
    # no-op ⇒ the build is byte-identical to today. Wrapped in try/except (fail-soft).
    if _rotation_in_mode() in ("watch", "starter"):
        try:
            from brain import rotation_intake as _ri
            from portfolio import watchlist as _wl
            _enrolled = 0
            for _call in _ri.active_calls(asof) or []:
                if not isinstance(_call, dict):
                    continue
                _cstate = _call.get("state")
                if _cstate not in ("EARLY", "TURNING"):   # only UNCONFIRMED turns; terminal/unknown skip
                    continue
                _cid = str(_call.get("call_id") or _call.get("target") or "").strip()
                if not _cid:
                    continue
                _ctarget = _call.get("target")
                _cconf = _call.get("confidence")
                _cthesis = f"rotation_in {_cid} {_cstate}"
                for _m in _ri.expand(_call) or []:
                    if not isinstance(_m, dict):
                        continue
                    _mt = _m.get("ticker")
                    if not _mt:
                        continue
                    if _wl.append_rotation(_mt, asof, _cid, target=_ctarget, confidence=_cconf,
                                           thesis=_cthesis, trigger=_call.get("falsifier")):
                        _enrolled += 1
            if _enrolled:
                _rl_log(_run_id, "book_step", "rotation-in enrolled",
                        f"n_parked={_enrolled} mode={_rotation_in_mode()}")
        except Exception as _e:  # noqa: BLE001 — enrollment is additive; a failure never blocks the build
            _rl_log(_run_id, "decision", "rotation-in enroll error", f"{_e!r}"[:160])

    # ———— CONVICTION sleeve ————
    from portfolio import conviction
    conv_budget = sum(cfg["sleeves"]["conviction_target"]) / 2
    decisions = []
    _synth_map: dict[str, tuple[dict, list]] = {}
    # currently-held conviction names get sector-cap priority (hysteresis) so the book doesn't
    # churn a name in and out across builds (the NVDA in/out problem).
    _held_conv = {p["ticker"] for p in position_log.open_positions()
                  if p.get("sleeve") == "conviction"}
    # E2.2: posture conviction_appetite scales the conviction budget (charter P5 — posture before
    # position). Flag OFF / no posture ⇒ multiplier 1.0, byte-identical.
    if _posture is not None:
        try:
            _app = float(_posture.get("conviction_appetite") or 1.0)
            if 0.0 <= _app < 1.0:
                conv_budget = round(conv_budget * _app, 6)
        except Exception:  # noqa: BLE001
            pass
    # ── W8 §2.8 CORE-PATH WATCHLIST RE-REVIEW (was judgment-flag-gated → parked names never
    # promoted on the default path). Runs every build: the composed predicate re-checks the L3
    # timing fields AND the entry engine AND the context gate, so a parked name promotes the
    # moment its actual withhold reason clears (tier flips fresh, range retreats, rollover ends,
    # contagion clears, unwind ends). Promoted names re-enter ONLY as candidates — the full gate
    # still decides. review() is idempotent per (ticker, asof) so the judgment layer's own later
    # review (when armed) cannot double-age. Best-effort; never breaks the build.
    _w8_promoted: list[str] = []
    try:
        from portfolio import watchlist as _wl_core
        if conviction._entry_gate_enabled():
            from portfolio import context_gate as _ctxg_core
            from portfolio import entry_engine as _eeng_core

            def _w8_still_withheld(_t: str):
                try:
                    _r = _wl_core.timing_withhold(_entry_tech_fields(_t))
                    if _r:
                        return _r
                    _r = _eeng_core.still_withheld_reason(_t)
                    if _r:
                        return _r
                    return _ctxg_core.still_blocked_reason(_t)
                except Exception:  # noqa: BLE001 — predicate failure keeps the name parked
                    return "re-check unavailable"

            _wl_core.review(asof, still_withheld=_w8_still_withheld)
            _w8_promoted = [c["ticker"] for c in _wl_core.promote_candidates(asof)
                            if c.get("ticker")]
            if _w8_promoted:
                _rl_log(_run_id, "decision", "watchlist promotions",
                        f"cleared_for_reentry={_w8_promoted}", sleeve="conviction")
    except Exception:  # noqa: BLE001 — the re-review is additive; never break the build
        _w8_promoted = []

    _build_result = conviction.build(conv_budget, name_cap=cfg["caps"]["name_cap"], held=_held_conv,
                                     asof=asof, extra_candidates=_w8_promoted)
    # conviction.build returns (sized_list, rejected_list) as a tuple
    if isinstance(_build_result, tuple) and len(_build_result) == 2:
        sized, _rejected = _build_result
    else:
        # legacy: single list returned
        sized = list(_build_result)
        _rejected = []

    # ── W8 PARK ENROLLMENT: conviction rejections carrying a `park` record (entry/context) go on
    # the watchlist TIMING lane; `rotation_candidate` rows (base_turn below the confluence bar) go
    # on the ROTATION lane (WATCH → ARMED as confirmation builds). Both idempotent; never raise.
    try:
        from portfolio import watchlist as _wl_park
        for _rj in _rejected:
            _pt = (_rj.get("ticker") or "").upper()
            if not _pt:
                continue
            if _rj.get("park"):
                _wl_park.append(_pt, asof, _rj.get("reason") or "parked (entry/context)",
                                tech=_entry_tech_fields(_pt), combined=None)
                _rl_log(_run_id, "decision", f"PARKED {_pt}",
                        f"reason={_rj.get('reason')}", ticker=_pt, sleeve="conviction")
            elif _rj.get("rotation_candidate"):
                _er = _rj.get("entry_report") or {}
                _wl_park.append_rotation(
                    _pt, asof, f"w8-base-turn-{_pt}",
                    confidence=float(_er.get("entry_score") or 50.0) / 100.0,
                    thesis=f"base turning (entry {_er.get('verdict')}, "
                           f"confluence {_rj.get('confluence')})",
                    trigger={"advance_on": "tier T1/T2 fresh + confluence > 0.30"})
                _rl_log(_run_id, "decision", f"ROTATION-PARKED {_pt}",
                        f"entry_score={_er.get('entry_score')} "
                        f"confluence={_rj.get('confluence')}", ticker=_pt, sleeve="conviction")
    except Exception:  # noqa: BLE001 — enrollment is additive; never break the build
        pass


    _rl_log(_run_id, "book_step", "conviction gate evaluated",
            f"candidates_sized={len(sized)} rejected={len(_rejected)} budget={conv_budget}")

    for _rej in _rejected:
        _rl_log(_run_id, "decision", f"VETOED {_rej['ticker']}",
                f"vetoes={_rej['vetoes']} bear={_rej['bear']} confluence={_rej['confluence']}",
                ticker=_rej["ticker"], vetoes=_rej["vetoes"],
                bear=_rej["bear"], confluence=_rej["confluence"])

    # ———— RESEARCH GATE — a full holistic paper must CONFIRM each buy ————
    # Before a name the engine wants can be bought, the Research Desk writes a complete
    # holistic report (thesis / valuation / fundamentals / revenue / catalysts / forward
    # earnings), re-digests it into a research_score, and combines that with the engine
    # buy-score. Only a combined "Conviction Index" over the bar lets the buy through — and
    # that combined score also scales the size (within the per-name cap). A NEW name gets a
    # full armed-Claude report; a carried name reuses its stored paper; the deterministic
    # engine-only report is the offline/CI fallback. Hard vetoes were already dropped by
    # conviction.build — the research gate can confirm / size / hold, but never rescue.
    from brain import research_paper as rpaper
    name_cap = cfg["caps"]["name_cap"]
    _open_conv = {p["ticker"] for p in position_log.open_positions()
                  if p.get("sleeve") == "conviction"}
    _armed_ok = rpaper.llm_enabled()
    gate_info: dict[str, dict] = {}          # ticker -> {full, paper, breakdown, research_block}
    confirmed_sized: list[dict] = []
    research_held: list[dict] = []
    _explored: list[dict] = []               # borderline rejects ε-EXPLORE-bought this run (#2 OPE)
    # ── shadow-book decision inputs: one self-contained record per evaluated candidate so the
    #    forward shadow books (portfolio.shadow_books) can replay TODAY under counterfactual policies
    #    (committee on/off, calibration on/off, alt sizing) WITHOUT re-running any LLM — purely from
    #    these stored inputs. Captures the full SENTINEL verdict (raw + de-confidenced confidence) so
    #    the no-calibration policy can re-derive NEXUS deterministically. Best-effort; never blocks.
    _shadow_inputs: list[dict] = []

    def _emit_shadow(ticker, c, breakdown, *, forge_confirmed, weight_prod,
                     committee_block, sentinel, price, is_new):
        sm = breakdown.get("size_mult") or 1.0
        wf = round(min(name_cap, (c.get("weight") or 0.0) * sm), 4) if forge_confirmed else 0.0
        _tech = _entry_tech_fields(ticker)
        _row: dict = {
            "ticker": ticker, "confluence": c.get("confluence"),
            "is_new": bool(is_new), "retained": bool(c.get("retained")),
            "forge_confirmed": bool(forge_confirmed),
            "engine_score": breakdown.get("engine_score"),
            "research_score": breakdown.get("research_score"),
            "combined": breakdown.get("combined"), "viability": breakdown.get("viability"),
            "size_mult": breakdown.get("size_mult"), "base_weight": c.get("weight"),
            "name_cap": name_cap, "weight_forge": wf,
            "weight_prod": round(float(weight_prod or 0.0), 4),
            "committee": committee_block, "sentinel": sentinel, "price": price,
            "raw_prob_correct": round(0.55 + min(0.15, (c.get("confluence") or 0.0) * 0.4), 2),
            "horizon_d": 21, "thesis_id": f"{asof}-{ticker}-conv",
            # entry-technical fields for the L3 timing lever (all nullable / defensive)
            "extension": _tech["pct_vs_200dma"], "pct_vs_200dma": _tech["pct_vs_200dma"],
            "rs": _tech["rs"], "urgency": _tech["urgency"],
            "eq_grade": _tech["eq_grade"], "parabolic": _tech["parabolic"],
        }
        # W-NW.1: optional per-candidate NW context (absent-safe — shadow_books.py consumers
        # use .get() on specific fields; unknown keys are ignored at replay time)
        try:
            from brain import neural_web_context as _nwc_mod  # lazy; never raises
            _nw_cand = _nwc_mod.candidate(ticker)
            if _nw_cand:
                _row["nw_context"] = _nw_cand
        except Exception:  # noqa: BLE001 — never break shadow emit
            pass
        _shadow_inputs.append(_row)

    def _maybe_explore(ticker, c, breakdown, research_block, stage, reason, *,
                       committee_block=None, sentinel=None, price=None, is_new=True):
        """ε-EXPLORATION (#2, flag-gated OFF): with probability ε, BUY a BORDERLINE reject (a committee
        drop / timing withhold — both already cleared conviction+research-confirm) at a floor weight
        instead of dropping it, so its forward outcome makes the gate's off-policy value estimable. The
        draw is DETERMINISTIC per (ticker, asof) so phase2 reruns are idempotent. Returns True iff the
        name was promoted to a buy (caller should `continue`). Inert (always False) unless
        MASTERMIND_SELECTION_EXPLORE is armed — so the live buy path is byte-identical by default. Never
        raises into the gate."""
        try:
            from portfolio import rejections as _rej_mod
            if not _rej_mod.explore_buy(ticker, asof, stage):
                return False
            ew = _rej_mod._explore_weight()
            confirmed_sized.append({**c, "weight": ew, "research": research_block,
                                    "explored": True, "explore_stage": stage})
            _explored.append({"ticker": ticker, "stage": stage, "reason": "explore: " + (reason or ""),
                              "combined": breakdown.get("combined"), "confluence": c.get("confluence")})
            _emit_shadow(ticker, c, breakdown, forge_confirmed=True, weight_prod=ew,
                         committee_block=committee_block, sentinel=sentinel, price=price, is_new=is_new)
            _rl_log(_run_id, "decision", f"EXPLORE BUY {ticker}",
                    f"{stage} explored at {ew} (eps); {reason}", ticker=ticker)
            return True
        except Exception:  # noqa: BLE001 — exploration is additive; never break the gate
            return False

    # ── DESK-QUORUM SEATS (once-per-build, memoized) — MASTERMIND_DESK_QUORUM off|shadow|enforce ──
    #    The other three conjunctive-quorum seats (MACRO STRATEGIST, PM-CONVICTION, PM-GATE OFFICER)
    #    join the entry-timing TECHNICIAN in the live buy funnel (Ch02 §2.4). They are top-down /
    #    whole-book seats, so each is run at most ONCE per build and its verdict looked up PER NAME
    #    (subtract-only). ZERO cost when the mode is "off" (the default): _desk_quorum_mode is read
    #    once here and every _seat_* helper short-circuits on "off" → nothing is invoked, the buy
    #    path stays byte-identical. A seat is invoked ONLY in shadow/enforce AND only when its own
    #    enabled()/LLM is reachable; a seat that CANNOT run (no LLM / infra off) yields a None verdict
    #    (pass-through, §2.5 row 7 "no LLM → engine decision stands"), while a seat that IS enabled
    #    but ERRORS yields its fail-conservative withhold value in ENFORCE (never a spurious approve)
    #    and None in shadow (never changes the book). Memoized in single-element caches so the loop
    #    can ask per-name without re-invoking the model.
    _dqm = "off"
    try:
        from brain import committee as _committee_mode
        _dqm = _committee_mode.desk_quorum_mode()
    except Exception:  # noqa: BLE001 — mode read never blocks the gate
        _dqm = "off"

    _strategist_cache: dict = {}
    _gate_cache: dict = {}
    _pm_cache: dict = {}

    def _seat_strategist_verdict() -> str | None:
        """MACRO STRATEGIST backdrop verdict for THIS build (name-independent, §2.5 row 1). Returns
        'hostile' when the top-down backdrop is risk_off (hard withhold), else 'neutral'/'supportive'
        (pass). None → seat not run (no LLM / off) OR ran-but-failed-in-shadow → committee no-op.
        In ENFORCE a genuine seat FAILURE returns None too (see caller) — the Strategist is a
        REQUIRED sign-off, but its withhold value is only synthesised by the caller in enforce."""
        if _dqm == "off":
            return None
        if "v" in _strategist_cache:
            return _strategist_cache["v"]
        v = None
        try:
            from brain import client as _client, strategist as _strategist
            if _strategist.enabled() and _client.available():
                _sv = _strategist.strategist_assess(asof, regime)
                if _sv is None:                       # seat enabled but produced nothing → error path
                    _strategist_cache["failed"] = True
                else:
                    stance = str(_sv.get("backdrop_stance") or "").strip().lower()
                    v = "hostile" if stance == "risk_off" else "neutral"
        except Exception:  # noqa: BLE001 — seat is additive; never block the gate
            _strategist_cache["failed"] = True
            v = None
        _strategist_cache["v"] = v
        return v

    def _seat_gate_decisions() -> dict:
        """PM-GATE OFFICER decisions for THIS build, keyed by ticker (whole-book pass, §2.5 rows 4/5).
        Runs gate_assess ONCE over the confirmed-candidate book. Returns {} when the seat is off / no
        LLM / fails → every name's gate verdict is None (pass-through). Each entry is the normalised
        {action: approve|veto|withhold|trim, scale} the caller maps to a nexus gate verdict."""
        if _dqm == "off":
            return {}
        if "d" in _gate_cache:
            return _gate_cache["d"]
        out: dict = {}
        try:
            from brain import client as _client, gate_officer as _gate
            if _gate.enabled() and _client.available():
                book = [{"ticker": c.get("ticker"), "weight": c.get("weight"),
                         "confluence": c.get("confluence")} for c in sized if c.get("ticker")]
                res = _gate.gate_assess(book, asof, regime=regime,
                                        portfolio_ctx={"held_conviction": sorted(_open_conv)})
                for d in (res or {}).get("decisions") or []:
                    tk = str(d.get("ticker") or "").upper().strip()
                    if tk:
                        out[tk] = {"action": d.get("action"), "scale": d.get("scale")}
                _gate_cache["ran"] = True
        except Exception:  # noqa: BLE001 — seat is additive; never block the gate
            _gate_cache["failed"] = True
            out = {}
        _gate_cache["d"] = out
        return out

    def _seat_pm_champions() -> set | None:
        """PM-CONVICTION championed-ticker set for THIS build (§2.4 gate 5, §2.5 row 6: no champion →
        withhold). Runs the armed PM book builder ONCE. Returns a set of championed tickers, or None
        when the PM seat is off / no LLM / fails — None means 'PM did not run', which the caller reads
        as 'do not gate on the champion' (pass-through) in shadow and, in enforce, only withholds a
        name when the PM RAN and did NOT champion it (a failed/absent PM never nukes the whole book)."""
        if _dqm == "off":
            return None
        if "s" in _pm_cache:
            return _pm_cache["s"]
        champions = None
        try:
            from brain import client as _client, pm_conviction as _pm
            if _pm.enabled() and _client.available():
                book = _pm.build_book(sized, _rejected, regime=regime, asof=asof,
                                      strategist=None, gate_info=gate_info,
                                      portfolio_ctx={"held_conviction": sorted(_open_conv)})
                if book and book.get("ran"):
                    champions = {str(h.get("ticker") or "").upper().strip()
                                 for h in (book.get("holdings") or []) if h.get("ticker")}
                else:
                    _pm_cache["failed"] = True     # enabled but no submission → error/no-op path
        except Exception:  # noqa: BLE001 — seat is additive; never block the gate
            _pm_cache["failed"] = True
            champions = None
        _pm_cache["s"] = champions
        return champions

    for c in sized:
        t = c["ticker"]
        try:
            _full = lenses_mod.full(t, "name")
            _syn = _full["synthesis"]
            _rows = _full["rows"]
        except Exception:
            _full, _syn, _rows = {}, {}, []
        px = ((_j(f"site/stockdata/{t}.json") or {}).get("tech", {}) or {}).get("price") \
            if (_V / f"site/stockdata/{t}.json").exists() else None
        is_new = t not in _open_conv
        paper = None if is_new else rpaper.latest_for(t)      # carried names reuse their paper
        paper_existed = paper is not None                     # a stored paper we are genuinely carrying
        if paper is None:
            # W8 §2.7: the paper receives the deterministic ENTRY/CONTEXT evidence + the Prophet
            # plan line so its quality read is weather-aware (sections 17/18 engage with them).
            _w8_prophet_line = None
            try:
                from portfolio import prophet_feed as _pfeed
                _w8_prophet_line = _pfeed.summary_line(t) or None
            except Exception:  # noqa: BLE001
                _w8_prophet_line = None
            paper = rpaper.generate(t, asof=asof, confluence=c["confluence"], rows=_rows,
                                    vetoes=_syn.get("vetoes", []), price=px, regime=regime,
                                    armed=(_armed_ok and is_new),
                                    entry_report=c.get("entry_report"),
                                    context_report=c.get("context_report"),
                                    prophet_line=_w8_prophet_line)
            rpaper.save_paper(paper)
            rpaper.write_feed_note(paper)
        # held-hysteresis (lower confirm bar) applies ONLY to a name we are CARRYING with its prior
        # paper — not to a held name whose paper we just regenerated this run (that's effectively new).
        breakdown = rpaper.score_breakdown(c["confluence"], paper, held=(not is_new and paper_existed))
        research_block = {
            "paper_id": paper["id"], "mode": paper["mode"],
            "engine_score": breakdown["engine_score"], "research_score": breakdown["research_score"],
            "combined": breakdown["combined"], "viability": breakdown["viability"],
            "confirmed": breakdown["confirmed"], "summary": paper.get("summary"),
            "price_at_review": paper.get("price_at_review"),
        }
        gate_info[t] = {"full": _full, "paper": paper, "breakdown": breakdown,
                        "research_block": research_block}
        if breakdown["confirmed"]:
            # ── L3 ENTRY-TIMING GATE (subtract-only, flag-gated, default OFF) ──
            # A NEW name that has cleared the gate + conviction + research confirm and would be BOUGHT
            # is WITHHELD when its entry technicals are poor (extended / weak-RS / 'avoid' / weak eq),
            # parked on the Flagship watchlist for daily re-review instead of being chased at a bad
            # entry. The predicate is IDENTICAL to the shadow lever (portfolio.watchlist.timing_withhold
            # mirrors portfolio.desk_ab.apply_timing_gated). Subtract-only: it can only DROP a would-be
            # buy (never add, never resize up) and ONLY a fresh add (a carried/held name is untouched).
            # When the flag is OFF this branch is inert → behavior is byte-identical to before.
            if is_new and _timing_gate_enabled():
                from portfolio import watchlist as _watchlist
                _tech = _entry_tech_fields(t)
                _twreason = _watchlist.timing_withhold(_tech)
                if _twreason:
                    # PARABOLIC withhold is NON-EXPLORABLE. ε-exploration is an intended off-policy
                    # channel that re-tries a timing-withheld name to make its forward value estimable
                    # — but a PARABOLIC entry is the one class the doctrine treats as a hard veto (a
                    # blow-off top you never chase), so it must NEVER be resurrected by the ε-draw.
                    # (This mirrors the extension hard veto: most parabolic names are already blocked
                    # upstream in lenses; this closes the residual grade='parabolic' / flag path where
                    # a name reaches the timing gate un-vetoed.) A non-parabolic withhold (extended /
                    # weak-RS / 'avoid' / weak-eq) is still explorable exactly as before.
                    if not _is_parabolic_withhold(_tech):
                        if _maybe_explore(t, c, breakdown, research_block, "timing_withhold", _twreason,
                                          price=px, is_new=is_new):
                            continue
                    research_held.append({"ticker": t, "reason": "timing withhold: " + _twreason,
                                          **research_block})
                    _emit_shadow(t, c, breakdown, forge_confirmed=True, weight_prod=0.0,
                                 committee_block=None, sentinel=None, price=px, is_new=is_new)
                    try:
                        _watchlist.append(t, asof, _twreason, tech=_tech,
                                          combined=breakdown.get("combined"))
                    except Exception:  # noqa: BLE001 — watchlist logging never blocks the gate
                        pass
                    _rl_log(_run_id, "decision", f"TIMING WITHHOLD {t}", _twreason, ticker=t)
                    continue
            # ── W8 §2.7 RESEARCH ENTRY DE-ESCALATION (LLM may only DE-escalate, never rescue) ──
            # The redigest's entry_agreement='disagree' on a NEW, deterministically-BUYABLE entry
            # downgrades it (apply_entry_deescalation), and the name takes the EXACT timing-withhold
            # path: parked with its paper's note, re-reviewed daily. An 'agree' can never upgrade a
            # blocked entry (that path never reaches here). Fail-soft: absent report/field → no-op.
            if is_new and c.get("entry_report"):
                try:
                    _de = rpaper.apply_entry_deescalation(c.get("entry_report"),
                                                          breakdown.get("entry_agreement"))
                except Exception:  # noqa: BLE001
                    _de = None
                if isinstance(_de, dict) and _de.get("deescalated"):
                    _dereason = ("research disagrees with entry — "
                                 + (str(breakdown.get("entry_note") or "no note"))[:160])
                    research_held.append({"ticker": t, "reason": _dereason, **research_block})
                    _emit_shadow(t, c, breakdown, forge_confirmed=True, weight_prod=0.0,
                                 committee_block=None, sentinel=None, price=px, is_new=is_new)
                    try:
                        from portfolio import watchlist as _watchlist_de
                        _watchlist_de.append(t, asof, _dereason, tech=_entry_tech_fields(t),
                                             combined=breakdown.get("combined"))
                    except Exception:  # noqa: BLE001
                        pass
                    _rl_log(_run_id, "decision", f"RESEARCH ENTRY DE-ESCALATION {t}",
                            _dereason, ticker=t)
                    continue
            scaled = round(min(name_cap, c["weight"] * breakdown["size_mult"]), 4)
            # ── blind adversarial committee (SENTINEL → NEXUS): a NEW buy FORGE confirmed gets an
            #    independent bear case; the committee can only DE-ESCALATE (trim/drop), never escalate.
            committee_block = None
            _sent = None
            if is_new:
                try:
                    from brain import committee as _committee
                    if _committee.enabled():
                        pctx = {"held_conviction": sorted(_open_conv), "n_held": len(_open_conv)}
                        cm = _committee.assess(t, asof, engine_full=_full, breakdown=breakdown,
                                               regime=regime, portfolio_ctx=pctx)
                        committee_block = {k: cm.get(k) for k in
                                           ("action", "scale", "lean", "rationale", "sentinel_stance")}
                        _sv = cm.get("sentinel") or {}
                        _sent = {k: _sv.get(k) for k in ("stance", "raw_confidence", "confidence")} \
                            if _sv else None
                        if cm.get("action") == "drop":
                            if _maybe_explore(t, c, breakdown, research_block, "committee_drop",
                                              cm.get("rationale", ""), committee_block=committee_block,
                                              sentinel=_sent, price=px, is_new=is_new):
                                continue
                            research_held.append({"ticker": t, "reason": "committee: " + cm.get("rationale", ""),
                                                  **research_block, "committee": committee_block})
                            _emit_shadow(t, c, breakdown, forge_confirmed=True, weight_prod=0.0,
                                         committee_block=committee_block, sentinel=_sent, price=px, is_new=is_new)
                            _rl_log(_run_id, "decision", f"COMMITTEE DROP {t}",
                                    f"sentinel={cm.get('sentinel_stance')} {cm.get('rationale')}", ticker=t)
                            continue
                        if cm.get("action") == "trim":
                            scaled = round(scaled * float(cm.get("scale", 1.0)), 4)
                except Exception:  # noqa: BLE001 — committee is additive; never block the gate
                    committee_block = None
            # ── DESK-QUORUM CONJUNCTIVE FUNNEL (MASTERMIND_DESK_QUORUM ladder off|shadow|enforce) ──
            #    The full four-seat quorum admits the entry-timing TECHNICIAN plus the MACRO STRATEGIST,
            #    PM-CONVICTION and PM-GATE OFFICER seats into the live buy gate (Ch02 §2.4). When the
            #    mode is "off" (the DEFAULT) the whole block is skipped up front on `_dqm != "off"`, so
            #    the gate is BYTE-IDENTICAL to base: NO seat is invoked (zero cost) and no ledger row is
            #    written. In "shadow"/"enforce" every wired seat's per-name verdict is collected and run
            #    through committee.nexus(...) — SUBTRACT-ONLY: the funnel can only park or scale a would-
            #    be buy down, never escalate. A pipeline-ledger row logging ALL stage verdicts is written;
            #    only in "enforce" is the synthesised action APPLIED (drop → park via the EXACT timing-
            #    withhold path; scale<1 → cap the size). Fail-soft: a seat that cannot run degrades to a
            #    None verdict (nexus no-op, §2.5 row 7 pass-through); a seat that IS armed but ERRORS
            #    degrades to its withhold value in ENFORCE (never a spurious approve) and None in shadow.
            if is_new and _dqm != "off":
                from brain import committee as _committee
                # TECHNICIAN — per-name entry-timing verdict (unchanged mechanism).
                _tech_verdict = None
                try:
                    from brain import technician as _technician
                    _tv = _technician.technician_assess(
                        _technician.technician_input(
                            t, entry_signal=_published_entry_signal(t),
                            tech=_entry_tech_fields(t)),
                        asof=asof, ticker=t)
                    _tech_verdict = (_tv or {}).get("verdict")
                except Exception:  # noqa: BLE001 — seat failure → None verdict (no-op, never parks)
                    _tech_verdict = None
                # STRATEGIST — top-down backdrop verdict (memoized once per build; name-independent).
                _strat_verdict = _seat_strategist_verdict()
                # PM-CONVICTION — champion verdict from the memoized championed-ticker set.
                _pm_champions = _seat_pm_champions()
                if _pm_champions is None:
                    # PM seat couldn't run (off / no-LLM / errored). In ENFORCE this is a REQUIRED
                    # positive sign-off that is MISSING → the conjunctive quorum is incomplete → withhold
                    # the NEW name (fail-closed, §2.4 gate 5 / §2.5 row 6-7). This gates new adds ONLY —
                    # it never touches a held name, so an LLM outage pauses new buys but can't nuke the
                    # book. In SHADOW it stays pass-through (None) and only logs, never changing the book.
                    _pm_verdict = "pass" if _dqm == "enforce" else None
                else:
                    _pm_verdict = "add" if t.upper() in _pm_champions else "pass"  # §2.5 row 6
                # GATE OFFICER — final ruling from the memoized whole-book decisions.
                _gate_dec = _seat_gate_decisions().get(t.upper())
                _gate_verdict = None
                _gate_downsize = None
                if _gate_dec is not None:
                    _gate_verdict = _gate_dec.get("action")
                    if _gate_verdict == "trim":
                        # a trim is an APPROVE-with-downsize (§2.5 row 5 smallest-size-wins): map the
                        # gate's absolute scale into apply_gate's approve+downsize_to cap.
                        _gate_downsize = _gate_dec.get("scale")
                        _gate_verdict = "approve"
                # FAIL-CONSERVATIVE (ENFORCE only): a REQUIRED sign-off that was armed but errored is
                # withheld, never approved (§1.5.1). In shadow a failure stays None (never changes the
                # book). The Strategist + Gate Officer are required positive sign-offs; the PM champion
                # gate already fails closed above (ran-but-not-championed → "pass") without nuking a book
                # the PM never ran. A seat that simply had no LLM is None on BOTH ladders (pass-through).
                if _dqm == "enforce":
                    if _strat_verdict is None and _strategist_cache.get("failed"):
                        _strat_verdict = "hostile"
                    if _gate_verdict is None and _gate_cache.get("failed"):
                        _gate_verdict = "withhold"
                # Build the seat-verdict dicts nexus consumes (its _seat_verdict reads these keys).
                _seat_kwargs = {
                    "technician": {"verdict": _tech_verdict} if _tech_verdict is not None else None,
                    "strategist": {"backdrop": _strat_verdict} if _strat_verdict is not None else None,
                    "pm_conviction": {"proposal": _pm_verdict} if _pm_verdict is not None else None,
                    "gate": ({"decision": _gate_verdict, "downsize_to": _gate_downsize}
                             if _gate_verdict is not None else None),
                }
                # Synthesise the conjunctive quorum on top of a fresh confirmed base (the SENTINEL/NEXUS
                # committee pass above already applied its own de-escalation to `scaled`; here the quorum
                # only ADDS the three new seats' subtract-only caps, so we start from a clean confirm/1.0
                # and multiply the resulting scale into `scaled`, mirroring the committee-trim path).
                _quorum = _committee.nexus(
                    {"confirmed": True, "combined": breakdown.get("combined")}, None,
                    technician=_seat_kwargs["technician"], strategist=_seat_kwargs["strategist"],
                    pm_conviction=_seat_kwargs["pm_conviction"], gate=_seat_kwargs["gate"])
                _q_action = _quorum.get("action")
                _q_scale = float(_quorum.get("scale", 1.0) or 0.0)
                # GATE-OFFICER approve-with-downsize (§2.5 row 5 smallest-size-wins) is NOT wired into
                # nexus's 3-arg apply_gate loop, so apply it here via the SAME pure helper — a min()-cap
                # that can only lower _q_scale, never raise it. A veto/withhold already dropped above.
                if _gate_downsize is not None and _q_action != "drop":
                    _q_action, _q_scale = _committee.apply_gate(
                        _q_action, _q_scale, "approve", downsize_to=_gate_downsize)
                    _q_scale = float(_q_scale or 0.0)
                _final_action = "park" if _q_action == "drop" else ("trim" if _q_scale < 1.0 else "confirm")
                try:
                    _pipeline_ledger_write({
                        "asof": asof, "ticker": t,
                        "forge_confirmed": bool(breakdown.get("confirmed")),
                        "sentinel_stance": (committee_block or {}).get("sentinel_stance"),
                        "strategist_verdict": _strat_verdict,
                        "technician_verdict": _tech_verdict,
                        "pm_verdict": _pm_verdict,
                        "gate_verdict": (_gate_dec or {}).get("action") if _gate_dec else _gate_verdict,
                        "final_action": _final_action, "final_scale": round(_q_scale, 4),
                        "mode": _dqm,
                    })
                except Exception:  # noqa: BLE001 — ledger is best-effort
                    pass
                if _dqm == "enforce":
                    if _q_action == "drop":
                        # PARK the name via the EXACT existing timing-withhold path: append to the
                        # watchlist, emit a shadow weight-0 row, and `continue` (no book entry). The
                        # reason names the strictest seat that cut (from the nexus rationale tail).
                        _twreason = "desk-quorum: " + str(_quorum.get("rationale") or "withhold")[:200]
                        research_held.append({"ticker": t, "reason": "timing withhold: " + _twreason,
                                              **research_block})
                        _emit_shadow(t, c, breakdown, forge_confirmed=True, weight_prod=0.0,
                                     committee_block=committee_block, sentinel=_sent, price=px, is_new=is_new)
                        try:
                            from portfolio import watchlist as _watchlist
                            _watchlist.append(t, asof, _twreason, tech=_entry_tech_fields(t),
                                              combined=breakdown.get("combined"))
                        except Exception:  # noqa: BLE001 — watchlist logging never blocks the gate
                            pass
                        _rl_log(_run_id, "decision", f"DESK-QUORUM WITHHOLD {t}", _twreason, ticker=t)
                        continue
                    if _q_scale < 1.0:
                        scaled = round(scaled * _q_scale, 4)
            entry = {**c, "weight": scaled, "research": research_block}
            if committee_block:
                entry["committee"] = committee_block
            confirmed_sized.append(entry)
            _emit_shadow(t, c, breakdown, forge_confirmed=True, weight_prod=scaled,
                         committee_block=committee_block, sentinel=_sent, price=px, is_new=is_new)
            _rl_log(_run_id, "decision", f"RESEARCH CONFIRM {t}",
                    f"combined={breakdown['combined']} (engine {breakdown['engine_score']} + "
                    f"research {breakdown['research_score']}) viab={breakdown['viability']} "
                    f"size_mult={breakdown['size_mult']} weight={scaled}"
                    + (f" | committee={committee_block['action']}(x{committee_block['scale']})"
                       if committee_block else ""),
                    ticker=t, **research_block)
        else:
            research_held.append({"ticker": t, "reason": breakdown["reason"], **research_block})
            _emit_shadow(t, c, breakdown, forge_confirmed=False, weight_prod=0.0,
                         committee_block=None, sentinel=None, price=px, is_new=is_new)
            _rl_log(_run_id, "decision", f"RESEARCH HOLD {t}",
                    f"reason={breakdown['reason']} combined={breakdown['combined']} "
                    f"viab={breakdown['viability']}",
                    ticker=t, **research_block)
    # ── NEW-SIZE-4: re-assert the BOOK-CAP invariant AFTER the return-flavored multiply ──────────
    # phase2:511 multiplies conviction.build()'s already risk-sized + sector-capped weight by the
    # research size_mult (0.5x..1.3x — a RETURN/conviction score, brain/research_paper.py:144). A
    # >1.0 size_mult can therefore RE-INFLATE a name the sector firebreak just trimmed, breaching the
    # SECTOR_MAX_FRACTION invariant that conviction.build enforced PRE-multiply (there is no re-cap
    # after the multiply — only the per-name min(name_cap,...) clamp at :511). We re-run the existing,
    # already-subtract-only _apply_sector_cap on the FINAL persisted book so no sector exceeds
    # SECTOR_MAX_FRACTION*budget after the return-flavored multiply — preserving the mandated
    # composition order (budget -> per-leg -> BOOK caps). It is a no-op on the common case (all
    # size_mult <= 1.0 leave the sector at/under the cap it was already at → nothing to scale down).
    try:
        conviction._apply_sector_cap(confirmed_sized, conv_budget)
    except Exception:  # noqa: BLE001 — subtract-only re-cap is defensive; never break the build
        pass
    sized = confirmed_sized
    # ── FLAGSHIP DEEP-REASONING JUDGMENT LAYER (flag-gated, default OFF) ───────────────────
    # When MASTERMIND_FLAGSHIP_JUDGMENT is OFF this branch is inert → the engine path below is
    # BYTE-IDENTICAL. When ON, the MACRO STRATEGIST + PM-CONVICTION seats reshape the engine's
    # confirmed list into the PM's Flagship target book (the PM may add high-conviction thematic
    # names + drop engine names lacking a live thesis), each name checked by the existing blind
    # SENTINEL + subtract-only NEXUS + name_cap clamp. The return keeps the EXACT schema
    # conviction.build emits, so the unchanged downstream loop rebalances/marks/publishes/grades
    # the judgment book identically to the engine book. Additive; never breaks the engine build.
    if _judgment_enabled():
        try:
            from brain import judgment_book as _jb
            _pctx = {"held_conviction": sorted(_open_conv), "n_held": len(_open_conv)}
            # W4 B1 LEADERSHIP PIPE: the leadership legs live in `book` (built + capped above). Pipe
            # them into the judgment layer so the PM sees the FULL sleeve-tagged book (killing the
            # placebo hole where the 40-60% leadership sleeve was structurally untouchable). The PM
            # may DROP a leadership leg (→ its budget becomes cash/defensive) or KEEP it at engine
            # weight (the deterministic authority clamp inside build() forbids re-weighting a
            # survivor). We snapshot the engine leadership legs, run the judgment reshape, then apply
            # the PM's keep/drop verdict to `book`'s leadership legs — the reshaped CONVICTION rows
            # flow on as `sized` exactly as before.
            _lead_legs = [p for p in book if p.get("sleeve") == "leadership"]
            _reshaped = _jb.build(sized, _rejected, regime=regime, asof=asof, gate_info=gate_info,
                                  shadow_inputs=_shadow_inputs, portfolio_ctx=_pctx, name_cap=name_cap,
                                  directive=directive, leadership=_lead_legs)
            # If the layer degraded to the engine path it returns the SAME `sized` object → no reshape
            # happened, leave leadership untouched (byte-identical). Otherwise split the reshaped book:
            # leadership-sleeve rows are the PM's KEPT leaders (weights already engine-clamped); the
            # rest are conviction rows that continue as `sized`.
            if _reshaped is not sized:
                _kept_lead = {}
                _conv_rows = []
                for r in _reshaped:
                    if r.get("sleeve") == "leadership":
                        _kept_lead[str(r.get("ticker") or "").upper()] = r
                    else:
                        _conv_rows.append(r)
                # apply keep/drop to book's leadership legs: keep only the tickers the PM retained,
                # at the engine weight the authority clamp restored (never re-weighted upward).
                _new_book = []
                for p in book:
                    if p.get("sleeve") != "leadership":
                        _new_book.append(p)
                        continue
                    _tk = str(p.get("ticker") or "").upper()
                    _kr = _kept_lead.get(_tk)
                    if _kr is None:
                        _rl_log(_run_id, "decision", f"judgment DROP leadership {_tk}",
                                "PM dropped leadership leg → budget to cash/defensive",
                                ticker=_tk, sleeve="leadership")
                        continue                       # dropped → freed to cash/defensive
                    _new_book.append(p)                # kept at engine weight (clamp already enforced)
                book = _new_book
                sized = _conv_rows
            else:
                sized = _reshaped
        except Exception:  # noqa: BLE001 — additive; a judgment-layer failure never breaks the build
            pass
    # ──────────────────────────────────────────────────────────────────────────────────────
    _rl_log(_run_id, "book_step", "research gate evaluated",
            f"confirmed={len(sized)} research_held={len(research_held)} "
            f"rejected={len(_rejected)} armed={_armed_ok}")
    # persist today's shadow-book decision inputs (audit + standalone replay source)
    try:
        from portfolio import shadow_books as _shadow
        _shadow.write_inputs(asof, _shadow_inputs)
    except Exception:  # noqa: BLE001 — shadow books are additive; never block the build
        pass

    for c in sized:
        t = c["ticker"]
        px = ((_j(f"site/stockdata/{t}.json") or {}).get("tech", {}) or {}).get("price") \
            if (_V / f"site/stockdata/{t}.json").exists() else None
        _gi = gate_info.get(t, {})
        _full_t = _gi.get("full") or {}
        _synth = _full_t.get("synthesis") or {}
        _matrix_rows = _full_t.get("rows") or []
        if not _synth:
            try:
                _matrix = lenses_mod.decision_matrix(t, "name")
                _synth = lenses_mod.synthesize(_matrix)
                _matrix_rows = _matrix["rows"]
            except Exception:
                _synth = {}
                _matrix_rows = []
        # a name kept only by exit-hysteresis is a HOLD (entry gate not re-cleared), not a fresh add —
        # the decision doc must say so rather than claiming "all sides confirm".
        is_retained = bool(c.get("retained"))
        if is_retained:
            _lean, _verdict = "hold", "hold"
            _thesis = (f"Hysteresis HOLD — confluence {c['confluence']:+.2f} is above the exit floor "
                       f"({conviction._EXIT_CONFLUENCE_FLOOR:+.2f}) but the entry gate was NOT re-cleared "
                       f"(bull {c['bull']}/bear {c['bear']}); carried, not freshly confirmed. Monitor for exit.")
            _evidence = [f"retained=hysteresis_hold", f"confluence={c['confluence']:+.2f}"]
        else:
            _lean, _verdict = "add", "add"
            _thesis = (f"Multi-sided confluence {c['confluence']:+.2f} (bull {c['bull']}/bear {c['bear']}); "
                       f"all sides confirm and no hard veto — sized {round(c['weight']*100)}%.")
            _evidence = [f"size_authority=up", f"confluence={c['confluence']:+.2f}"]
        # FORGE confidence, de-confidenced by its realized calibration (≤1.0 shrink only). The RAW
        # value is stored so calibration always grades the model's native confidence (convergent).
        _raw_pc = round(0.55 + min(0.15, c["confluence"] * 0.4), 2)
        try:
            from brain import calibration as _calib
            _pc = round(_raw_pc * _calib.multiplier("forge"), 2)
        except Exception:  # noqa: BLE001
            _pc = _raw_pc
        # PUBLISHED entry_signal risk levels (P-NEW-3) — the dashboard's per-name stop / buy_zone /
        # entry_grade / chase_above. All None-on-miss; keys are added to entry_levels + the book dict
        # ONLY when non-null so legacy/absent-signal names stay byte-identical (add-only, degrade-safe).
        # This is pure PROVENANCE + persistence: it records a level, it does not size, gate, or exit.
        _es = _published_entry_signal(t)
        _entry_levels = {"ticker": t}
        if px:
            _entry_levels["price"] = px
        if _es.get("stop") is not None:
            _entry_levels["stop"] = _es["stop"]
        if _es.get("buy_zone"):
            _entry_levels["buy_zone"] = _es["buy_zone"]
        if _es.get("entry_grade"):
            _entry_levels["entry_grade"] = _es["entry_grade"]
        if _es.get("chase_above") is not None:
            _entry_levels["chase_above"] = _es["chase_above"]
        doc = DecisionDoc(
            id=f"{asof}-{t}-conv", subject=t, lean=_lean, conviction="medium",
            prob_correct=_pc, raw_prob_correct=_raw_pc,
            horizon_d=21, state_asof=asof, sleeve="conviction", order_layer=1,
            thesis=_thesis,
            evidence=_evidence
                     + ([f"divergence:{d}" for d in c["divergences"]] if c["divergences"] else []),
            dissent="Held at 0 if any side vetoes (parabolic/distress/cycle-blocked).",
            entry_levels=_entry_levels,
        ).finalize()
        _synth_map[doc.id] = (_synth, _matrix_rows)
        decisions.append(doc.to_json())
        ledger.append(doc.to_json())
        store.insert_thesis(con, doc.to_json())
        _book_row = {"ticker": t, "theme_id": _conv_theme_id(t), "sleeve": "conviction", "stage": 2,
                     "weight": c["weight"], "verdict": _verdict, "thesis_id": doc.id,
                     "time_stop_by": doc.time_stop_by, "confluence": c["confluence"],
                     "entry_price": px, "research": c.get("research"), "retained": is_retained,
                     "size_stage": c.get("size_stage"),
                     # carry entry_levels so position_log.update() can persist the published stop
                     "entry_levels": _entry_levels}
        # mirror the published levels onto the book row too (add-only) so downstream marks/audit see them
        if _es.get("stop") is not None:
            _book_row["published_stop"] = _es["stop"]
        if _es.get("buy_zone"):
            _book_row["buy_zone"] = _es["buy_zone"]
        if _es.get("entry_grade"):
            _book_row["entry_grade"] = _es["entry_grade"]
        book.append(_book_row)
        _rl_log(_run_id, "trade", f"sized {t} conviction",
                f"ticker={t} weight={c['weight']} confluence={c['confluence']:+.2f} "
                f"bull={c['bull']} bear={c['bear']} price={px} verdict={_verdict}",
                ticker=t, sleeve="conviction", weight=c["weight"],
                confluence=c["confluence"], verdict=_verdict)

    # ———— W4 B2: DETERMINISTIC DEF_SLEEVE rotation floor (architecture Stage 4 — the E1-failure branch) ——
    # AFTER the judgment layer, BEFORE the cross-sleeve firebreaks. When the armed additive PM (task B1)
    # echoes the engine, this deterministic sleeve ships defensive rotation ANYWAY; it is ALSO the floor
    # an armed PM may not undercut (rotation.floor_legs). It BUYS from the ONE canonical pool
    # (portfolio/defensive_candidates.py), equal-weight across frozen weights, tagged
    # theme_id='DEFENSIVE_<archetype>' so enforce_book_caps below sees a real cluster-mapped position.
    # BUDGET DISCIPLINE (no double-claim): the sleeve consumes ONLY the cash the W2 flex + W2/W3 caps
    # already freed FROM leadership (rotation._headroom: total gross with the sleeve <= the un-flexed
    # engine gross AND cash >= floor). DEF_SLEEVE_MAX=0 (doctrine default) → def_budget 0 → NO legs →
    # the book is BYTE-IDENTICAL to today (the E1 control arm). Additive; never breaks the build.
    try:
        from portfolio import rotation as _rot
        # best-effort read of the persistent dwell state for the fragility sizing (read-only here — the
        # macro-risk CAP itself runs later, unchanged). A failure → None → the sleeve degrades safely.
        _rot_risk_state = None
        try:
            from brain import macro_risk as _rot_mr
            _rot_risk_state = _rot_mr.risk_state(asof, regime)
        except Exception:  # noqa: BLE001 — the sleeve tolerates a None risk_state (dwell term → 0)
            _rot_risk_state = None
        # E2.2: posture ON ⇒ the decider's defense_floor IS the sleeve target and
        # fragility_signal is not consulted (its planes live in posture D — single consumption).
        _def = _rot.build_def_sleeve(book, _rot_risk_state, _budget_inputs,
                                     evidence=_rotation_evidence,
                                     target=(_posture.get("defense_floor")
                                             if _posture is not None else None))
        if _def.get("legs"):
            book.extend(_def["legs"])
            _rl_log(_run_id, "book_step", "DEF_SLEEVE rotation floor",
                    _def["reason"], def_actual=_def["def_actual"], def_budget=_def["def_budget"],
                    headroom=_def["headroom"], fragility_signal=_def["fragility_signal"],
                    tickers=[p["ticker"] for p in _def["legs"]])
    except Exception as _e:  # noqa: BLE001 — a contingent rotation sleeve must never break the build
        _rl_log(_run_id, "decision", "def_sleeve error", f"{_e!r}"[:160])

    # ———— cross-sleeve firebreaks ————
    capped = enforce_book_caps(book)
    book = capped["positions"]

    # ———— W3 B1: FIRM-WIDE headroom clamp (architecture Stage 6.3 — the BINDING firm cap) ————
    # After the per-book cluster/name firebreaks, clamp THIS book's contribution DOWN so the FIRM-WIDE
    # cluster (0.30) / name (0.10) caps hold across all US books. The audit proved four US books
    # independently max-convicted the SAME SMH with nothing trimming the aggregate — this is the fix.
    # Subtract-only: freed weight falls to CASH (the cash sizing below reads the reduced gross); never
    # raises a weight; a byte-identical no-op when no peer file is readable or the book already fits.
    # SEQUENTIAL FAIRNESS (BY DESIGN): Flagship builds FIRST in the 22:40 order, so it claims firm
    # headroom first — it sees the OTHER books' prior-published exposure and clamps against it; the
    # later books then clamp against Flagship's freshly-published book. Flag-gated (MASTERMIND_FIRM_CAPS,
    # default ON — see firm_exposure.caps_enabled for why default-on). Best-effort; never breaks the build.
    try:
        from portfolio import firm_exposure as _firm
        if _firm.caps_enabled():
            _fc = _firm.clamp_book(book, "flagship")
            book = _fc["positions"]
            if _fc.get("bound"):
                _rl_log(_run_id, "book_step", "FIRM CAP clamp",
                        f"freed={_fc['freed']} clamped={_fc['clamped']}",
                        firm_clamp={"book": "flagship", "freed": _fc["freed"],
                                    "clamped": _fc["clamped"]})
    except Exception as _e:  # noqa: BLE001 — a firm cap must never break the build
        # GuardrailResult.FREEZE: freeze to prior book — no new adds, no weight increases.
        # Uses _firm_clamp_freeze_flagship (module-level) so the logic is independently testable.
        book = _firm_clamp_freeze_flagship(book, _e, run_id=_run_id)

    # ———— W2 GUARD-RAIL: offensive-gross floor tripwire (architecture Stage 6.5) ————
    # After ALL brakes (leadership caps + cross-sleeve firebreaks), the offensive (leadership) gross must
    # stay >= floor_frac · lead_budget unless a parabolic hard veto fired — otherwise the compounding-
    # shrink stack has permanently under-invested the book. We EMIT A LOUD TRIPWIRE ('over_degross')
    # rather than scale the brakes back: the brakes are subtract-only SAFETY caps whose whole point is
    # that an over-extended / late-cycle leg is unsafe at size, so mechanically un-shrinking them to hit
    # a gross floor would re-inflate the very risk they removed. The tripwire makes it auditable.
    try:
        from portfolio import sleeves as _sleeves_tw
        _tw = _sleeves_tw.offensive_gross_tripwire(book, lead_budget, parabolic_veto_fired=False)
        if _tw.get("breached"):
            _rl_log(_run_id, "decision", "TRIPWIRE over_degross",
                    _tw["reason"], offensive_gross=_tw["offensive_gross"], floor=_tw["floor"])
    except Exception as _e:  # noqa: BLE001 — a monitoring tripwire must never break the build
        _rl_log(_run_id, "decision", "over_degross tripwire error", f"{_e!r}"[:160])

    # ———— D5 dead-capital time-stop — an ACTUAL exit (doctrine: D5/self is a hard sizing veto) ——
    # Flag conviction lots that are past their time_stop_by AND flat/negative since entry AND lagging
    # the leading sector, then REDEPLOY — drop them from the book and close their thesis so the
    # capital is freed (was unwired with unpopulated inputs, so it could never fire).
    _d5_fired: list[dict] = []
    _d5_exited_tk: set[str] = set()   # names exited on the D5 time-stop — used to code their ledger close
    try:
        from portfolio import paper_account as _pa_d5
        # use the PRE-update ledger so a carried name carries its ORIGINAL time_stop_by (the book's is
        # recomputed each run, which would reset the clock so the time-stop could never elapse), and
        # restrict to names still in today's book.
        _book_conv = {p["ticker"] for p in book if p.get("sleeve") == "conviction"}
        _open_conv = [hp for hp in position_log.open_positions()
                      if hp.get("sleeve") == "conviction" and hp["ticker"] in _book_conv]
        _leader_pctile = secrs[0].get("pctile_252d") if secrs else None
        _etf_pctile = {x.get("ticker"): x.get("pctile_252d") for x in secrs}
        _sector_pctile, _d5_prices = {}, {}
        for _hp in _open_conv:
            _t = _hp["ticker"]
            _sd = lenses_mod._load(f"site/stockdata/{_t}.json") or {}
            _sector_pctile[_t] = _etf_pctile.get(lenses_mod._SECTOR_ETF.get(_sd.get("sector")))
            _px = _pa_d5._current_price(_t)
            if _px:
                _d5_prices[_t] = _px
        _d5_lots = _build_d5_lots(_open_conv, _d5_prices, _sector_pctile, _leader_pctile,
                                  date.fromisoformat(asof))
        _d5_fired = detectors.d5_dead_capital(_d5_lots, date.fromisoformat(asof), "self")
        _d5_exit = {d["subject"] for d in _d5_fired if d.get("mode") == "self"}
        _d5_exited_tk = set(_d5_exit)
        if _d5_exit:
            book = [p for p in book
                    if not (p.get("sleeve") == "conviction" and p["ticker"] in _d5_exit)]
            for _t in _d5_exit:
                try:
                    ledger.close(_t, "exited (D5 dead-capital time-stop)")
                except Exception:
                    pass
            _rl_log(_run_id, "decision", "D5 dead-capital EXIT",
                    f"redeployed={sorted(_d5_exit)}", exited=sorted(_d5_exit))
    except Exception as _e:
        _rl_log(_run_id, "decision", "d5 wiring error", f"{_e!r}"[:160])

    # ———— D1/D2/D4 — advisory behavioural failure-mode tells (on the post-exit book) ————
    _d124_fired: list[dict] = []
    try:
        from portfolio import paper_account as _pa_det
        _open_by_tk = {hp["ticker"]: hp for hp in position_log.open_positions()
                       if hp.get("sleeve") == "conviction"}
        _lead_pct = secrs[0].get("pctile_252d") if secrs else None
        _etf_pct = {x.get("ticker"): x.get("pctile_252d") for x in secrs}
        _lots, _new_buys = [], []
        for _p in book:
            if _p.get("sleeve") != "conviction":
                continue
            _t = _p["ticker"]
            _sd = lenses_mod._load(f"site/stockdata/{_t}.json") or {}
            _prior = _open_by_tk.get(_t)
            _px = _pa_det._current_price(_t)
            _entry = (_prior or {}).get("entry_price") or _p.get("entry_price")
            _rel = (_px / _entry - 1.0) if (_px and _entry and float(_entry) > 0) else None
            _spct = _etf_pct.get(lenses_mod._SECTOR_ETF.get(_sd.get("sector")))
            _gap = (max(0.0, (_lead_pct - _spct) / 100.0)
                    if (_spct is not None and _lead_pct is not None) else 0.0)
            _para = bool(lenses_mod._g(_sd, "conviction.ext.parabolic")
                         or lenses_mod._g(_sd, "conviction.extension.parabolic"))
            _eg = (lenses_mod._g(_sd, "conviction.ext.grade")
                   or lenses_mod._g(_sd, "conviction.extension.grade"))
            _pv2 = lenses_mod._g(_sd, "tech.pct_vs_200dma")
            _prior_w = (_prior or {}).get("current_weight") or 0.0
            _lot = {"ticker": _t, "id": _p.get("thesis_id"), "is_new": _prior is None,
                    "is_add": (_p.get("weight") or 0) > (_prior_w + 0.005),
                    "rel_return_since_entry": _rel, "rs_leader_gap": round(_gap, 4),
                    "held_days": (_prior or {}).get("held_days") or 0, "time_stop_td": 63,
                    "extension_bear": _para or _eg in ("stretched", "parabolic") or ((_pv2 or 0) >= 30),
                    "parabolic": _para}
            _lots.append(_lot)
            if _lot["is_new"]:
                _new_buys.append(_lot)
        _held_lots = [l for l in _lots if not l["is_new"]]
        _d124_fired = (detectors.d1_disposition(_held_lots, "self")
                       + detectors.d2_late_stage_reach(_new_buys, "self")
                       + detectors.d4_avg_down_into_divergence(_held_lots, "self"))
    except Exception as _e:
        _rl_log(_run_id, "decision", "d1/d2/d4 wiring error", f"{_e!r}"[:160])

    # ———— RISK OFFICER — judgment exits over HELD conviction (layered ON TOP of the mechanical
    # detectors above: D5 dead-capital, hard-veto sweep, hysteresis). Subtract-only — trim/exit
    # only, never adds, never averages down; the never-blow-to-cash guard is enforced inside
    # risk_assess (the returned `decisions` are already guard-honoured). Exits reuse the SAME
    # close/trim recording the detectors use (position_log.close_position + ledger.close) so they
    # are gradable. Flag-gated (MASTERMIND_RISK_OFFICER, default OFF) → byte-identical until armed.
    # Additive + reversible: any failure leaves `book` untouched. ————
    if _risk_officer_enabled():
        try:
            from brain import risk_officer as _ro
            _ro_held = [hp for hp in position_log.open_positions()
                        if hp.get("sleeve") == "conviction"]
            _ro_res = _ro.risk_assess(book, asof, regime=regime, held_positions=_ro_held)
            _ro_exits: list[str] = []
            _ro_trims: list[str] = []
            for _d in _ro_res.get("decisions", []):
                _t = _d.get("ticker")
                if _d.get("action") == "exit":
                    book = [p for p in book
                            if not (p.get("sleeve") == "conviction" and p["ticker"] == _t)]
                    try:
                        position_log.close_position("conviction", _t, asof,
                                                    reason="risk_officer_exit",
                                                    reason_code="risk_officer_exit")
                    except Exception:
                        pass
                    try:
                        ledger.close(_t, f"exited (Risk Officer: {_d.get('reason', 'thesis_broken')})")
                    except Exception:
                        pass
                    _ro_exits.append(_t)
                elif _d.get("action") == "trim":
                    for _p in book:
                        if _p["ticker"] == _t and _p.get("sleeve") == "conviction":
                            try:
                                _p["weight"] = round(float(_p.get("weight") or 0.0)
                                                     * float(_d.get("scale", 1.0)), 4)
                            except (TypeError, ValueError):
                                pass
                            _ro_trims.append(_t)
            _rl_log(_run_id, "decision", "risk officer pass",
                    f"exits={_ro_exits} trims={_ro_trims}",
                    exited=_ro_exits)
        except Exception as _e:
            _rl_log(_run_id, "decision", "risk officer wiring error", f"{_e!r}"[:160])

    # ———— MACRO RISK OFFICER cap — the top-down DEFENSE the desk lacked on 2026-06-23. Subtract-only,
    # bound to the deterministic risk state (no LLM dependence): in caution/risk-off it scales the
    # conviction book's gross down to the (driver-tightened) gross cap and trims an over-concentrated
    # CRACKING fragility chain. This catches the ENGINE path even when the Flagship judgment layer is
    # off. Names trimmed to zero are realized as exits via the SAME close path the detectors use (so
    # they are gradable). Flag-gated (MASTERMIND_MACRO_RISK, default OFF) → byte-identical; never raises. ——
    if _macro_risk_enabled():
        try:
            from brain import macro_risk as _mr
            _mrs = _mr.run(asof, regime)
            if _mrs.get("state") != "risk_on":
                _conv = [p for p in book if p.get("sleeve") == "conviction"]
                _other = [p for p in book if p.get("sleeve") != "conviction"]
                _capped = _mr.apply_risk_state(_conv, _mrs)
                _kept_tk = {p["ticker"] for p in _capped}
                for _p in _conv:
                    if _p["ticker"] not in _kept_tk:
                        try:
                            position_log.close_position("conviction", _p["ticker"], asof,
                                                        reason="macro_risk_cap",
                                                        reason_code="cap_trim")
                        except Exception:
                            pass
                        try:
                            ledger.close(_p["ticker"], "exited (macro risk cap)")
                        except Exception:
                            pass
                book = _other + _capped
                _rl_log(_run_id, "decision", "macro risk cap",
                        f"state={_mrs.get('state')} gross_cap={_mrs.get('gross_cap')} "
                        f"kept={sorted(_kept_tk)}")
        except Exception as _e:
            _rl_log(_run_id, "decision", "macro risk wiring error", f"{_e!r}"[:160])

    # ———— safety overlay: SUBTRACT-ONLY de-gross of a fragile book (CONSUMED, not display) ————
    # Measure the proposed book's risk (static-weight historical sim; local prices only so the
    # build stays fast + deterministic) and, if it is fragile (deep drawdown / high beta /
    # one-factor concentration / low score), scale every position down — the freed weight
    # becomes cash. This is the safety read actually CHANGING the book, never levering it up.
    # Runs LAST, on the post-exit book (after the Risk Officer + Macro Risk Officer passes), so the
    # fragility read + the de-gross reflect the final set of held names.
    _safety = None
    _safety_overlay = {"gross_mult": 1.0}
    try:
        from portfolio import safety as _safety_mod
        _pw = {p["ticker"]: p["weight"] for p in book}
        if _pw:
            _pre = round(sum(_pw.values()), 4)
            _safety = _safety_mod.compute_safety(
                portfolio_id="flagship", asof=asof, weights=_pw,
                cash_weight=round(max(0.0, 1.0 - _pre), 4), bootstrap=True, network=False)
            _safety_overlay = _safety_mod.gross_overlay(_safety)
            _gm = float(_safety_overlay.get("gross_mult", 1.0))
            if _gm < 1.0:
                for p in book:
                    p["weight"] = round(p["weight"] * _gm, 4)
                    p["safety_degross"] = _gm
                _rl_log(_run_id, "book_step", "safety de-gross",
                        f"gross_mult={_gm} reasons={_safety_overlay.get('reasons')}")
            _safety["overlay"] = {**_safety_overlay, "applied": True}   # the consumed action, on the report
            try:
                _safety_mod.persist(_safety, "flagship")     # so /api/risk serves it w/o recompute
            except Exception:
                pass
    except Exception as _e:
        _rl_log(_run_id, "decision", "safety overlay error", f"{_e!r}"[:160])

    # ———— cash (sized after all exits + the safety de-gross) ————
    gross = round(sum(p["weight"] for p in book), 4)
    macro_implied_cash = round(max(0.0, 1.0 - gross), 4)
    cash = round(binding_cash(macro_implied_cash), 4)
    top_theme_conc = max((p["weight"] for p in book), default=0.0)

    _rl_log(_run_id, "book_step", "caps applied",
            f"gross={gross} cash={cash} breaches={capped['breaches']} d5_exits={len(_d5_fired)}")

    # ———— detectors (on the post-exit book) ————
    fired = detectors.d3_no_rotation_capacity(cash, top_theme_conc, "self") + \
        detectors.d6_cap_breach(capped["breaches"], "self") + _d5_fired + _d124_fired
    try:                                              # D7 = whole-book fragility (from the safety read)
        from portfolio import safety as _safety_mod
        fired += _safety_mod.fragility_detectors(_safety, "self")
    except Exception:
        pass

    if fired:
        _rl_log(_run_id, "decision", "detectors fired",
                f"codes={[d['code'] for d in fired]}")

    # ———— build per-ticker close-reason index BEFORE the ledger update ————
    # Reconstruct WHY each held conviction name left the book so observers can distinguish a
    # routine rebuild rotation (the S4 scenario) from a deliberate risk exit.  The three rebuild
    # paths are mutually exclusive (hard_exit → exit-floor → not-in-universe), ordered by
    # severity.  Non-rebuild paths (D5 time-stop, Risk Officer, Macro Risk Cap) are already
    # closed by their own sweep above and will NOT be re-processed by update() because
    # close_position() has already set still_open=False; we include them in the ledger string
    # for completeness but they are effectively a no-op on the position_log side.
    _rejected_index: dict[str, dict] = {r["ticker"]: r for r in _rejected}

    def _rebuild_reason(ticker: str) -> str:
        """Derive the most specific rebuild close-reason for a dropped conviction name."""
        rej = _rejected_index.get(ticker)
        if rej is None:
            # The name was not even in the candidate universe this run (filtered upstream).
            return "rebuild: not in new candidate universe"
        # If the rejection reason signals a hard structural failure (veto / downtrend / blocked),
        # it is a hard exit from the rebuild, not merely falling below the hysteresis floor.
        _hard_markers = ("vetoed", "blocked", "downtrend", "falling knife")
        _reason_lower = (rej.get("reason") or "").lower()
        if rej.get("vetoes") or any(m in _reason_lower for m in _hard_markers):
            return f"rebuild: hard exit (veto/downtrend/blocked) — {rej.get('reason', '')}"
        # Otherwise the name was evaluated but fell below the exit confluence floor.
        conf = rej.get("confluence")
        _conf_str = f"{conf:+.2f}" if isinstance(conf, (int, float)) else str(conf)
        return f"rebuild: fell below exit floor (confluence {_conf_str})"

    _final_conv = {p["ticker"] for p in book if p.get("sleeve") == "conviction"}
    _dropped_conv = _held_conv - _final_conv
    _close_reasons: dict[str, str] = {t: _rebuild_reason(t) for t in _dropped_conv}

    def _rebuild_reason_code(ticker: str) -> str:
        """Structured close code for a name the rebuild reconciliation drops from the book.

        A name evaluated-and-rejected on a hard structural marker (veto / downtrend / blocked) is a
        'hard_veto'; every other rebuild drop (fell below the exit floor, or simply not in the new
        candidate universe) is a routine 'rebuild_dropped' rotation. This is what makes a nightly
        rotation machine-distinguishable from a deliberate risk exit."""
        rej = _rejected_index.get(ticker)
        if rej is not None:
            _reason_lower = (rej.get("reason") or "").lower()
            _hard_markers = ("vetoed", "blocked", "downtrend", "falling knife")
            if rej.get("vetoes") or any(m in _reason_lower for m in _hard_markers):
                return "hard_veto"
        return "rebuild_dropped"

    # Structured companion to _close_reasons — same keys, machine-readable codes (see
    # position_log.REASON_CODES). Threaded to update() so the ledger close event carries both.
    _close_reason_codes: dict[str, str] = {t: _rebuild_reason_code(t) for t in _dropped_conv}

    # A D5 dead-capital exit REMOVES the name from `book` above, so update()'s reconciliation would
    # otherwise close it as a generic 'rebuild_dropped' rotation — hiding the time-stop. Override the
    # code (and the human string) for D5-exited names still in _dropped_conv so the close is honestly
    # coded as the time-stop it is. (position_log.close_position isn't used on the D5 path; the close
    # lands via update()'s reconciliation, which is why the override belongs here.)
    for _t in _d5_exited_tk & _dropped_conv:
        _close_reason_codes[_t] = "time_stop_d5"
        _close_reasons[_t] = "exited (D5 dead-capital time-stop)"

    # ———— MW3 R3 STALE-ANCHOR FREEZE — applied BEFORE ledger/store/rebalance/publish ————
    # When a FREEZE-class anchor is stale beyond its contract budget, freeze the flagship
    # targets to the prior published book: no new adds, no weight increases (de-risk allowed).
    # The seam mirrors the firm-clamp freeze (_firm_clamp_freeze_flagship, ~:1067) so both
    # arms gate the same downstream writes.  Kill-switch: MASTERMIND_STALE_FREEZE=0 → log only.
    _stale_freeze_summary: dict | None = None
    if (isinstance(stale_freeze, dict) and stale_freeze.get("freeze")):
        from data_layer.macro_refresh import _freeze_enabled
        _sf_reasons: list = stale_freeze.get("freeze_reasons") or []
        if _freeze_enabled():
            book = _stale_freeze_flagship(book, _sf_reasons, run_id=_run_id)
            _stale_freeze_summary = {
                "applied": True,
                "reasons": _sf_reasons,
                "asof": stale_freeze.get("asof"),
            }
            _rl_log(_run_id, "decision", "STALE-ANCHOR FREEZE applied",
                    f"flagship frozen to prior: {_sf_reasons}"[:200])
        else:
            # Kill-switch active: log but do not apply
            _stale_freeze_summary = {
                "applied": False,
                "kill_switch": True,
                "reasons": _sf_reasons,
            }
            _rl_log(_run_id, "decision", "STALE-ANCHOR FREEZE suppressed (kill-switch)",
                    f"MASTERMIND_STALE_FREEZE=0; reasons={_sf_reasons}"[:200])

    # ———— update positions ledger ————
    position_log.update(book, asof, close_reasons=_close_reasons, reason_codes=_close_reason_codes)

    # close ledger theses for conviction names that LEFT the book this run — the append-only ledger
    # otherwise keeps the old thesis 'open' forever (blocking any re-proposal and accreting stale
    # names in the open-thesis candidate pool).
    for _dropped in _dropped_conv:
        try:
            ledger.close(_dropped, _close_reasons.get(_dropped, "exited (left book)"))
        except Exception:
            pass

    # ———— STOP-BREACH surfacing (P-NEW-3, NON-EXECUTING) ————
    # For every open conviction position that persisted a published entry_signal.stop, compare it to
    # the current price. If price < published_stop, LOG a loud runlog step. This W1 deliverable
    # OBSERVES and RECORDS — it does NOT sell (stop EXECUTION is W2 work). Invariant-safe: a name
    # with no persisted stop, or a name with no current price, is silently skipped (degrades to
    # today's behaviour). Best-effort: wrapped so it can never break the build.
    try:
        from portfolio import paper_account as _pa_stop
        for _op in position_log.open_positions():
            if _op.get("sleeve") != "conviction":
                continue
            _stop = _op.get("published_stop")
            if _stop is None:
                continue
            _t = _op.get("ticker")
            _cur = _pa_stop._current_price(_t)
            if _cur is None:
                continue
            try:
                if float(_cur) < float(_stop):
                    _rl_log(_run_id, "risk", f"STOP BREACH {_t}",
                            f"price={_cur} < published_stop={_stop} (published entry_signal.stop; "
                            f"W1 surfaces, does not exit)", ticker=_t, sleeve="conviction")
            except (TypeError, ValueError):
                continue
    except Exception as _e:  # noqa: BLE001
        _rl_log(_run_id, "decision", "stop-breach surfacing error", f"{_e!r}"[:160])

    # ———— enrich positions with opened_at / held_days / thesis_full ————
    decisions_by_id = {d["id"]: d for d in decisions}
    for p in book:
        ledger_info = position_log.get_entry_info(p.get("sleeve", ""), p["ticker"])
        p["opened_at"] = ledger_info.get("opened_at")
        p["held_days"] = ledger_info.get("held_days")
        if p.get("entry_price") is None:
            p["entry_price"] = ledger_info.get("entry_price")
        # honest time-stop: count from the ORIGINAL entry (persisted), not this run's recomputed value
        if ledger_info.get("time_stop_by"):
            p["time_stop_by"] = ledger_info["time_stop_by"]

        sleeve = p.get("sleeve", "leadership")
        if sleeve == "conviction":
            thesis_id = p.get("thesis_id", "")
            dec_doc = decisions_by_id.get(thesis_id)
            _synth, _matrix_rows = _synth_map.get(thesis_id, ({}, []))
            facts_for_thesis = {"_matrix_rows": _matrix_rows}
            p["thesis_full"] = thesis_mod.compose(p, dec_doc, _synth, facts_for_thesis)
        else:
            p["thesis_full"] = thesis_mod.compose(p, None, None, None)

    # ———— PIT signal MEMORY: record what the engine SAW + decided, keep-first per (asof, ticker) ——
    # latest.json overwrites in place, so this append-only snapshot is the ONLY faithful record of the
    # day's lens reads + decisions — the substrate the calibration/learning loop will join to realized
    # outcomes once theses resolve (~2026-07-17). Irreversible if skipped; degrade-safe.
    try:
        from brain import signal_history
        # E3 — decision-time LEARNING fields (always-on observability; additive + fail-soft). Compute
        # the per-build SHARED reads ONCE (regime_frame.cycles() + the NW market plane) so the per-name
        # extractor is cheap; a failure here degrades every field to None (never breaks the record).
        try:
            from brain import regime_frame as _rf_e3
            _e3_cycles = _rf_e3.cycles() or {}
        except Exception:  # noqa: BLE001
            _e3_cycles = {}
        try:
            from brain import neural_web_context as _nwc_e3
            _e3_market_plane = _nwc_e3.market_plane() or {}
        except Exception:  # noqa: BLE001
            _e3_market_plane = {}
        _sig: list[dict] = []
        for p in book:
            if p.get("sleeve") == "conviction":
                _syn, _rows = _synth_map.get(p.get("thesis_id"), ({}, []))
                _e3 = _decision_time_learning_fields(
                    p["ticker"], cycles=_e3_cycles, market_plane=_e3_market_plane, synthesis=_syn)
                _sig.append(signal_history.make_record(
                    asof, p["ticker"], sleeve="conviction",
                    decision="held" if p.get("retained") else "sized", regime=regime,
                    synthesis=_syn, rows=_rows, verdict=p.get("verdict"), weight=p.get("weight"),
                    size_stage=p.get("size_stage"), price=p.get("entry_price"),
                    time_stop_by=p.get("time_stop_by"), extra=_e3))
            else:
                _e3 = _decision_time_learning_fields(
                    p["ticker"], cycles=_e3_cycles, market_plane=_e3_market_plane, synthesis=None)
                _sig.append(signal_history.make_record(
                    asof, p["ticker"], sleeve=p.get("sleeve", "leadership"), decision="leadership",
                    regime=regime, verdict=p.get("verdict"), weight=p.get("weight"),
                    price=p.get("entry_price"), extra=_e3))
        for _r in _rejected:
            # keep the rejected record's original synthesis byte-identical (confluence + vetoes only);
            # E3 fields are purely ADDITIVE via extra= (a rejected name that carries a divergence still
            # gets divergence_from_sector inside extra, without changing the base record).
            _r_syn = {"confluence": _r.get("confluence"), "vetoes": _r.get("vetoes") or []}
            _e3 = _decision_time_learning_fields(
                _r["ticker"], cycles=_e3_cycles, market_plane=_e3_market_plane,
                synthesis={**_r_syn, "divergences": _r.get("divergences") or []})
            _sig.append(signal_history.make_record(
                asof, _r["ticker"], sleeve="conviction", decision="rejected", regime=regime,
                synthesis=_r_syn, reason=_r.get("reason"), extra=_e3))
        _n_sig = signal_history.archive(asof, _sig)
        _rl_log(_run_id, "book_step", "signal history archived",
                f"records={_n_sig} (sized/held + leadership + rejected)")
    except Exception as _e:
        _rl_log(_run_id, "decision", "signal history error", f"{_e!r}"[:160])

    # ———— OUTCOME LEDGER: grade resolved theses (prediction vs outcome vs what-it-saw) ————
    # The reliability + lens-edge substrate (complementary to the agent de-confidencing in
    # brain/calibration.py): when a thesis matures (first cohort ~2026-07-17), record prob_correct vs
    # realized hit + the decision-time lens snapshot, so the engine can finally measure whether its
    # confidence is CALIBRATED and which lenses actually predicted. No-op until resolutions; degrade-safe.
    try:
        from brain import outcome_ledger
        _n_ol = outcome_ledger.resolve(asof)
        if _n_ol:
            _rl_log(_run_id, "book_step", "outcome ledger recorded", f"resolutions={_n_ol}")
    except Exception as _e:
        _rl_log(_run_id, "decision", "outcome ledger error", f"{_e!r}"[:160])

    # ———— persist + score + bridge ————
    for p in book:
        store.upsert_position(con, asof, {**p, "size_pct": int(round(p["weight"] * 100)),
                                          "cycle_blocked": 0, "reason": {"sleeve": p["sleeve"]}})
    # grade matured theses against their realized rel-return path → the Brier loop actually accrues
    # skill as positions resolve (was always 'building n=0' with no realized feed). Uses the
    # brain.outcomes realized-returns labeler; then CLOSE the resolved theses so they leave the open
    # set (the append-only ledger would otherwise keep them 'open' forever).
    try:
        from brain import outcomes as _outcomes
        _realized = _outcomes.realized_returns(date.fromisoformat(asof))
    except Exception:  # noqa: BLE001 — labeling is best-effort; never block the build
        _realized = {}
    tr = scorer.track_record(date.fromisoformat(asof), realized=_realized)
    if _realized:
        _by_id = {t["id"]: t for t in ledger.all_theses()}
        for _tid, _rr in _realized.items():
            _th = _by_id.get(_tid)
            if _th and _th.get("status", "open") == "open":
                try:
                    ledger.close(_th["subject"], "resolved", realized=_rr)
                except Exception:
                    pass
    store.save_track_record(con, asof, tr)
    # refresh the empirical calibration from the freshly-resolved outcomes → each agent's
    # confidence multiplier tracks its realized reliability for the NEXT build (safe, bounded,
    # de-confidence-only; inert until an agent has MIN_N resolved decisions).
    try:
        from brain import calibration as _calibration
        _calibration.persist(date.fromisoformat(asof))
    except Exception:  # noqa: BLE001 — calibration refresh is best-effort, never fatal
        pass

    # ———— paper $1M account: TRADE only during market hours, else QUEUE pending ————
    # Doctrine: the desk trades ONLY during the regular US cash session, at the live
    # market price. A build that runs while the market is CLOSED (overnight, weekend,
    # holiday) NEVER books an instantaneous fill at a stale close — it queues PENDING
    # buy orders (estimated at the previous close) that settle at the next open. When a
    # build does run while open, any queued orders fill FIRST, then the book rebalances.
    _market_open = False
    _pending_orders: list = []
    try:
        from collections import defaultdict as _dd
        from portfolio import market_calendar, paper_account
        _market_open = market_calendar.is_open()
        _next_open_day = market_calendar.next_open_day().isoformat()
        _tw: dict = _dd(float)
        for p in book:
            _tw[p["ticker"]] += p.get("weight", 0.0)
        # Price the union of TARGET ∪ HELD ∪ SPY. Without HELD, a name dropped from the target is
        # absent from _prices, so rebalance()'s close-out sells it at avg_cost — a fake zero-P&L exit
        # at a price that never traded (the sibling Brain books already price set(target)|set(held)).
        try:
            _held_now = list((paper_account._load_account().get("positions") or {}).keys())
        except Exception:  # noqa: BLE001
            _held_now = []
        _prices: dict = {}
        for _t in set(_tw) | set(_held_now) | {"SPY"}:
            _px = paper_account._current_price(_t)
            if _px and _px > 0:
                _prices[_t] = _px

        if _market_open:
            _settled = paper_account.fill_pending(_prices, asof)   # settle overnight orders at the open
            paper_account.rebalance(dict(_tw), _prices, asof)      # then move the book at market prices
            paper_account.mark(_prices, asof)
            _rl_log(_run_id, "book_step", "paper account traded (market open)",
                    f"priced={len(_prices)}/{len(_tw) + 1} filled_pending={len(_settled)} positions={len(_tw)}")
        else:
            # market closed → queue the FULL rebalance (sells + buys) at the prev-close estimate,
            # to settle at the next open. queue_orders reads the currently-held positions itself and
            # queues a SELL for every held name the target book reduces or drops (the fix for the
            # flagship book that structurally never sold), plus the buys for entries/top-ups.
            # nav_base=None → the weights size against the CURRENT marked NAV, not the stale $1M
            # inception NAV (the historical sizing bug on a book that has drifted from $1M).
            _pending_orders = paper_account.queue_orders(
                dict(_tw), _prices, asof, nav_base=None,
                fill_after=_next_open_day)
            paper_account.mark(_prices, asof)                      # NAV unchanged; nothing executed
            # tag the book so the dashboard renders each holding as PENDING (fills at next open).
            # Only BUY-side pendings mark a book row as a pending OPEN; a queued sell is an exit of a
            # name that (by construction) may still appear in the book, and must not be shown as an open.
            _pend_by_tk = {o["ticker"]: o for o in _pending_orders if o.get("side") != "sell"}
            for p in book:
                o = _pend_by_tk.get(p["ticker"])
                if o:
                    p["pending"] = True
                    p["status"] = "pending_open"
                    p["est_price"] = o["est_price"]
                    p["fill_after"] = _next_open_day
            _n_sells = sum(1 for o in _pending_orders if o.get("side") == "sell")
            _rl_log(_run_id, "book_step", "orders queued (market closed)",
                    f"pending={len(_pending_orders)} sells={_n_sells} next_open={_next_open_day} "
                    f"priced={len(_prices)}/{len(_tw) + 1}")
    except Exception as _e:
        _rl_log(_run_id, "decision", "paper account error", f"{_e!r}"[:160])
        _prices = {}

    # ———— parallel forward SHADOW BOOKS — leakage-free A/B of decision policies ————
    # Re-derive today's book under counterfactual policies (committee on/off, calibration on/off,
    # alt sizing) from the stored decision inputs, run each as an isolated paper book, and label it
    # forward. No LLM, no look-ahead, never touches prod state. Best-effort.
    try:
        from portfolio import shadow_books as _shadow
        _sb = _shadow.run(asof, prices=_prices, inputs=_shadow_inputs)
        _rl_log(_run_id, "book_step", "shadow books marked",
                f"policies={len(_sb.get('books', {}))}")
    except Exception as _e:
        _rl_log(_run_id, "decision", "shadow books error", f"{_e!r}"[:160])

    # ———— desk-lever A/B (Arm B) — forward shadow of the proposed desk levers ————
    # Re-derive the L1/L2/L3/L4/desk_proxy books from the SAME stored decision inputs + daily prices,
    # each isolated under data/shadow/desk_ab/ (reuses the shadow_books paper-account + grader so the
    # books get a daily NAV row and the ~168-day forward clock ticks). Best-effort + fully isolated;
    # a failure here can NEVER affect prod. Runs right after shadow_books so Arm B accrues each build.
    try:
        from portfolio import desk_ab as _desk_ab
        _da = _desk_ab.run(asof, prices=_prices, inputs=_shadow_inputs)
        _rl_log(_run_id, "book_step", "desk A/B marked",
                f"policies={len(_da.get('books', {}))}")
    except Exception as _e:
        _rl_log(_run_id, "decision", "desk A/B error", f"{_e!r}"[:160])

    # ———— universe-wide forward PREDICTION LOG — the statistical-power unlock ————
    # Log + forward-label a falsifiable rel-return thesis for EVERY name the engine has a directional
    # opinion on (~1,600), not just the owned ~7 → cross-sectional date-clustered edge measurement in
    # weeks, not years. Isolated from the prod ledger; no LLM, no look-ahead. Best-effort.
    try:
        from portfolio import predictions as _pred
        _pc = _pred.record(asof)
        _rl_log(_run_id, "book_step", "prediction log updated",
                f"open={_pc.get('n_open')} resolved={_pc.get('n_resolved')} total={_pc.get('n_total')}")
    except Exception as _e:
        _rl_log(_run_id, "decision", "prediction log error", f"{_e!r}"[:160])

    # ———— off-policy REJECTION log — the desk's NEGATIVE space, forward-graded ————
    # Log every name the gate REJECTED (conviction veto / research hold / committee drop / timing
    # withhold) with the policy's selection propensity, then forward-grade each vs SPY → the veto-regret
    # read ("did the gate veto winners?") and, once ε-exploration is armed, off-policy value estimates.
    # Isolated under data/shadow/rejections/; no LLM, no look-ahead. Best-effort.
    try:
        from portfolio import rejections as _rej
        _rjc = _rej.record(asof, rejected=_rejected, held=research_held, explored=_explored)
        _rl_log(_run_id, "book_step", "rejection log updated",
                f"open={_rjc.get('n_open')} resolved={_rjc.get('n_resolved')} total={_rjc.get('n_total')}")
    except Exception as _e:
        _rl_log(_run_id, "decision", "rejection log error", f"{_e!r}"[:160])

    # ———— E2 DECISION-PROVENANCE ledger — one replayable row per evaluated candidate ————
    # ALWAYS-ON OBSERVABILITY (additive + fail-soft): derive one self-contained provenance row per
    # candidate from data ALREADY computed above (book verdicts, gate_info stage breakdowns, the
    # shadow-book decision inputs' NW/committee/sentinel blocks, research_held + rejects), stamped
    # with the flag-config fingerprint so a build is REPLAYABLE end-to-end. It only READS the finalized
    # decision state and WRITES a jsonl sidecar — it sizes nothing, gates nothing, changes no
    # book/size/verdict, and the shadow-book record it reads stays byte-identical. A write failure is
    # logged + swallowed here and inside the writer, so it can NEVER break the build.
    try:
        from brain import decision_provenance as _dprov, regime_frame as _rf_e2
        try:
            _e2_cycles = _rf_e2.cycles() or {}
        except Exception:  # noqa: BLE001
            _e2_cycles = {}
        _prov_rows = _provenance_rows(
            book=book, gate_info=gate_info, shadow_inputs=_shadow_inputs,
            research_held=research_held, rejected=_rejected, cycles=_e2_cycles)
        _dprov.write(asof, _prov_rows)
        _rl_log(_run_id, "book_step", "decision provenance written",
                f"rows={len(_prov_rows)} flags_hash={_dprov.flags_hash()}")
    except Exception as _e:  # noqa: BLE001 — provenance is observability-only; never break the build
        _rl_log(_run_id, "decision", "decision provenance error", f"{_e!r}"[:160])

    # ———— forward-proof READINESS watcher — pings (persistent dashboard alert) the moment a
    #      forward threshold crosses (calibration n≥min, cross-sectional IC ≥ enough clusters, shadow
    #      books first resolved). Rides this daily heartbeat; once-per-crossing. Best-effort. ————
    try:
        from portfolio import readiness as _readiness
        _rr = _readiness.check_and_record(asof)
        if _rr.get("new"):
            _rl_log(_run_id, "decision", "READINESS crossed", f"newly_ready={_rr['new']}")
    except Exception as _e:
        _rl_log(_run_id, "decision", "readiness check error", f"{_e!r}"[:160])

    store.record_run(con, asof, True, decision["triggers"], sig, datetime.now(timezone.utc).isoformat())

    # ── W8 PUBLISH COMPACTION: the full entry/context reports are IN-MEMORY evidence (research
    # prompt injection, park enrollment, provenance) — the published artifact gets a compact,
    # schema-stable summary instead. This keeps latest.json / the site snapshot from inheriting
    # deep nested report objects (every in-process consumer has already run by this point).
    def _w8_compact(row: dict) -> None:
        try:
            er = row.pop("entry_report", None)
            cr = row.pop("context_report", None)
            if isinstance(er, dict):
                row["entry_check"] = {"verdict": er.get("verdict"),
                                      "score": er.get("entry_score"),
                                      "note": (er.get("notes") or [None])[0]}
            if isinstance(cr, dict):
                row["context_check"] = {"verdict": cr.get("verdict"),
                                        "score": cr.get("context_score"),
                                        "note": (cr.get("reasons") or [None])[0]}
            pk = row.get("park")
            if isinstance(pk, dict):
                row["park"] = {"entry_verdict": pk.get("entry_verdict"),
                               "context_verdict": pk.get("context_verdict")}
        except Exception:  # noqa: BLE001 — compaction is cosmetic; never break the publish
            pass

    for _row in book:
        _w8_compact(_row)
    for _row in _rejected:
        _w8_compact(_row)

    payload = {
        "as_of": asof, "gross": gross, "cash": cash,
        "regime": {"quad": regime["quad"], "quad_name": regime.get("quad_name"),
                   "liquidity_overlay": regime["liquidity_overlay"]},
        "sleeves": {"leadership": round(sum(p["weight"] for p in book if p["sleeve"] == "leadership"), 4),
                    "conviction": round(sum(p["weight"] for p in book if p["sleeve"] == "conviction"), 4),
                    "cash": cash},
        "positions": book, "decisions": decisions, "detectors": fired, "track_record": tr,
        "rejected": _rejected,
        "research_held": research_held,
        "safety": _safety,                  # the consumed risk backtest (drives the de-gross below)
        "safety_overlay": _safety_overlay,  # {gross_mult, cash_added, reasons} actually applied
        "llm_used": bool(_armed_ok),
        # market-hours discipline: whether this book is live-traded or queued for the next open
        "market_status": "open" if _market_open else "closed",
        "pending_orders": _pending_orders,
    }
    paths = write(payload)

    # —— close run log ——
    n_lead = len([p for p in book if p["sleeve"] == "leadership"])
    n_conv = len([p for p in book if p["sleeve"] == "conviction"])
    _rl_log(_run_id, "book_step", "book written",
            f"leadership={n_lead} conviction={n_conv} gross={gross} cash={cash}")
    try:
        from brain import runlog
        if _run_id:
            runlog.end_run(_run_id,
                           summary=(f"quad={regime.get('quad_name')} gross={gross:.0%} cash={cash:.0%} "
                                    f"leaders={n_lead} conviction={n_conv} rejected={len(_rejected)}"))
    except Exception:
        pass

    _run_out = {"ran": True, "triggers": decision["triggers"], "book": book,
                "sleeves": payload["sleeves"],
                "detectors": fired, "track_record": tr, "paths": paths,
                "llm_used": payload["llm_used"],
                "safety": _safety, "safety_overlay": _safety_overlay,
                "research": research_out, "research_held": research_held, "run_id": _run_id,
                "stale_freeze": _stale_freeze_summary,
                "asof": asof, "gross": gross, "currency": "USD"}

    # ── MW5: mandate-compliance packet (ADVISORY ONLY — never gates) ──────
    try:
        from portfolio import mandate_packet as _mp
        _pkt = _mp.build("flagship", _run_out)
        _run_out["mandate_packet"] = _pkt
        _mp.write_packet(_pkt, "flagship")
        _mp.emit_run_event(_pkt, "flagship", job="phase2_daily")
    except Exception:  # noqa: BLE001
        pass

    return _run_out


def run_flagship(asof: str | None = None, *, directive: str | None = None) -> dict:
    """Overnight watch entrypoint for the Flagship book — mirrors the ``run_autonomous`` /
    ``run_etf`` signature so ``bot/overnight._RUNNERS`` can invoke it uniformly.

    Thin wrapper over :func:`run` that adapts the return dict to the shape the overnight
    watch loop expects: ``decided`` (bool), ``queued_for_open`` (bool), ``brain`` (dict).
    When ``directive`` is set the build gate is forced open (overnight tape already passed
    materiality) and the directive is threaded into the judgment layer if armed.  When the
    flag ``MASTERMIND_FLAGSHIP_JUDGMENT`` is OFF the deterministic engine just rebuilds with
    fresh regime/severity inputs — the directive has no additive effect, but the refresh is
    still valuable: the dwell/severity state can cut the book overnight.

    Never raises."""
    from portfolio import paper_account
    try:
        res = run(asof=asof, directive=directive)
        ran = bool(res.get("ran"))
        # A flagship overnight tick queues (market is closed); check for a pending target.
        has_pending = bool(paper_account.load_pending_target("flagship"))
        return {
            "decided": ran,
            "queued_for_open": has_pending,
            # brain is the judgment path's LLM result; surface llm_used as a proxy.
            "brain": {"ok": ran, "llm_used": res.get("llm_used")},
            "holdings": len(res.get("book") or []),
        }
    except Exception as _e:  # noqa: BLE001 — overnight misses must never kill the scheduler
        return {"decided": False, "queued_for_open": False,
                "brain": {"ok": False, "error": repr(_e)[:200]}, "holdings": 0}


if __name__ == "__main__":
    out = run()
    if not out["ran"]:
        print("Gate: carried forward —", out["reason"])
    else:
        print("\n=== PHASE 2 — gated multi-name 3-sleeve book ===")
        print("triggers   :", out["triggers"], "| llm:", out["llm_used"])
        print("sleeves    :", out["sleeves"])
        print("book:")
        for p in sorted(out["book"], key=lambda x: -x["weight"]):
            print(f"  {p['ticker']:6} {p['sleeve']:11} w={p['weight']:.3f}  {p['verdict']}"
                  + (f"  confluence={p.get('confluence'):+.2f} (all sides confirm)" if p["sleeve"] == "conviction" else ""))
        print("detectors  :", [f"{d['code']}/{d['mode']}/{d['severity']}" for d in out["detectors"]] or "none")
        print("track rec  :", out["track_record"])
        print("bridge     :", out["paths"]["hub"])
        print("run_id     :", out.get("run_id"))
