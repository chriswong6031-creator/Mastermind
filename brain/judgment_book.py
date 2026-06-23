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
          portfolio_ctx: dict | None = None, name_cap: float | None = None) -> list[dict]:
    """Reshape the engine's confirmed conviction list (``sized``) into the PM's target book,
    checked by SENTINEL + NEXUS and clamped by ``name_cap``. Returns the SAME schema
    ``conviction.build`` emits, so the downstream loop is unchanged.

    Degrade-safe: returns the input ``sized`` UNCHANGED whenever the flag is OFF, either seat
    is unavailable, or the PM did not produce a book → the engine path stays byte-identical.
    Never raises."""
    if not _enabled():
        return sized

    gate_info = gate_info or {}
    portfolio_ctx = portfolio_ctx or {}
    cap = float(name_cap) if name_cap is not None else _DEFAULT_NAME_CAP

    try:
        from brain import strategist, pm_conviction, committee
        from portfolio import lenses as lenses_mod
    except Exception:  # noqa: BLE001
        return sized

    # (A) MACRO STRATEGIST — top-down confirmed-leadership themes (best-effort; None on failure)
    try:
        strat = strategist.run(asof, regime)
    except Exception:  # noqa: BLE001
        strat = None

    # (B) PM-CONVICTION — the armed Opus PM builds the Flagship target book
    try:
        book = pm_conviction.build_book(sized, rejected, regime=regime, asof=asof,
                                        strategist=strat, gate_info=gate_info,
                                        portfolio_ctx=portfolio_ctx)
    except Exception:  # noqa: BLE001
        book = None
    if not book or not book.get("ran") or not book.get("holdings"):
        # PM did not produce a usable book → degrade to the engine path untouched.
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
        out.append({
            "ticker": t,
            "weight": w,
            "confluence": confluence,
            "bull": syn.get("bull") or "",
            "bear": syn.get("bear") or "",
            "divergences": (syn.get("divergences") or []),
            "retained": False,
            "size_stage": None,
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

    return out
