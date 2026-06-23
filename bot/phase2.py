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


def run(asof: str | None = None, force: bool = False, research: bool = False) -> dict:
    # —— open run log ——
    _run_id: str | None = None
    try:
        from brain import runlog
        _run_id = runlog.start_run("book", title="phase2 book build")
        _rl_log(_run_id, "book_step", "phase2 start",
                f"asof={asof} force={force} research={research}")
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

    con = store.connect()
    sig = gate.state_signature(regime, top_sector)
    decision = gate.should_run(sig, store.last_run(con),
                               interval_days=int(cfg["scorecard"].get("rebuild_interval_days", 1)),
                               force=force, asof=asof)
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
                                                                   reason="hard_exit_sweep"):
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
    lead_budget = sum(cfg["sleeves"]["leadership_target"]) / 2
    leaders = [s for s in secrs[:6] if s.get("above_200d_trend")][:4]
    lw = round(lead_budget / max(1, len(leaders)), 4)
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

    # ———— CONVICTION sleeve ————
    from portfolio import conviction
    conv_budget = sum(cfg["sleeves"]["conviction_target"]) / 2
    decisions = []
    _synth_map: dict[str, tuple[dict, list]] = {}
    # currently-held conviction names get sector-cap priority (hysteresis) so the book doesn't
    # churn a name in and out across builds (the NVDA in/out problem).
    _held_conv = {p["ticker"] for p in position_log.open_positions()
                  if p.get("sleeve") == "conviction"}
    _build_result = conviction.build(conv_budget, name_cap=cfg["caps"]["name_cap"], held=_held_conv)
    # conviction.build returns (sized_list, rejected_list) as a tuple
    if isinstance(_build_result, tuple) and len(_build_result) == 2:
        sized, _rejected = _build_result
    else:
        # legacy: single list returned
        sized = list(_build_result)
        _rejected = []


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
        _shadow_inputs.append({
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
        })

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
            paper = rpaper.generate(t, asof=asof, confluence=c["confluence"], rows=_rows,
                                    vetoes=_syn.get("vetoes", []), price=px, regime=regime,
                                    armed=(_armed_ok and is_new))
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
    sized = confirmed_sized
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
        doc = DecisionDoc(
            id=f"{asof}-{t}-conv", subject=t, lean=_lean, conviction="medium",
            prob_correct=_pc, raw_prob_correct=_raw_pc,
            horizon_d=21, state_asof=asof, sleeve="conviction", order_layer=1,
            thesis=_thesis,
            evidence=_evidence
                     + ([f"divergence:{d}" for d in c["divergences"]] if c["divergences"] else []),
            dissent="Held at 0 if any side vetoes (parabolic/distress/cycle-blocked).",
            entry_levels={"ticker": t, "price": px} if px else {"ticker": t},
        ).finalize()
        _synth_map[doc.id] = (_synth, _matrix_rows)
        decisions.append(doc.to_json())
        ledger.append(doc.to_json())
        store.insert_thesis(con, doc.to_json())
        book.append({"ticker": t, "theme_id": _conv_theme_id(t), "sleeve": "conviction", "stage": 2,
                     "weight": c["weight"], "verdict": _verdict, "thesis_id": doc.id,
                     "time_stop_by": doc.time_stop_by, "confluence": c["confluence"],
                     "entry_price": px, "research": c.get("research"), "retained": is_retained,
                     "size_stage": c.get("size_stage")})
        _rl_log(_run_id, "trade", f"sized {t} conviction",
                f"ticker={t} weight={c['weight']} confluence={c['confluence']:+.2f} "
                f"bull={c['bull']} bear={c['bear']} price={px} verdict={_verdict}",
                ticker=t, sleeve="conviction", weight=c["weight"],
                confluence=c["confluence"], verdict=_verdict)

    # ———— cross-sleeve firebreaks ————
    capped = enforce_book_caps(book)
    book = capped["positions"]

    # ———— D5 dead-capital time-stop — an ACTUAL exit (doctrine: D5/self is a hard sizing veto) ——
    # Flag conviction lots that are past their time_stop_by AND flat/negative since entry AND lagging
    # the leading sector, then REDEPLOY — drop them from the book and close their thesis so the
    # capital is freed (was unwired with unpopulated inputs, so it could never fire).
    _d5_fired: list[dict] = []
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

    # ———— safety overlay: SUBTRACT-ONLY de-gross of a fragile book (CONSUMED, not display) ————
    # Measure the proposed book's risk (static-weight historical sim; local prices only so the
    # build stays fast + deterministic) and, if it is fragile (deep drawdown / high beta /
    # one-factor concentration / low score), scale every position down — the freed weight
    # becomes cash. This is the safety read actually CHANGING the book, never levering it up.
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

    # ———— update positions ledger ————
    position_log.update(book, asof)

    # close ledger theses for conviction names that LEFT the book this run — the append-only ledger
    # otherwise keeps the old thesis 'open' forever (blocking any re-proposal and accreting stale
    # names in the open-thesis candidate pool).
    _final_conv = {p["ticker"] for p in book if p.get("sleeve") == "conviction"}
    for _dropped in (_held_conv - _final_conv):
        try:
            ledger.close(_dropped, "exited (left book)")
        except Exception:
            pass

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
        _sig: list[dict] = []
        for p in book:
            if p.get("sleeve") == "conviction":
                _syn, _rows = _synth_map.get(p.get("thesis_id"), ({}, []))
                _sig.append(signal_history.make_record(
                    asof, p["ticker"], sleeve="conviction",
                    decision="held" if p.get("retained") else "sized", regime=regime,
                    synthesis=_syn, rows=_rows, verdict=p.get("verdict"), weight=p.get("weight"),
                    size_stage=p.get("size_stage"), price=p.get("entry_price"),
                    time_stop_by=p.get("time_stop_by")))
            else:
                _sig.append(signal_history.make_record(
                    asof, p["ticker"], sleeve=p.get("sleeve", "leadership"), decision="leadership",
                    regime=regime, verdict=p.get("verdict"), weight=p.get("weight"),
                    price=p.get("entry_price")))
        for _r in _rejected:
            _sig.append(signal_history.make_record(
                asof, _r["ticker"], sleeve="conviction", decision="rejected", regime=regime,
                synthesis={"confluence": _r.get("confluence"), "vetoes": _r.get("vetoes") or []},
                reason=_r.get("reason")))
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
        _prices: dict = {}
        for _t in set(_tw) | {"SPY"}:
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
            # market closed → queue buys at the prev-close estimate, fill at next open. No sells.
            _pending_orders = paper_account.queue_orders(
                dict(_tw), _prices, asof, nav_base=paper_account._STARTING_NAV,
                fill_after=_next_open_day)
            paper_account.mark(_prices, asof)                      # NAV unchanged; nothing executed
            # tag the book so the dashboard renders each holding as PENDING (fills at next open)
            _pend_by_tk = {o["ticker"]: o for o in _pending_orders}
            for p in book:
                o = _pend_by_tk.get(p["ticker"])
                if o:
                    p["pending"] = True
                    p["status"] = "pending_open"
                    p["est_price"] = o["est_price"]
                    p["fill_after"] = _next_open_day
            _rl_log(_run_id, "book_step", "orders queued (market closed)",
                    f"pending={len(_pending_orders)} next_open={_next_open_day} priced={len(_prices)}/{len(_tw) + 1}")
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

    return {"ran": True, "triggers": decision["triggers"], "book": book, "sleeves": payload["sleeves"],
            "detectors": fired, "track_record": tr, "paths": paths, "llm_used": payload["llm_used"],
            "safety": _safety, "safety_overlay": _safety_overlay,
            "research": research_out, "research_held": research_held, "run_id": _run_id}


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
