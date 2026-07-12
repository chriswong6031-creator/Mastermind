# `us_board_track_record.v1` — the buy-board forward-track-record publication contract

**Status:** PROPOSED. The macro side has NOT yet published a bot-consumable export; this document is
the contract the macro emitter builds to and the bot reader already consumes fail-soft.
**Owners:** the MACRO dashboard emits (a JSON projection of its ledger); the CONSUMPTION session
(Mastermind bot) reads.
**Reader:** `brain/board_track_record.py` is the SOLE bot-side reader of this artifact firm-wide.
Nothing else opens the file. If you are adding a track-record consumer, import `board_track_record` —
never re-open the JSON. The committed golden fixture (`tests/fixtures/us_board_track_record.json`) is
the executable copy of this schema.

This is the seam between the macro buy-board's forward accountability ledger and the bot's
single-stock divergence detector. It is intentionally short and testable: the schema, the two macro
surfaces it projects, the keep-first / point-in-time semantics, the staleness budget, and the two
open operator rulings.

---

## 1. The two macro surfaces (and why the ledger, not the board)

The macro dashboard has TWO distinct surfaces for buy-board names. They are NOT interchangeable:

| Surface | File | Nature | History? | gate_go |
|---|---|---|---|---|
| **The board** | `site/factordata/us_standouts.json` | VOLATILE — overwritten every build | none | carries `gate_go` (currently `false`) |
| **The ledger** | `data/us_board_ledger/retro_grades.parquet` (macro repo) | PERSISTENT, append-only, keep-FIRST per `(date, ticker)` | full | a past surfacing is a fact — not gate-gated |

The board answers "what looks good RIGHT NOW" and is gate-validated; the ledger answers "what was
surfaced on date D, and how did it do FORWARD" — the accountability record rendered in
`site/us_stocks.html`. A confirmed real ledger row:

> **AAPL · Technology · Surfaced 2026-07-01 · Return +7.4% · status "running".**

This contract projects the LEDGER (the retained, point-in-time, forward-graded record) — not the
volatile board — into a bot-consumable JSON, because the bot needs the immutable historical surface
(e.g. "was AAPL surfaced on 2026-07-01?") that the overwritten board can no longer answer.

---

## 2. Schema

```jsonc
{
  "schema": "us_board_track_record.v1",
  "as_of": "YYYY-MM-DD",        // the date this projection was cut — drives the reader's freshness gate
  "window_sessions": 21,         // the forward-grading window in sessions (provenance; e.g. 21 ≈ 1mo)
  "rows": [
    {
      "ticker": "AAPL",
      "sector": "Technology",         // GICS sector name as the board carries it
      "surfaced": "YYYY-MM-DD",        // the board-ENTRY date — keep-FIRST per (date, ticker)
      "return_pct": 7.4,               // forward return since `surfaced`, in PERCENT (not a fraction)
      "status": "running|stopped|flat",// the forward grade — see §3
      "board_pos": 1,                  // rank on the board when surfaced (int) | null (advisory)
      "fwd_mfe_pct": 9.1,              // forward max-favorable-excursion since surfaced, percent | null
      "on_board": true                 // is the name CURRENTLY on the (volatile) board
    }
  ]
}
```

### Reader validation (what `board_track_record` actually enforces)

Two tiers, deliberately minimal so the seam can evolve additively:

1. **Envelope gate** (all-or-nothing — failure ⇒ `[]`, the empty read):
   - `schema == "us_board_track_record.v1"`
   - `as_of` present and parseable
   - `as_of` fresh: `age_days ≤ 5` calendar days (see §5)
2. **Per-row gate** (a bad row is SKIPPED, never fatal): a row is kept iff it is a dict with a
   non-empty `ticker`, a parseable `surfaced` date, and a `status` in `{running, stopped, flat}`.
   Everything else (`return_pct`, `board_pos`, `fwd_mfe_pct`, `on_board`, `sector`) is coerced /
   passed through — a malformed or missing optional field never drops an otherwise-valid row.

`return_pct` and `fwd_mfe_pct` are coerced to float (or `None`); `board_pos` to int (or `None`);
`on_board` to bool. `ticker`/`status`/`surfaced` are normalized (upper-cased ticker, `surfaced`
truncated to `YYYY-MM-DD`).

---

## 3. Status semantics (the forward grade)

Verbatim from the macro grader:

- **running** — advancing, no stop breach. (Still "in play".)
- **stopped** — broke its stop. (A decided loss.)
- **flat**    — neither: chop, no decisive forward move.

`board_stats().win_rate` is `running / (running + stopped)`, **guarding `/0` → `None`** and
**excluding `flat` from the denominator** — a flat outcome is neither a win nor a loss.

---

## 4. Semantics — keep-first + point-in-time

1. **Keep-FIRST per `(surfaced_date, ticker)`.** The macro ledger records the FIRST surfacing of a
   name on a given date and never overwrites it. The reader mirrors this: on a duplicate
   `(surfaced, ticker)` it keeps the first-seen occurrence, and `record(ticker)` returns the earliest
   surfacing of a name across dates. This is the ledger's stable identity for the name.
