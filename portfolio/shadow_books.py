"""Parallel forward shadow books — the leakage-free way to A/B decision policies.

The single live book learns slowly and can't tell you *which layer* of the stack earned its keep.
This module runs N counterfactual paper books on the SAME live data, each under a different policy,
and labels them FORWARD. Because every book is re-derived purely from prod's stored decision inputs
(`data/shadow/inputs/<asof>.json`) — no LLM is re-invoked and no future price is ever read at
decision time — the comparison is free of **parametric leakage** (the model already knows how the
past played out, so historical LLM replay is contaminated; a forward shadow book is not).

Policies isolate one layer each, so the leaderboard reads as an attribution:
  * prod            — the full live stack (engine + research + committee + calibration).
  * no_committee    — drop the blind SENTINEL→NEXUS overlay (what did the committee cost/save?).
  * no_calibration  — committee on, but re-derive NEXUS from SENTINEL's RAW confidence + grade Brier
                      on raw prob_correct (what does the calibration loop actually contribute?).
  * engine_only     — size on raw engine confluence; no research size-multiplier, no committee.

Safety: each book is fully ISOLATED under `data/shadow/books/<id>/` (its own account + NAV history +
thesis ledger). It NEVER imports/writes prod's singletons (paper_account state, brain ledger, store).
Only the read-only price accessor is reused. Best-effort throughout — never breaks the build.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SHADOW = _ROOT / "data" / "shadow"
_INPUTS = _SHADOW / "inputs"
_BOOKS = _SHADOW / "books"
_LEADERBOARD = _SHADOW / "leaderboard.json"

_STARTING_NAV = 1_000_000.0

# Each policy maps a stored decision-input record → a target weight. `sizing`: 'research' starts from
# the research-scaled FORGE weight; 'engine' uses the raw confluence weight. `committee`/`calibration`
# toggle those layers. Order = display order; 'prod' first (the baseline everything is compared to).
POLICIES = [
    {"id": "prod", "label": "Prod — full stack", "label_zh": "实盘 — 全栈",
     "desc": "Engine + research + committee + calibration (the live policy).",
     "desc_zh": "引擎 + 研究 + 委员会 + 校准（实盘策略）。",
     "committee": True, "calibration": True, "sizing": "research"},
    {"id": "no_committee", "label": "No committee", "label_zh": "无委员会",
     "desc": "FORGE-confirmed names at research size; skips the SENTINEL→NEXUS overlay.",
     "desc_zh": "按研究规模买入 FORGE 确认的标的；跳过 SENTINEL→NEXUS 叠加。",
     "committee": False, "calibration": True, "sizing": "research"},
    {"id": "no_calibration", "label": "No calibration", "label_zh": "无校准",
     "desc": "Committee on, but re-derived from SENTINEL's raw (un-shrunk) confidence.",
     "desc_zh": "启用委员会，但基于 SENTINEL 的原始（未收缩）置信度重新推导。",
     "committee": True, "calibration": False, "sizing": "research"},
    {"id": "engine_only", "label": "Engine-only sizing", "label_zh": "仅引擎规模",
     "desc": "Raw engine-confluence sizing; no research multiplier, no committee.",
     "desc_zh": "原始引擎合流规模；无研究乘数，无委员会。",
     "committee": False, "calibration": True, "sizing": "engine"},
    {"id": "risk_tilt", "label": "Risk-tilt (low-vol overlay)", "label_zh": "风险倾斜（低波动叠加）",
     "desc": "Prod book + a SUBTRACT-ONLY vol-managed / anti-lottery size haircut — the forward test "
             "of the only backtest-confirmed edge (low-risk is a Sharpe/drawdown lever, not a picker).",
     "desc_zh": "实盘组合 + 仅做减法的波动管理/反彩票规模折减 — 对唯一经回测确认的边际的前瞻检验"
                "（低风险是夏普/回撤杠杆，而非选股器）。",
     "committee": True, "calibration": True, "sizing": "research", "risk_tilt": True},
    # ── W-L / L1: the two counterfactuals the incident audit proved UNPRICEABLE until now ──
    {"id": "do_nothing", "label": "Do-nothing (carry)", "label_zh": "按兵不动（结转）",
     "desc": "Yesterday's positions carried forward untouched — the cost of churn. Never trades on a "
             "new signal; only marks forward.",
     "desc_zh": "结转昨日持仓、原封不动 — 换手的代价。不因新信号交易，仅前瞻标记。",
     "synthetic": "do_nothing"},
    {"id": "defensive", "label": "Defensive basket (XLU/XLV/XLF/XLP)", "label_zh": "防御篮子",
     "desc": "The user's static equal-weight defensive sleeve (utilities/healthcare/financials/"
             "staples) — the benchmark the semis unwind proved beat the Brain.",
     "desc_zh": "用户的静态等权防御板块（公用/医疗/金融/必需消费）— 半导体崩盘中战胜大脑的基准。",
     "synthetic": "defensive"},
]

# The static equal-weight defensive basket (mirror of brain.benchmark_ledger.DEFENSIVE_BASKET —
# one defensive pool, charter P7). The `defensive` arm rebalances to this each run.
_DEFENSIVE_BASKET = ["XLU", "XLV", "XLF", "XLP"]

_POLICY_BY_ID = {p["id"]: p for p in POLICIES}


# ─────────────────────────────────────────────────────────────────────────────
# decision inputs (written by bot.phase2)
# ─────────────────────────────────────────────────────────────────────────────
def write_inputs(asof: str, inputs: list) -> None:
    """Persist today's per-candidate decision inputs (audit + standalone replay). Never raises."""
    try:
        _INPUTS.mkdir(parents=True, exist_ok=True)
        (_INPUTS / f"{str(asof)[:10]}.json").write_text(json.dumps(inputs, indent=2, default=str))
    except Exception:  # noqa: BLE001
        pass


