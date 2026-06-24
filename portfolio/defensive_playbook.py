"""Driver-aware defensive playbook — WHERE defense lives depends on WHAT is cracking.

A fixed "go to staples + bonds" risk-off rule is wrong: a liquidity/AI-capex unwind and an inflation
shock want OPPOSITE bond exposure, and rate-sensitive banks/REITs flip from hurt→helped depending on
whether the driver is rates-UP or a growth-scare. This module reads the Macro Risk Officer's state
(``brain/macro_risk.risk_state`` — the cracking driver chains + the regime growth/inflation sign) and
selects the appropriate defensive TILT from ``config/defensive_playbook.yml`` (externalized + tunable,
in-code default fallback so a bad edit never breaks a book).

HONESTY ABOUT THE SUBTRACT-ONLY INVARIANT. The desk's Risk Officer can only DE-RISK, so the only
ENFORCED teeth this playbook contributes are ``cash_floor`` (= the gross cap → a hard cash floor) and
the ``avoid`` add-block. The ``favor`` list is ADVISORY — fed to the additive PM seat as context for
its next decision, surfaced on the dashboard, and logged as recommendations. The desk NEVER auto-buys
a defensive (that would break subtract-only). Callers must treat ``favor`` as a suggestion, not an order.

Pure / deterministic / NEVER raises.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_SPEC_PATH = _ROOT / "config" / "defensive_playbook.yml"


# ── in-code DEFAULT — fallback when the YAML is missing/malformed (kept in sync with the yml) ────
_DEFAULT_ARCHETYPES: dict[str, dict] = {
    "ai_capex_unwind": {
        "favor": ["XLP", "XLV", "USMV", "QUAL", "SGOV", "GLD"],
        "avoid": ["SMH", "SOXX", "ARKK"],
        "cash_floor": 0.30,
        "rationale": ("A crowded AI-buildout/semis blow-off into contracting liquidity breaks the "
                      "most-extended momentum first; rotate to quality/low-vol/staples/healthcare + "
                      "cash; gold/duration only help if it's a growth-scare."),
        "caveats": ["Duration (TLT) HELPS only if the unwind is a growth-scare (inflation < 0); "
                    "if rates are still rising, duration adds risk.",
                    "Banks (XLF) are not a clean hedge — a liquidity event stresses credit.",
                    "REITs (XLRE) are rate-sensitive — avoid while real yields rise."],
    },
    "liquidity_tightening": {
        "favor": ["XLP", "XLV", "USMV", "QUAL", "SGOV"],
        "avoid": ["IWM", "ARKK", "KRE"],
        "cash_floor": 0.30,
        "rationale": ("Contracting liquidity de-rates the highest-beta, lowest-quality, most-leveraged "
                      "cohorts first; up-in-quality + cash."),
        "caveats": ["Prefer T-bills (SGOV) over long duration (TLT) until the growth-vs-inflation "
                    "driver is clear.", "Avoid adding leverage/small-caps into a tightening tape."],
    },
    "inflation_shock": {
        "favor": ["XLE", "XLB", "GLD", "SGOV", "VLUE"],
        "avoid": ["TLT", "IEF", "ARKK", "QQQ"],
        "cash_floor": 0.25,
        "rationale": ("An inflation impulse lifts real-asset cyclicals and CRUSHES long-duration bonds "
                      "and high-multiple growth; tilt to value + short duration."),
        "caveats": ["The ONE driver where duration is the wrong hedge — AVOID TLT/IEF.",
                    "Energy/materials crack too if the shock tips into a hard growth-scare."],
    },
    "credit_event": {
        "favor": ["XLV", "XLP", "USMV", "TLT", "SGOV"],
        "avoid": ["HYG", "KRE", "IWM", "XLF"],
        "cash_floor": 0.35,
        "rationale": ("A credit/funding event is a flight to quality and Treasuries; AVOID high-yield, "
                      "leverage, small-caps and banks; Treasuries HELP here."),
        "caveats": ["Banks (XLF/KRE) are the epicenter — avoid, do not buy the dip.",
                    "Duration HELPS in a credit/growth-scare event (the one place TLT is a real hedge)."],
    },
    "broad_derisk": {
        "favor": ["XLP", "XLV", "USMV", "SGOV"],
        "avoid": ["ARKK", "IWM"],
        "cash_floor": 0.25,
        "rationale": ("Default risk-off with no single dominant driver: up-in-quality, low-vol, "
                      "staples/healthcare, raise cash; stay driver-agnostic until the tell is clear."),
        "caveats": ["No clear driver yet — keep the defensive tilt shallow and cash-led."],
    },
}


def load_spec() -> dict:
    """The externalized playbook (``config/defensive_playbook.yml``). {} on a missing/corrupt file or
    absent PyYAML — the caller falls back to the in-code default. Read fresh; never raises."""
    try:
        import yaml
        if _SPEC_PATH.exists():
            d = yaml.safe_load(_SPEC_PATH.read_text())
            return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _archetypes() -> dict[str, dict]:
    a = load_spec().get("archetypes")
    return a if isinstance(a, dict) and a else _DEFAULT_ARCHETYPES


def _num(x, default: float) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def select_archetype(risk_state: dict | None) -> str:
    """Pick the defensive archetype from the macro read. PURE; never raises.

    Logic — the cracking DRIVER first, then the regime growth/inflation sign:
      * a credit axis breaking (HYG/TLT) with the credit axis dominant → ``credit_event``
      * the inflation impulse positive AND the credit/USD axis is the dominant stress → ``inflation_shock``
      * the liquidity axis dominant + a named theme-chain driver (e.g. ai_buildout) → ``ai_capex_unwind``
      * the liquidity axis dominant, no single theme chain → ``liquidity_tightening``
      * otherwise → ``broad_derisk``
    """
    rs = risk_state or {}
    axes = rs.get("axes") or {}

    def _ax(name) -> float:
        a = axes.get(name) or {}
        return _num(a.get("fragility"), 0.0)

    infl = _num(rs.get("regime_inflation"), 0.0)
    credit = _ax("credit_usd")
    liq = _ax("liquidity")
    vol = _ax("volatility")
    crowd = _ax("crowding")

    driver_ids = set()
    for d in (rs.get("drivers") or []):
        if isinstance(d, dict) and d.get("id"):
            driver_ids.add(str(d["id"]))
        elif isinstance(d, str):
            driver_ids.add(d)

    archetypes = _archetypes()
    # Priority is most-specific → least. A crowded theme-chain cracking into tightening liquidity (the
    # 06-23 case) is the most specific read and takes precedence even when credit/vol are ALSO maxed;
    # an inflation impulse flips the duration hedge so it outranks a generic credit read; a credit-led
    # break (credit the dominant standalone stress, no inflation/theme driver) is next.
    # 1. crowded theme-chain unwinding into tightening liquidity (named driver present)
    if (liq >= 0.4 or crowd >= 0.4) and driver_ids and "ai_capex_unwind" in archetypes:
        return "ai_capex_unwind"
    # 2. an inflation impulse with credit/USD (DXY/real-asset) stress → inflation shock (duration is wrong)
    if infl > 0.15 and credit >= 0.4 and "inflation_shock" in archetypes:
        return "inflation_shock"
    # 3. a credit-led break: credit firing hardest, no inflation impulse / theme driver to override
    if credit >= 0.5 and credit >= max(liq, crowd, vol) and "credit_event" in archetypes:
        return "credit_event"
    # 4. liquidity tightening with no single named driver
    if liq >= 0.4 and "liquidity_tightening" in archetypes:
        return "liquidity_tightening"
    return "broad_derisk" if "broad_derisk" in archetypes else next(iter(archetypes), "broad_derisk")


def defensive_tilt(risk_state: dict | None) -> dict:
    """The driver-appropriate defensive tilt for the current macro state. PURE; never raises.

    Returns::

        {archetype, favor:[...], avoid:[...], cash_floor, rationale, caveats:[...],
         rate_sensitive_note, advisory:True}

    ``favor`` is ADVISORY (the subtract-only desk never auto-buys it). ``avoid`` + ``cash_floor`` are
    the enforceable teeth (add-block + cash floor). The rate-sensitive note flips with the regime
    inflation sign: rates-UP (inflation > 0) → banks/REITs are a hazard, avoid; a growth-scare
    (inflation < 0) → duration can help and the rate-sensitive avoid softens."""
    rs = risk_state or {}
    name = select_archetype(rs)
    a = _archetypes().get(name) or _DEFAULT_ARCHETYPES.get(name) or _DEFAULT_ARCHETYPES["broad_derisk"]

    infl = _num(rs.get("regime_inflation"), 0.0)
    growth = _num(rs.get("regime_growth"), 0.0)
    favor = [str(t).upper() for t in (a.get("favor") or [])]
    avoid = [str(t).upper() for t in (a.get("avoid") or [])]

    # CONDITIONAL rate-sensitive logic — the caveat the playbook exists to encode.
    if infl > 0.1:
        rate_note = ("Rates-UP driver (inflation impulse positive): banks/REITs/long-duration are "
                     "rate hazards — AVOID adding; long Treasuries are NOT a hedge here.")
        for t in ("XLRE", "TLT", "IEF"):
            if t not in avoid and name != "credit_event":
                avoid.append(t)
        favor = [t for t in favor if t not in ("TLT", "IEF")]
    elif growth < 0 or infl < -0.1:
        rate_note = ("Growth-scare driver (growth weak / inflation falling): long duration (TLT) can "
                     "HELP and banks' loan-loss risk dominates — duration as a hedge, banks still avoid.")
        if "TLT" not in favor and name in ("ai_capex_unwind", "liquidity_tightening", "broad_derisk",
                                           "credit_event"):
            favor.append("TLT")
    else:
        rate_note = ("Driver sign unclear — prefer T-bills (SGOV) over long duration; keep "
                     "rate-sensitive banks/REITs off the add list until the driver resolves.")

    return {
        "archetype": name,
        "favor": favor,
        "avoid": avoid,
        "cash_floor": round(_num(a.get("cash_floor"), 0.25), 4),
        "rationale": str(a.get("rationale") or ""),
        "caveats": [str(c) for c in (a.get("caveats") or [])],
        "rate_sensitive_note": rate_note,
        "advisory": True,   # favor is a suggestion, not an order — the desk is subtract-only
    }


def brief(tilt: dict | None) -> str:
    """A one-line human/Brain summary of the tilt (for prompts + dashboard copy). Never raises."""
    t = tilt or {}
    favor = ", ".join((t.get("favor") or [])[:6]) or "—"
    avoid = ", ".join((t.get("avoid") or [])[:6]) or "—"
    return (f"Defensive tilt [{t.get('archetype', 'broad_derisk')}]: favor {favor}; avoid {avoid}; "
            f"cash floor {round((t.get('cash_floor') or 0) * 100)}%. {t.get('rate_sensitive_note', '')}")