2. **`surfaced_on(asof)` is the point-in-time board-ENTRY set.** It answers the historically-correct
   "was this name surfaced on the buy board that day" — immutable, and NOT gated by the volatile
   board's `gate_go` (a past surfacing is a fact). This is the query the single-stock divergence
   replay uses to re-ground its AAPL-Jul-1 example: `divergence_clue._default_board_membership(asof)`
   unions the volatile live board (gate_go-respecting) with `surfaced_on(asof)` (not gated), so a
   replay of `asof='2026-07-01'` sees AAPL as a valid trigger even though today's board reads
   `gate_go=false`. **This broadens only the point-in-time TRIGGER membership (what to look at); it
   never weakens the live `gate_go` discipline for SIZING.**
3. **Absence ≠ affirmative.** No file, a stale file, a wrong/absent schema, or an unparseable
   artifact all mean **"no track record available"** — every accessor returns its empty read
   (`[] / None / set() / the zero stat block`). Missing data may coarsen or freeze; it never
   fabricates a surfacing, a grade, or a board membership.

---

## 5. Freshness budget — 5 calendar days

The budget is **5 calendar days**: `age_days = today − as_of`, stale iff `age_days > 5`. An `as_of`
that is absent or unparseable is treated as **stale** (fail-closed), never as fresh.

This budget is LOOSER than the rotation seam's 2-session budget on purpose: the track-record ledger
is a slow-moving, mostly-append artifact — a forward grade does not go materially wrong in 3–5 days —
but it is still bounded so a dead or abandoned export can never masquerade as a live one. If the
export cadence turns out to be daily, tighten `_STALE_DAYS` toward the rotation budget.

---

## 6. Publication home — OPEN (operator ruling #a)

The bot reader supports a primary + fallback path today, so the ruling is a one-line change:

- **Option A — macro-side site plane (reader PRIMARY):**
  `vendor/macro/site/factordata/us_board_track_record.json`. Published alongside the other
  `site/*.json` signal contracts, next to `us_standouts.json` itself, and eligible for the same
  Cloudflare R2 data plane the per-ticker stockdata already uses. Cleanest join to the existing
  factordata surface; this is the reader's PRIMARY path.
- **Option B — bot-side data plane (reader FALLBACK):**
  `data/us_board_ledger/track_record.json` under the repo root. Git-tracked with the bot; simplest
  join to the bot's own ledgers; the reader's FALLBACK path, tried only if the primary is absent.
- **Option C — the bot reads the parquet directly** (`data/us_board_ledger/retro_grades.parquet`,
  macro-side). Rejected for v1: it couples the bot to the macro store layout + a parquet dependency
  and breaks the "single JSON contract, macro owns the projection" seam discipline. A JSON projection
  keeps the surfaces decoupled and R2-eligible. Revisit only if the projection proves lossy.

**Ruling needed:** which path macro publishes to (A vs B). The reader already prefers A, falls back to
B; picking one lets the other be dropped.

---

## 7. Candidacy when `gate_go=false` — OPEN (operator ruling #b)

**Candidacy ≠ sizing.** The re-grounding above broadens the divergence detector's point-in-time
TRIGGER membership only — it never lifts the `gate_go` cap on SIZE. The open question is narrower and
downstream:

> When the live board reads `gate_go=false`, should the bot's *candidacy funnel* still CONSIDER names
> the ledger shows as currently `on_board=true` (or recently surfaced + `status=running`) — as
> starter-grade candidates for the gate to filter — or should a gate-failed board suppress candidacy
> entirely?

Arguments both ways:

- **Consider them (candidacy-open):** a name that surfaced, is `running`, and is still `on_board` has
  a live, positive forward grade regardless of the board-level gate. Adding it as a *candidate for the
  gate to filter* costs nothing (the gate + sizing discipline still apply) and is exactly the
  early-eyes intent — `gate_go` governs the BOARD's aggregate conviction, not any single name's
  divergence.
- **Suppress them (candidacy-closed):** `gate_go=false` is the macro side's honest "this surface is
  not validated right now" — honoring it at candidacy keeps the bot from leaning on a surface its own
  author has flagged as unreliable, at the cost of missing the occasional AAPL-style single-name
  diverger during a gated regime.

**Default until ruled:** candidacy stays gated by `MASTERMIND_DIVERGENCE_CLUE` (default OFF); the
re-grounding only changes the historical/point-in-time TRIGGER data source, byte-identically for a
default-OFF consumer. The candidacy-consideration question is deferred to this ruling — it does not
change sizing either way.

---

## 8. The absence bridge (until the macro export ships)

The macro export does not yet exist in any checkout. Until it ships, `board_track_record` reads NONE
of it and every accessor fail-softs to its empty read — the module is provably inert, and the
divergence re-grounding degrades cleanly (the ledger leg contributes nothing, the volatile-board leg
is unchanged). The committed fixture (`tests/fixtures/us_board_track_record.json`) is the executable
reference semantics for the macro emitter and the assertion surface for the bot mechanism.