def read_inputs(asof: str) -> list:
    try:
        return json.loads((_INPUTS / f"{str(asof)[:10]}.json").read_text())
    except Exception:  # noqa: BLE001
        return []


# ─────────────────────────────────────────────────────────────────────────────
# policy → target weights (pure)
# ─────────────────────────────────────────────────────────────────────────────
def _policy_weight(policy: dict, r: dict) -> float:
    """Target weight this policy assigns to one decision-input record. Pure + deterministic."""
    if not r.get("forge_confirmed"):
        return 0.0
    if policy["sizing"] == "engine":                       # raw confluence size, no overlay
        base = r.get("base_weight") or 0.0
        cap = r.get("name_cap") or 0.20
        return round(min(cap, base), 4)
    wf = r.get("weight_forge") or 0.0                      # research-scaled, pre-committee
    if not policy["committee"]:
        return round(wf, 4)
    comm = r.get("committee")
    if not comm:                                           # committee didn't run for this name
        wp = r.get("weight_prod")
        return round(float(wp if wp is not None else wf), 4)
    if policy["calibration"]:                              # exactly what prod did (committee+calib on)
        return round(float(r.get("weight_prod") or 0.0), 4)
    # calibration OFF but committee ON → re-derive NEXUS from SENTINEL's RAW confidence (pure fn)
    sent = r.get("sentinel") or {}
    raw = sent.get("raw_confidence")
    if not sent or raw is None:
        return round(float(r.get("weight_prod") or 0.0), 4)
    try:
        from brain import committee as _committee
        d = _committee.nexus({"confirmed": True, "combined": r.get("combined")},
                             {**sent, "confidence": raw})
        return round(wf * float(d.get("scale", 1.0)), 4)
    except Exception:  # noqa: BLE001
        return round(float(r.get("weight_prod") or 0.0), 4)


