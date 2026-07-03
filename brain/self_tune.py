"""Bounded self-repair through the Lab harness — the self-modification boundary (W-L / L4).

THE ONE thing in this whole system that may change its own behaviour with no LLM and no human. So it
is drawn tighter than anything else, by charter P3 (signals earn authority) and P8 (autonomy earned
in shadow):

  * It may propose new values ONLY for `config/doctrine.yml` keys carrying the literal
    '(unverified-prior)' tag — the constants the waves deliberately parked as priors.
  * A DENYLIST (hard-coded here AND mirrored in doctrine so a mistag can never widen the surface)
    additionally excludes cap hard-bounds and anything under gates / replay / charter config, EVEN IF
    such a key were mistagged unverified-prior.
  * ONE parameter family per week (a family = one top-level doctrine block).
  * Every candidate is judged through loop/harness.py's IMMUTABLE frozen judge + loop/pbo.cscv + a
    ONE-SHOT loop/holdout touch — REUSED verbatim. self_tune adds ZERO new statistics; it never
    edits how a candidate is judged (the harness's cardinal rule).
  * A survivor runs SHADOW for a configured window (a predicted-vs-realized ledger) before it may
    apply. On apply it writes a journal entry and arms an auto-revert trigger; if realized
    underperforms the shadow projection by `revert_margin` over the window it reverts + journals.
  * A family that reverts TWICE is marked proposal-only FOREVER (persisted in
    data/self_tune/state.json) — never re-armed without a human.

ARMING: MASTERMIND_SELF_TUNE defaults OFF. When OFF every public entrypoint is a pure no-op and the
book / doctrine are byte-identical (P8 — it earns arming in shadow like everything else). Best-effort
throughout; a failure degrades to no-op, never raises into a build (P2).

WHAT IT NEVER TOUCHES (the hard denylist, also in doctrine.yml self_tune.denylist): the charter, the
replay batteries, the validation gates, cap hard-bounds, anything outside the (unverified-prior)-tagged
doctrine constants. The system may PROPOSE changes to its own gates (they land in the agenda as
opus-session items); it may never APPLY them.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

_ROOT = Path(__file__).resolve().parent.parent
_STATE_DIR = _ROOT / "data" / "self_tune"
_STATE_PATH = _STATE_DIR / "state.json"
_DOCTRINE_PATH = _ROOT / "config" / "doctrine.yml"

# The literal tag a doctrine key MUST carry to be self-tunable — hard-coded so a mistyped tag in the
# yaml can never widen the surface (belt-and-suspenders with doctrine self_tune.unverified_tag).
_UNVERIFIED_TAG = "(unverified-prior)"

# The DENYLIST — dotted-path substrings self_tune may NEVER propose against, even if the key carries
# the unverified-prior tag. Hard-coded here (P3/P8 ship-blocker); doctrine mirrors it for auditability
# but the CODE is authoritative — a doctrine edit can never REMOVE a denylist entry from the guard.
_HARD_DENYLIST = (
    "caps.",            # every cap-block key (name_cap, ballast_cap, cluster/firm caps…)
    "armed_ceiling",    # def_sleeve hard clamp
    "validation_",      # *_validation gate thresholds (rotation_tensor / perception)
    "perception_validation.",  # the ENTIRE gate-definition block: a tuner that can redefine what
                        # counts as a drawdown event / episode / walk window can gerrymander its own
                        # exams (Fable boundary review, W-L merge) — deny the block, not just the
                        # 'gate' tokens inside it
    "auc_gate",         # perception AUC gate
    "fires_max_frac",   # perception firing-rate gate
    "gate",             # anything with 'gate' in its path
    "replay",           # replay-battery config
    "charter",          # charter-tagged constants
    "hard_bound",       # explicit hard-bound markers
    "self_tune.",       # self_tune may NEVER tune its OWN bounds (step/margin/two-strikes) — a
                        # meta-boundary: a self-modifier that can widen its own leash is unbounded (P8)
    "governor",         # the posture governor's OWN arming guards — never self-adapt the adapter (P8)
)

# defaults mirroring the doctrine self_tune block (degrade-safe if the block is absent)
_DEFAULTS = {
    "step_pct": 0.25,
    "n_candidates": 2,
    "shadow_window_sessions": 21,
    "revert_margin": 0.20,
    "reverts_to_lock": 2,
    "min_holdout_sharpe": 0.0,
}


# ─────────────────────────────────────────────────────────────────────────────
# arming flag (charter P8 — default OFF, byte-identical no-op)
# ─────────────────────────────────────────────────────────────────────────────
def _on() -> bool:
    return os.environ.get("MASTERMIND_SELF_TUNE", "0").strip().lower() in ("1", "true", "yes", "on")


def _cfg() -> dict:
    """doctrine.yml self_tune block merged over the degrade-safe defaults. Never raises."""
    out = dict(_DEFAULTS)
    try:
        from bot.doctrine_config import load_doctrine
        block = load_doctrine().get("self_tune") or {}
        for k in _DEFAULTS:
            if block.get(k) is not None:
                out[k] = block[k]
    except Exception:  # noqa: BLE001
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────────
# the tunable-surface guard (charter P3/P8 — the whole point of the module)
# ─────────────────────────────────────────────────────────────────────────────
def _denied(dotted_path: str) -> bool:
    """True iff `dotted_path` (block.key) hits the hard denylist — case-insensitive substring."""
    p = (dotted_path or "").lower()
    return any(tok in p for tok in _HARD_DENYLIST)


def _line_is_tagged(line: str) -> bool:
    return _UNVERIFIED_TAG in (line or "")


def tunable_keys(doctrine_text: str | None = None) -> dict:
    """The set of self-tunable dotted paths, parsed straight from the doctrine YAML TEXT so the
    '(unverified-prior)' tag is read from the SAME source of truth a human audits (a key is tunable iff
    its own line carries the tag AND its dotted path is not denied). Returns {path: current_value} for
    scalar (int/float) leaves only — a list / mapping value is never a tunable scalar.

    Text-parsing (not the loaded dict) is deliberate: the tag lives in a line comment that yaml.safe_load
    discards, so the surface can only be derived from the raw text. Best-effort; never raises.
    """
    try:
        text = doctrine_text if doctrine_text is not None else _DOCTRINE_PATH.read_text()
    except Exception:  # noqa: BLE001
        return {}

    out: dict = {}
    block: Optional[str] = None
    block_indent = -1
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        # a top-level (indent 0) `name:` opens a family block
        m_top = re.match(r"^([A-Za-z0-9_]+):\s*(#.*)?$", raw)
        if indent == 0 and m_top:
            block = m_top.group(1)
            block_indent = 0
            continue
        if block is None:
            continue
        # a scalar leaf `key: value  # ...` directly under the current block (indent 2)
        m_leaf = re.match(r"^(\s+)([A-Za-z0-9_]+):\s*([^#\n]+?)\s*(#.*)?$", raw)
        if not m_leaf:
            continue
        leaf_indent = len(m_leaf.group(1))
        key = m_leaf.group(2)
        val_str = m_leaf.group(3).strip()
        # only a direct child of the top-level block (a nested map re-scopes; we tune flat leaves)
        if leaf_indent != 2:
            continue
        if not _line_is_tagged(raw):
            continue
        # value must be a numeric scalar
        try:
            val = float(val_str)
        except ValueError:
            continue
        path = f"{block}.{key}"
        if _denied(path):
            continue
        out[path] = val
    return out


# ─────────────────────────────────────────────────────────────────────────────
# candidate value generation (bounded steps — ±step_pct or one band-edge)
# ─────────────────────────────────────────────────────────────────────────────
def candidate_values(current: float, *, step_pct: float | None = None,
                     n: int | None = None) -> list[float]:
    """Bounded candidate values around `current`: the ±step_pct pair (default), rounded to a sane
    precision. NEVER an unbounded search — the step is capped so one week can only nudge a prior, not
    leap it. A zero current degrades to a small absolute step so ±% is not inert."""
    cfg = _cfg()
    sp = float(step_pct if step_pct is not None else cfg["step_pct"])
    k = int(n if n is not None else cfg["n_candidates"])
    base = float(current)
    if base == 0.0:
        deltas = [0.01, -0.01]
    else:
        deltas = [base * sp, -base * sp]
    cands: list[float] = []
    for d in deltas[: max(1, k)]:
        v = round(base + d, 6)
        if v != base and v not in cands:
            cands.append(v)
    return cands


# ─────────────────────────────────────────────────────────────────────────────
# persisted state — the two-strikes ledger (charter P8: reverts twice → proposal-only FOREVER)
# ─────────────────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    try:
        raw = json.loads(_STATE_PATH.read_text())
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(state, indent=2, default=str, sort_keys=True))
    except Exception:  # noqa: BLE001
        pass


def _family_rec(state: dict, family: str) -> dict:
    fams = state.setdefault("families", {})
    return fams.setdefault(family, {"reverts": 0, "proposal_only": False, "history": []})


def is_proposal_only(family: str, state: dict | None = None) -> bool:
    """True iff `family` has reverted `reverts_to_lock` times and is locked to proposal-only FOREVER.
    Reads the persisted ledger (P8) — a locked family may never again be auto-applied."""
    st = state if state is not None else _load_state()
    rec = (st.get("families") or {}).get(family) or {}
    return bool(rec.get("proposal_only"))


def record_revert(family: str, evidence: dict | None = None) -> dict:
    """Record an auto-revert for `family`. On the `reverts_to_lock`-th revert the family flips to
    proposal-only FOREVER (persisted). Returns the updated family record. This is the two-strikes rule
    and it is IRREVERSIBLE from code — only a human editing state.json can unlock a family."""
    cfg = _cfg()
    st = _load_state()
    rec = _family_rec(st, family)
    if rec.get("proposal_only"):
        return rec                                   # already locked — nothing to escalate
    rec["reverts"] = int(rec.get("reverts", 0)) + 1
    rec.setdefault("history", []).append({
        "event": "revert", "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "evidence": evidence or {},
    })
    if rec["reverts"] >= int(cfg["reverts_to_lock"]):
        rec["proposal_only"] = True
        rec["locked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _save_state(st)
    return rec


# ─────────────────────────────────────────────────────────────────────────────
# the harness gate — REUSE loop/harness + pbo + holdout (add ZERO new statistics)
# ─────────────────────────────────────────────────────────────────────────────
def evaluate_candidate(candidate, closes, bill, *, pool: dict | None = None,
                       s6040: float | None = None, holdout_start: str = "2022-01-01",
                       touched: set | None = None) -> dict:
    """Judge ONE tune candidate through the IMMUTABLE Lab harness, exactly as loop/iterate does:
    in-sample harness.score → pbo.cscv over the pool → a ONE-SHOT holdout touch → promote.gate.

    `candidate` is a Lab strategy-candidate object (materialize/spec_hash/spec — the SAME contract
    loop/candidates yields) whose returns already REFLECT the proposed doctrine value (the caller's
    candidate_factory bakes the value into the spec). We do NOT reimplement scoring — we call the
    frozen judge and the frozen gate. Returns the promote.gate verdict enriched with the raw metrics.
    Best-effort; a scoring failure returns a 'rejected' verdict (never raises)."""
    try:
        from loop import harness, pbo, holdout, promote
        from engine import validation as V
        import pandas as pd

        insample = closes[closes.index < pd.Timestamp(holdout_start)]
        holdout_slice = closes[closes.index >= pd.Timestamp(holdout_start)]
        s40 = s6040 if s6040 is not None else harness.sixty_forty_sharpe(insample, bill)

        m = harness.score(candidate, insample, bill, n_eff=1, sixty40_sharpe=s40)
        if not m:
            return {"stage": "rejected", "reason": "candidate did not backtest", "checks": {}}

        # PBO over the pool of {baseline + candidates}; the candidate's own series is included so the
        # selection is tested for overfitting exactly as iterate tests its pool.
        pool = dict(pool or {})
        pool[candidate.spec_hash] = m["net"]
        pbo_val = pbo.cscv(pool, S=12).get("pbo") if len(pool) >= 2 else None

        # BH-FDR on this candidate's p-value (single-hypothesis FDR degrades to the raw reject rule).
        fdr = V.benjamini_hochberg({candidate.spec_hash: m["p_value"]}, alpha=0.10)
        fdr_reject = bool(fdr[candidate.spec_hash]["reject"])

        # ONE-SHOT holdout touch (the frozen guard) — a re-tune produces a new spec_hash → new look.
        # A holdout-scoring failure degrades to hconf=False (a NON-confirming holdout — the safe
        # direction: it can only BLOCK a promotion, never manufacture one) without masking the
        # primary in-sample gate verdict below (P2 — a missing check coarsens, never fabricates).
        touched = touched if touched is not None else set()
        hconf = False
        try:
            holdout.touch(candidate.spec_hash, touched)
            mh = harness.score(candidate, holdout_slice, bill, n_eff=1, sixty40_sharpe=s40)
            ho_sharpe = mh["sharpe"] if mh else 0.0
            hconf = holdout.confirms(m["sharpe"], ho_sharpe) and ho_sharpe >= float(_cfg()["min_holdout_sharpe"])
        except holdout.HoldoutBurned:
            hconf = False
        except Exception:  # noqa: BLE001 — holdout could not be scored → non-confirming (blocks, never promotes)
            hconf = False

        g = promote.gate(m, fdr_reject=fdr_reject, pbo=pbo_val,
                         holdout_confirms=hconf, budget_exhausted=False)
        g = dict(g)
        g["metrics"] = {k: m[k] for k in ("dsr", "sharpe", "maxdd", "cagr", "beats_spy",
                                          "beats_6040", "crisis_pass", "fold_robust") if k in m}
        g["pbo"] = pbo_val
        g["holdout_confirms"] = hconf
        return g
    except Exception as e:  # noqa: BLE001
        return {"stage": "rejected", "reason": f"evaluation error: {e}", "checks": {}}


# ─────────────────────────────────────────────────────────────────────────────
# the weekly proposal pipeline (agenda evidence → candidates → harness → shadow)
# ─────────────────────────────────────────────────────────────────────────────
def propose(family: str, *, evidence: dict | None = None,
            candidate_factory: Optional[Callable] = None,
            closes=None, bill=None, asof: str | None = None,
            state: dict | None = None) -> dict:
    """Run ONE weekly self-tune proposal for a doctrine `family` (a top-level block).

    Pipeline (design §3), all guard-checked and P2-degrade-safe:
      (1) the agenda's `evidence` NAMES the family (passed in);
      (2) enumerate the family's self-tunable scalar keys and their bounded candidate values;
      (3) judge each candidate through the IMMUTABLE harness (via `candidate_factory`, which turns a
          (key, value) into a Lab strategy-candidate whose returns reflect that value) — a factory is
          REQUIRED to reach the harness; without one the proposal is 'no-factory' (agenda opus-session);
      (4) the survivor (if any) is emitted for SHADOW enrollment (the caller stages it — self_tune does
          not apply live here; apply() does that after the shadow window).

    Returns a verdict dict: {family, status, proposals:[...], survivor, ...}. status ∈
    {'off', 'proposal-only', 'no-tunable-keys', 'no-factory', 'no-survivor', 'shadow-staged'}.
    NEVER applies a live change — the shadow-first discipline (P8) lives in apply()."""
    if not _on():
        return {"family": family, "status": "off"}

    st = state if state is not None else _load_state()

    # (6) a locked family is proposal-only FOREVER — it may still be PROPOSED (into the agenda for an
    # opus-session), but self_tune must never auto-evaluate/apply it.
    if is_proposal_only(family, st):
        return {"family": family, "status": "proposal-only",
                "note": "family reverted twice — auto-tuning is permanently disabled (charter P8)"}

    keys = {p: v for p, v in tunable_keys().items() if p.split(".", 1)[0] == family}
    if not keys:
        return {"family": family, "status": "no-tunable-keys",
                "note": "no (unverified-prior)-tagged, non-denied scalar in this family"}

    proposals = []
    for path, cur in sorted(keys.items()):
        for val in candidate_values(cur):
            proposals.append({"path": path, "current": cur, "candidate": val})

    if candidate_factory is None:
        # Cannot reach the frozen harness without a factory that bakes the value into a backtest.
        # This is an HONEST degrade: the tune lands in the agenda as an opus-session item, never
        # auto-applied off an unjudged number (P3).
        return {"family": family, "status": "no-factory", "proposals": proposals,
                "note": "no candidate_factory — cannot reach the Lab harness; route to agenda as opus-session"}

    survivor = None
    pool: dict = {}
    touched: set = set()
    graded = []
    for prop in proposals:
        try:
            cand = candidate_factory(prop["path"], prop["candidate"])
        except Exception:  # noqa: BLE001
            cand = None
        if cand is None:
            graded.append({**prop, "stage": "rejected", "reason": "factory produced no candidate"})
            continue
        g = evaluate_candidate(cand, closes, bill, pool=pool, touched=touched)
        graded.append({**prop, "stage": g.get("stage"), "reason": g.get("reason"),
                       "metrics": g.get("metrics"), "pbo": g.get("pbo")})
        if g.get("stage") == "paper" and survivor is None:   # first harness survivor wins this week
            survivor = {**prop, "verdict": g}

    if survivor is None:
        return {"family": family, "status": "no-survivor", "proposals": graded,
                "note": "no candidate cleared the immutable harness gate this week"}

    # (4) stage the survivor for SHADOW — it does NOT apply live until the window resolves (P8).
    rec = _stage_shadow(family, survivor, evidence, asof, st)
    return {"family": family, "status": "shadow-staged", "survivor": survivor,
            "shadow": rec, "proposals": graded}


def _stage_shadow(family: str, survivor: dict, evidence: dict | None,
                  asof: str | None, state: dict) -> dict:
    """Open a shadow record for a harness-surviving candidate: predicted (shadow) vs realized ledger,
    to be reconciled after `shadow_window_sessions`. Persisted under the family's history. The shadow's
    projection is the candidate's in-sample Sharpe (the realized-vs-projection margin drives the
    auto-revert). Best-effort."""
    cfg = _cfg()
    st = state
    rec = _family_rec(st, family)
    projection = None
    try:
        projection = float((survivor.get("verdict") or {}).get("metrics", {}).get("sharpe"))
    except Exception:  # noqa: BLE001
        projection = None
    shadow = {
        "opened_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "asof": str(asof)[:10] if asof else str(date.today()),
        "path": survivor["path"], "from": survivor["current"], "to": survivor["candidate"],
        "projection_sharpe": projection,
        "window_sessions": int(cfg["shadow_window_sessions"]),
        "evidence": evidence or {},
        "state": "shadow",          # shadow → applied → (reverted | kept)
    }
    rec.setdefault("history", []).append({"event": "shadow_open", **shadow})
    rec["active_shadow"] = shadow
    _save_state(st)
    return shadow


# ─────────────────────────────────────────────────────────────────────────────
# apply + auto-revert (the live transition — only after a shadow window, always journaled)
# ─────────────────────────────────────────────────────────────────────────────
def apply(family: str, *, realized_sharpe: float | None = None,
          journal_fn: Optional[Callable] = None, state: dict | None = None) -> dict:
    """After a family's shadow window resolves, decide APPLY vs AUTO-REVERT and journal it.

    AUTO-REVERT (P8): if the realized shadow Sharpe underperforms the projection by >= `revert_margin`
    (fractional), the tune reverts and `record_revert` is called (which may lock the family to
    proposal-only on the second strike). Otherwise the tune is 'kept' (an ops step then writes the new
    value into doctrine.yml at the coordinated restart — self_tune stages, ops applies; the module
    never edits the yaml itself, so a tune can never land un-reviewed).

    A journal entry is written through `journal_fn(entry)` when supplied (the L2 journal seam) so every
    self-modification carries its evidence + falsifier. Best-effort; never raises."""
    if not _on():
        return {"family": family, "status": "off"}
    st = state if state is not None else _load_state()
    rec = (st.get("families") or {}).get(family) or {}
    shadow = rec.get("active_shadow")
    if not shadow or shadow.get("state") != "shadow":
        return {"family": family, "status": "no-active-shadow"}

    cfg = _cfg()
    proj = shadow.get("projection_sharpe")
    reverted = False
    if realized_sharpe is not None and proj is not None and proj != 0:
        shortfall = (float(proj) - float(realized_sharpe)) / abs(float(proj))
        reverted = shortfall >= float(cfg["revert_margin"])

    entry = {
        "kind": "self_tune",
        "family": family, "path": shadow.get("path"),
        "from": shadow.get("from"), "to": shadow.get("to"),
        "projection_sharpe": proj, "realized_sharpe": realized_sharpe,
        "evidence": shadow.get("evidence"),
        # the falsifier every self-modification must carry (charter P3 — a lesson without a falsifier
        # is not learned): the tune is REVERTED the moment realized underperforms the projection.
        "falsifier": f"realized Sharpe < projection − {cfg['revert_margin']:.0%} over "
                     f"{shadow.get('window_sessions')} sessions → auto-revert",
    }

    if reverted:
        entry["decision"] = "auto-reverted"
        rec2 = record_revert(family, evidence={"realized_sharpe": realized_sharpe,
                                               "projection_sharpe": proj, "path": shadow.get("path")})
        entry["reverts"] = rec2.get("reverts")
        entry["proposal_only"] = bool(rec2.get("proposal_only"))
        # clear the active shadow on the (re-read) state so the ledger reflects the revert
        st = _load_state()
        r = _family_rec(st, family)
        r.pop("active_shadow", None)
        r.setdefault("history", []).append({"event": "reverted",
                                            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                            "path": shadow.get("path")})
        _save_state(st)
    else:
        entry["decision"] = "kept"
        shadow["state"] = "applied"
        rec["active_shadow"] = None
        rec.setdefault("history", []).append({"event": "applied",
                                             "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                             "path": shadow.get("path"), "to": shadow.get("to")})
        _save_state(st)

    if journal_fn is not None:
        try:
            journal_fn(entry)
        except Exception:  # noqa: BLE001 — journaling is additive; never break the transition
            pass
    return {"family": family, "status": entry["decision"], "entry": entry}
