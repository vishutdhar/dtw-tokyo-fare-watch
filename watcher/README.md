# Fare watcher

Versioned copy of the DTW → Tokyo nonstop fare watcher that produces this
repo's `data/state.json` (the file the dashboard reads). It records fares only —
it does **not** book, hold, or modify any reservation.

This is the repo copy for review. Pointing Hermes cron at it is a separate,
deliberate step (see *Deploying*).

## What it does

- Searches each configured Tokyo airport (`HND`, `NRT`) for **nonstop/direct**,
  round-trip economy fares on the watched dates via `fast-flights` (Google Flights).
- Appends one observation per airport per run and rewrites `data/state.json` with
  the schema the dashboard expects (observations + aggregate stats).
- `current` = cheapest fare of the **most recent run** (ties prefer HND);
  `best` = global minimum across all history.
- Prints concise Discord-style summary lines to stdout for the cron caller to
  forward, and (optionally) commits/pushes the data update.

## Behavior / constraints

- **Nonstop/direct only.** An airport with no valid nonstop priced result in a run
  is skipped — a connecting flight is never substituted.
- **Partial failure tolerated.** Only `required_airports` (HND) gate run health: if
  NRT fails or returns nothing while HND succeeds, the run stays `ok` and the
  `consecutive_errors` streak does not advance. A required airport failing marks the
  run `degraded` and increments the streak.
- **Alert semantics (match Hermes):**
  - `good_fare_total = 3000` — good fare at/below this total.
  - **material drop = down ≥ $100 AND ≥ 5%** vs the prior same-airport check
    (`material_min_drop_usd` / `material_min_drop_pct`).
  - Existing alert fields preserved: `alert`, `materially_good_fare`,
    `material_price_drop`, `price_dropped`.
- **No dashboard fields removed** — output is a superset (adds `watch.airports`).
- **Discord untouched.** This script never posts to Discord; it emits summary lines
  for the cron caller. Whatever consumes the alert flags keeps its semantics.
- **Publish is scoped.** The publish step stages and commits **only**
  `data/state.json` — never `index.html` or anything else.

> ⚠️ Confirm the thresholds above match the existing Hermes values before pointing
> live cron here.

> ℹ️ If `fast-flights` returns `401 "no token provided"` on the hosted `fallback`
> fetch, set `CONFIG["fetch_mode"]` to `"local"` (needs Playwright) or `"common"`.
> NRT failing this way no longer degrades the run as long as HND succeeds.

## Usage

```bash
pip install -r watcher/requirements.txt

python3 watcher/fare_watch.py --dry-run      # print state.json + summary; writes nothing, no publish
python3 watcher/fare_watch.py --no-publish   # write data/state.json but do not commit/push
python3 watcher/fare_watch.py --summary-only # just print the summary from the existing state.json
python3 watcher/fare_watch.py                # write state.json, print summary, commit+push data/state.json
```

Configuration (origin, airports, required airports, dates, passengers, fetch mode,
thresholds) is the `CONFIG` dict at the top of `fare_watch.py`.

## Tests

```bash
python3 watcher/test_logic.py
```

Covers (mock flights, no network): schema mapping, cross-airport `current`/`best`,
the **$100-AND-5%** material-drop rule, the good-fare alert, the nonstop-skip path,
**partial-failure / required-vs-optional** degraded behavior, the summary lines, and
source-URL generation/preservation. The live `fast-flights` fetch in
`search_airport()` is the only part not exercised — verify it with `--dry-run`.

## Deploying (manual, deliberate)

1. `python3 watcher/test_logic.py` (all pass, no network).
2. `--dry-run` on the target host: confirm a sane `state.json`, that NRT returns
   nonstop results (or fails gracefully without degrading), and review the summary.
3. Confirm the alert thresholds match Hermes and that the summary lines match the
   current Discord output.
4. Only then point the Hermes cron job at this copy.