def apply_policy(policy: dict, inputs: list) -> dict:
    """{ticker: target_weight} for a policy over the day's inputs. Long-only, no leverage:
    if gross exceeds 1.0 the book is scaled to fully-invested (so books stay comparable)."""
    raw: dict = {}
    for r in inputs or []:
        tk = (r.get("ticker") or "").upper()
        if not tk:
            continue
        w = _policy_weight(policy, r)
        if w and w > 0:
            raw[tk] = round(raw.get(tk, 0.0) + w, 4)
    total = sum(raw.values())
    if total > 1.0:
        raw = {k: round(v / total, 4) for k, v in raw.items()}
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# isolated per-book paper account (NEVER touches prod state)
# ─────────────────────────────────────────────────────────────────────────────
def _book_dir(book_id: str) -> Path:
    return _BOOKS / book_id


def _fresh_account() -> dict:
    return {"inception_date": None, "starting_nav": _STARTING_NAV,
            "cash": _STARTING_NAV, "positions": {}}


def _load_account(book_id: str) -> dict:
    try:
        raw = json.loads((_book_dir(book_id) / "account.json").read_text())
        if isinstance(raw.get("cash"), (int, float)) and isinstance(raw.get("positions"), dict):
            return raw
    except Exception:  # noqa: BLE001
        pass
    return _fresh_account()


def _save_account(book_id: str, acct: dict) -> None:
    d = _book_dir(book_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "account.json").write_text(json.dumps(acct, indent=2, default=str))


def _mark_to(prices: dict, positions: dict) -> float:
    """Mark held positions: a live price if available, else the avg cost (so a missing quote can't
    swing NAV) — mirrors the prod paper_account convention."""
    inv = 0.0
    for t, p in positions.items():
        px = prices.get(t)
        if not px or px <= 0:
            px = p.get("avg_cost") or 0.0
        inv += (p.get("shares") or 0.0) * px
    return inv


def _rebalance(book_id: str, targets: dict, prices: dict, asof: str) -> None:
    acct = _load_account(book_id)
    if not acct.get("inception_date"):
        acct["inception_date"] = asof
    positions = acct.setdefault("positions", {})
    nav = acct.get("cash", 0.0) + _mark_to(prices, positions)
    if nav <= 0:
        nav = _STARTING_NAV
    for t in set(targets) | set(positions):
        px = prices.get(t)
        cur = positions.get(t, {"shares": 0.0, "avg_cost": 0.0})
        cur_sh = cur.get("shares") or 0.0
        if not px or px <= 0:                              # can't trade without a mark — hold as-is
            continue
        tgt_sh = (targets.get(t, 0.0) * nav) / px
        delta = tgt_sh - cur_sh
        acct["cash"] = acct.get("cash", 0.0) - delta * px
        if tgt_sh <= 1e-9:
            positions.pop(t, None)
            continue
        if delta > 0:                                      # weighted avg cost on adds; trims keep it
            tot = cur_sh * (cur.get("avg_cost") or px) + delta * px
            cur["avg_cost"] = round(tot / tgt_sh, 6)
        cur["shares"] = round(tgt_sh, 6)
        positions[t] = cur
    _save_account(book_id, acct)


