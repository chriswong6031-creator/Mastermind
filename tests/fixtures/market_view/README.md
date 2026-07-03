# market_view fixtures — frozen 06-20..07-02 incident window

These fixtures are the frozen inputs and expected outputs for the W-E.0 perception
organ tests (E0.4). They feed `tests/incident_replays/test_incident_2026_07_02.py`
and `tests/test_phase2.py` (E0.5 perception runlog asserts).

## Fixture provenance

Each file documents its own `_note` with a clear REAL / RECONSTRUCTED distinction
(charter P2 — degrade-never-fabricate; the same standard applies to fixture labeling).

| File | Content | Provenance |
|---|---|---|
| `regime_snapshot_incident.json` | Full regime/latest.json with embedded sub-blocks for the 07-01 crash-eve read | Top-level quad/confidence/transition: REAL (from incident fixture). Embedded blocks (risk_radar, froth_fragility, mtf_signals, risk_state, turning_point, vol_shock): REAL from live `data/regime/latest.json` as of 2026-06-26 (same publish as 07-01 read per post-mortem). |
| `regime_snapshot_calm.json` | Constructed calm-tape control regime snapshot | CONSTRUCTED — synthesized to represent a high-confidence agreeing window. Not from a real date. Used for calm-tape invariance assertion (conflict=False, zero drift). |
| `sector_cycles_incident.json` | Enriched sector_cycles snapshot for incident window | Phase/pos/osc_slope/signal: REAL (from existing incident fixture). RS fields (rs_rank/rs_63d/rs_126d/timing_state): RECONSTRUCTED from `rotation_spec.md` worked example. |
| `rotation_tensor_06_24.json` | Expected rotation_tensor output for 2026-06-24 (episode start) | RECONSTRUCTED from `rotation_spec.md` §1(e) worked example. XLV-vs-XLK R_bps_day/dR computed off vendored yahoo through 07-01 per the spec. |
| `rotation_tensor_06_29.json` | Expected rotation_tensor output for 2026-06-29 (episode established) | RECONSTRUCTED from `rotation_spec.md` §1(e) — the 4-session confirmed episode. |
| `anticipation_06_25.json` | Expected anticipation output for 2026-06-25 (SECTOR-TOP CRITICAL) | RECONSTRUCTED from `judged_anticipation.md` synthesis: SECTOR-TOP ELEVATED by ~06-19, CRITICAL by 06-22..25. |
| `anticipation_06_26.json` | Expected anticipation output for 2026-06-26 (CRASH-RISK ELEVATED) | RECONSTRUCTED from `build_plan.md` §4.1 pre-registered assert. |
| `market_view_07_01.json` | Expected `brain/market_view.py` output for 2026-07-01 | RECONSTRUCTED from `judged_market-view.md` synthesis: label risk_on at conf 0.327 vs 3 validated planes risk_off, conflict=True, coherence~0.38, posture_floor_defense=True. |
| `market_view_calm.json` | Expected market_view output for calm-tape control | CONSTRUCTED — all planes agreeing risk_on, conflict=False. |

## Design constraints these fixtures encode

1. **Embedded-key access (coverage_audit.md §structural finding):** `regime_snapshot_incident.json`
   contains the FULL embedded sub-blocks that the bot discards today. The E0.3 adapters read
   these fields from the already-loaded regime JSON, zero new fetches.

2. **Advisory-only, cold_start=True (build_plan.md §3):** every `anticipation_*.json` and
   `rotation_tensor_*.json` stamps `advisory: true, cold_start: true, effective_n: 0` — P3
   status must be earned through the walk-forward gate (E1.4), not assumed.

3. **Conflict=True pre-registered dates (build_plan.md §4.3):** `market_view_07_01.json`
   asserts `label_vs_planes.conflict=True` for the 06-26..07-01 window. The test
   `test_market_view_conflict_incident_window` in the incident replay file checks this.

4. **Calm-tape zero-drift (build_plan.md §4.6):** `market_view_calm.json` asserts
   conflict=False with all planes agreeing. The test `test_calm_tape_no_conflict` checks
   that the calm fixture produces zero drift.

## Missing fixtures (documented gaps)

- `rotation_tensor_07_01.json` — the full 6-session XLV-vs-SMH episode at +704 bps, percentile
  0.91. Will be added once `brain/rotation_tensor.py` (E0.1) exists and can be replayed on
  the vendored yahoo closes.
- Per-date `market_view_{date}.json` for 06-26..07-01 — will be added once E0.3 exists and
  can replay on the incident fixture inputs. The 07-01 fixture covers the key hard assertion.