def _append_nav(book_id: str, row: dict, asof: str) -> None:
    """Append a NAV snapshot, idempotent per date (re-running a build replaces that day's row)."""
    d = _book_dir(book_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "nav_history.jsonl"
    rows = []
    try:
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    except Exception:  # noqa: BLE001
        rows = []
    rows = [x for x in rows if x.get("date") != asof]
    rows.append(row)
    path.write_text("".join(json.dumps(x, default=str) + "\n" for x in rows))


def _mark(book_id: str, prices: dict, asof: str) -> dict:
    acct = _load_account(book_id)
    positions = acct.get("positions", {})
    invested = _mark_to(prices, positions)
    nav = acct.get("cash", 0.0) + invested
    row = {"date": asof, "nav": round(nav, 2), "cash": round(acct.get("cash", 0.0), 2),
           "invested": round(invested, 2), "spy_px": prices.get("SPY")}
    _append_nav(book_id, row, asof)
    return row


def _nav_rows(book_id: str) -> list:
    try:
        rows = [json.loads(l) for l in (_book_dir(book_id) / "nav_history.jsonl")
                .read_text().splitlines() if l.strip()]
        return sorted(rows, key=lambda x: x.get("date", ""))
    except Exception:  # noqa: BLE001
        return []


# ─────────────────────────────────────────────────────────────────────────────
# per-book thesis ledger (for forward Brier/hit-rate) — isolated mini-ledger
# ─────────────────────────────────────────────────────────────────────────────
def _load_theses(book_id: str) -> list:
    try:
        return [json.loads(l) for l in (_book_dir(book_id) / "theses.jsonl")
                .read_text().splitlines() if l.strip()]
    except Exception:  # noqa: BLE001
        return []


def _save_theses(book_id: str, theses: list) -> None:
    d = _book_dir(book_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "theses.jsonl").write_text("".join(json.dumps(t, default=str) + "\n" for t in theses))


def _make_thesis(policy: dict, r: dict, asof: str) -> dict | None:
    """A schema-identical DecisionDoc thesis so brain.outcomes.label_thesis can grade it exactly like
    prod. prob_correct is de-confidenced for calibration-on policies, raw for calibration-off."""
    try:
        from brain.decision import DecisionDoc
        raw_pc = float(r.get("raw_prob_correct") or 0.55)
        mult = 1.0
        if policy["calibration"]:
            try:
                from brain import calibration as _cal
                mult = _cal.multiplier("forge")
            except Exception:  # noqa: BLE001
                mult = 1.0
        px = r.get("price")
        doc = DecisionDoc(
            id=f"{asof}-{r['ticker']}-{policy['id']}", subject=r["ticker"], lean="add",
            conviction="medium", prob_correct=round(raw_pc * mult, 2), raw_prob_correct=raw_pc,
            horizon_d=int(r.get("horizon_d") or 21), state_asof=asof, sleeve="conviction",
            thesis=f"shadow:{policy['id']}",
            entry_levels={"ticker": r["ticker"], "price": px} if px else {"ticker": r["ticker"]},
        ).finalize().to_json()
        doc["status"] = "open"
        return doc
    except Exception:  # noqa: BLE001
        return None


def _update_theses(book_id: str, policy: dict, inputs: list, targets: dict, asof: str) -> list:
    """Open one thesis per newly-bought name (deduped while open, like prod's ledger), then label
    every open thesis forward and resolve the matured ones. Returns the full ledger."""
    theses = _load_theses(book_id)
    open_subj = {t.get("subject") for t in theses if t.get("status", "open") == "open"}
    by_ticker = {(r.get("ticker") or "").upper(): r for r in inputs or []}
    for tk, w in targets.items():
        if w > 0 and tk not in open_subj:
            r = by_ticker.get(tk)
            if not r:
                continue
            th = _make_thesis(policy, r, asof)
            if th:
                theses.append(th)
                open_subj.add(tk)
    try:
        from brain import outcomes
        asof_d = date.fromisoformat(str(asof)[:10])
        for t in theses:
            if t.get("status", "open") != "open":
                continue
            lab = outcomes.label_thesis(t, asof_d)
            if lab and lab.get("resolved") and lab.get("rel_return") is not None:
                t["status"] = "resolved"
                t["realized"] = lab["rel_return"]
                t["barrier"] = lab.get("barrier")
    except Exception:  # noqa: BLE001
        pass
    _save_theses(book_id, theses)
    return theses


def _brier(rows: list) -> float | None:
    graded = [r for r in rows if r.get("prob_correct") is not None and "outcome" in r]
    if not graded:
        return None
    return round(sum((r["prob_correct"] - r["outcome"]) ** 2 for r in graded) / len(graded), 4)


def _book_summary(policy: dict, theses: list, account: dict, nav_rows: list) -> dict:
    resolved = [t for t in theses if t.get("status") == "resolved" and t.get("realized") is not None]
    # grade EXACTLY as brain.scorer.track_record does — a thesis is a hit if it was NOT falsified by
    # its own threshold predicate (a bullish thesis with rel_return −2% is within the −5% tolerance →
    # a hit), NOT merely if rel_return > 0. hit_rate is derived from the same outcome rows the Brier
    # uses, so the two can never disagree and the shadow 'prod' book mirrors the live track record.
    rows = []
    for t in resolved:
        chk = (t.get("falsifier") or {}).get("check") or {}
        op, thr = chk.get("op", "<"), chk.get("threshold", -0.05)
        falsified = (t["realized"] < thr) if op == "<" else (t["realized"] > thr)
        rows.append({"prob_correct": t.get("prob_correct"), "outcome": 0 if falsified else 1})
    n_hits = sum(r["outcome"] for r in rows)
    last = nav_rows[-1] if nav_rows else None
    first = nav_rows[0] if nav_rows else None
    nav = last["nav"] if last else _STARTING_NAV
    ret = nav / _STARTING_NAV - 1.0
    spy_ret = None
    if first and last and first.get("spy_px") and last.get("spy_px"):
        try:
            spy_ret = last["spy_px"] / first["spy_px"] - 1.0
        except Exception:  # noqa: BLE001
            spy_ret = None
    held = sorted(t for t, p in account.get("positions", {}).items() if (p.get("shares") or 0) > 0)
    return {
        "id": policy["id"], "label": policy["label"], "label_zh": policy.get("label_zh"),
        "desc": policy.get("desc"), "desc_zh": policy.get("desc_zh"),
        "is_baseline": policy["id"] == "prod",
        "nav": round(nav, 2), "return_pct": round(ret * 100, 2),
        "spy_return_pct": round(spy_ret * 100, 2) if spy_ret is not None else None,
        "vs_spy_pct": round((ret - spy_ret) * 100, 2) if spy_ret is not None else None,
        "n_positions": len(held), "holdings": held,
        "n_theses": len(theses), "n_resolved": len(resolved),
        "hit_rate": round(n_hits / len(rows), 3) if rows else None,
        "avg_rel_return": round(sum(t["realized"] for t in resolved) / len(resolved), 4) if resolved else None,
        "brier": _brier(rows), "n_marks": len(nav_rows),
        "inception": account.get("inception_date"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# driver
# ─────────────────────────────────────────────────────────────────────────────
def _gather_prices(tickers: set, seed: dict | None, asof: str | None = None) -> dict:
    """Mark every needed symbol through the ONE marking layer (portfolio.marks, W-L / L1) so the
    do-nothing + defensive arms — and every policy arm — read the same price every other book does
    (polygon-EOD → yahoo-parquet → last-good-carry, never avg_cost). Any symbol `marks` can't price
    falls back to the legacy paper_account accessor, and `seed` (test/shared fetch) always wins."""
    px = {(k or "").upper(): v for k, v in (seed or {}).items() if v and v > 0}
    want = {(t or "").upper() for t in tickers} | {"SPY", *(_DEFENSIVE_BASKET)}
    want = {t for t in want if t}
    try:
        from portfolio import marks
        marked = marks.prices_for(want, asof or str(date.today()), seed=px, persist=False)
        for t, v in marked.items():
            if t not in px and v and v > 0:
                px[t] = v
    except Exception:  # noqa: BLE001
        pass
    try:
        from portfolio import paper_account
        for t in want:
            if t not in px or not px.get(t):
                q = paper_account._current_price(t)
                if q and q > 0:
                    px[t] = q
    except Exception:  # noqa: BLE001
        pass
    return px


# ─────────────────────────────────────────────────────────────────────────────
# risk-tilt overlay — the forward test of the backtest-confirmed low-risk edge
# ─────────────────────────────────────────────────────────────────────────────
_RT_TARGET_VOL = 0.25      # annualized vol target for the vol-managed leg
_RT_WIN = 63              # trailing business days for realized vol / max-daily-return
_RT_FLOOR = 0.30          # never haircut a name below 30% of its prod size


def _risk_mult(panel: dict, ticker: str, asof: str) -> float:
    """SUBTRACT-ONLY (≤1.0) size multiplier for one name: vol-managed (min(1, target/realized_vol))
    × an anti-lottery haircut for big single-day pops. Computed ONLY from prices ≤ asof (leakage-free).
    A name absent from the panel keeps full size (1.0)."""
    try:
        import numpy as np
        import pandas as pd
        s = (panel or {}).get((ticker or "").upper())
        if s is None:
            return 1.0
        s = s[s.index <= pd.Timestamp(asof)]
        r = s.pct_change().dropna().iloc[-_RT_WIN:]
        if len(r) < 20:
            return 1.0
        vol = float(r.std()) * (252 ** 0.5)
        vol_mult = min(1.0, _RT_TARGET_VOL / vol) if vol > 0 else 1.0
        maxret = float(r.max())
        lottery_mult = max(0.6, 1.0 - 2.0 * max(0.0, maxret - 0.10))   # >10% pop → progressive haircut
        return round(max(_RT_FLOOR, min(1.0, vol_mult * lottery_mult)), 4)
    except Exception:  # noqa: BLE001
        return 1.0


def _synthetic_carry_targets(book_id: str, inputs: list) -> dict:
    """The do-nothing arm's target = whatever it already holds (marked forward, never traded).

    Cold-start: an empty do-nothing book inherits PROD's target weights on the first day prod has a
    non-empty allocation, so it has a real inception snapshot to carry (a do-nothing book that starts
    empty and never trades would carry $0 of positions forever). After that inception it NEVER changes
    its targets — it just re-marks its held weights each run (held_value / nav)."""
    acct = _load_account(book_id)
    positions = acct.get("positions") or {}
    if positions:                                          # already seeded → carry current holdings
        nav = acct.get("cash", 0.0) + sum(
            (p.get("shares") or 0.0) * (p.get("avg_cost") or 0.0) for p in positions.values())
        if nav <= 0:
            return {t: 0.0 for t in positions}
        # target = current dollar weight at avg_cost (a mark-agnostic snapshot; _rebalance re-solves
        # shares at live prices but the delta is ~0 since these ARE the held shares).
        return {t: round((p.get("shares") or 0.0) * (p.get("avg_cost") or 0.0) / nav, 4)
                for t, p in positions.items()}
    # cold-start inception: adopt prod's weights so there is something to freeze
    prod_targets = apply_policy(_POLICY_BY_ID["prod"], inputs)
    return prod_targets


def run(asof: str, prices: dict | None = None, inputs: list | None = None) -> dict:
    """Re-derive every policy book for `asof`, mark each forward, label its theses, and write the
    leaderboard. `inputs` defaults to the persisted decision-inputs file. Best-effort; never raises."""
    asof = str(asof)[:10]
    inputs = inputs if inputs is not None else read_inputs(asof)
    targets_by_book = {p["id"]: apply_policy(p, inputs)
                       for p in POLICIES if not p.get("synthetic")}
    # ── W-L / L1 synthetic arms — targets are NOT derived from decision inputs ──
    # do_nothing: carry whatever this arm already holds (targets == current held weights → no trade,
    #             just a forward mark). On a fresh book with no positions it stays all-cash until it
    #             inherits prod's inception snapshot on its first non-empty run.
    # defensive:  a static equal-weight basket, rebalanced back to EW each run.
    for p in POLICIES:
        syn = p.get("synthetic")
        if not syn:
            continue
        if syn == "do_nothing":
            targets_by_book[p["id"]] = _synthetic_carry_targets(p["id"], inputs)
        elif syn == "defensive":
            targets_by_book[p["id"]] = {t: round(1.0 / len(_DEFENSIVE_BASKET), 4)
                                        for t in _DEFENSIVE_BASKET}
    # risk_tilt = prod weights scaled by a leakage-free, subtract-only risk-quality multiplier, then
    # re-capped. Gross only SHRINKS (more cash) → it's the forward A/B of the low-risk Sharpe lever.
    if "risk_tilt" in targets_by_book and targets_by_book["risk_tilt"]:
        try:
            from portfolio import predictions as _pred
            _panel = _pred._load_panel()
            rt = {tk: round(w * _risk_mult(_panel, tk, asof), 4)
                  for tk, w in targets_by_book["risk_tilt"].items()}
            tot = sum(rt.values())
            if tot > 1.0:
                rt = {k: round(v / tot, 4) for k, v in rt.items()}
            targets_by_book["risk_tilt"] = rt
        except Exception:  # noqa: BLE001
            pass
    needed: set = set()
    for tw in targets_by_book.values():
        needed |= set(tw)
    for p in POLICIES:                                     # also re-mark names still held
        needed |= set(_load_account(p["id"]).get("positions", {}))
    px = _gather_prices(needed, prices, asof)
    # GUARD against the empty-inputs LIQUIDATION trap: on a day that carried NO decision input (a
    # quiet/carried flagship day → inputs==[]) every policy's targets are {}, and an unguarded
    # _rebalance would drive every held position to zero — wiping the forward A/B books. On such a
    # day we HOLD existing positions and only re-mark/relabel forward. A genuine all-cash signal
    # (inputs PRESENT but nothing forge-confirmed) still liquidates: `targets or _has_inputs`.
    _has_inputs = bool(inputs)
    books = {}
    for p in POLICIES:
        bid = p["id"]
        targets = targets_by_book[bid]
        try:
            if targets or _has_inputs:
                _rebalance(bid, targets, px, asof)
            nav_rows = _nav_rows(bid)
            row = _mark(bid, px, asof)
            nav_rows = [x for x in nav_rows if x.get("date") != asof] + [row]
            theses = _update_theses(bid, p, inputs, targets, asof)
            books[bid] = _book_summary(p, theses, _load_account(bid), nav_rows)
        except Exception:  # noqa: BLE001 — one book failing must not sink the others
            continue
    result = {"as_of": asof, "books": books, "leaderboard": leaderboard(books)}
    try:
        _SHADOW.mkdir(parents=True, exist_ok=True)
        _LEADERBOARD.write_text(json.dumps(result, indent=2, default=str))
    except Exception:  # noqa: BLE001
        pass
    return result


def leaderboard(books: dict) -> list:
    """Books ranked by forward return, each annotated with how it diverges from prod's holdings."""
    prod = books.get("prod") or {}
    prod_held = set(prod.get("holdings") or [])
    out = []
    for b in books.values():
        held = set(b.get("holdings") or [])
        out.append({
            "id": b["id"], "label": b["label"], "label_zh": b.get("label_zh"),
            "desc": b.get("desc"), "desc_zh": b.get("desc_zh"), "is_baseline": b.get("is_baseline"),
            "return_pct": b.get("return_pct"), "vs_spy_pct": b.get("vs_spy_pct"),
            "nav": b.get("nav"), "n_positions": b.get("n_positions"),
            "hit_rate": b.get("hit_rate"), "brier": b.get("brier"),
            "n_resolved": b.get("n_resolved"), "n_theses": b.get("n_theses"),
            "n_marks": b.get("n_marks"), "avg_rel_return": b.get("avg_rel_return"),
            "extra_vs_prod": sorted(held - prod_held),     # held here, not by prod
            "missing_vs_prod": sorted(prod_held - held),   # held by prod, not here
        })
    return sorted(out, key=lambda x: (x["return_pct"] is None, -(x["return_pct"] or 0)))


def load_leaderboard() -> dict:
    try:
        return json.loads(_LEADERBOARD.read_text())
    except Exception:  # noqa: BLE001
        return {}
