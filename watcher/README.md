# Fare watcher

Versioned copy of the DTW → Tokyo nonstop fare watcher that produces this
repo's `data/state.json` (the file the dashboard reads). It records fares only —
it does **not** book, hold, or modify any reservation.

This is the repo copy for review. It is **not** wired into live cron by this PR;
pointing Hermes cron at it is a separate, deliberate step (see *Deploying* below).

## What it does

- Searches each configured Tokyo airport (`HND`, `NRT`) for **nonstop/direct**,
  round-trip economy fares on the watched dates via the `fast-flights` library
  (Google Flights).
- Appends one observation per airport per run and rewrites `data/state.json` with
  the same schema the dashboard already expects (observations + aggregate stats).
- Prints a concise Discord-ready summary every run; strong alert wording appears
  only for material drops or good-fare triggers.
- By default, a real run commits/pushes only `data/state.json` after a fast-forward
  sync from `origin/main`; it never regenerates or overwrites `index.html`.
- Computes the headline numbers **across airports**: `current` is the cheapest of
  each airport's latest observation (ties prefer HND), `best` is the global
  minimum across all history.

## Constraints honored

- **Nonstop/direct only.** If an airport returns no valid nonstop priced result
  in a run, it is skipped — a connecting flight is never substituted.
- **Prefer HND, include NRT when valid.** `preferred_airport` stays `HND`; NRT is
  added whenever it has a valid nonstop priced result.
- **No dashboard fields removed.** Output is a superset of the current schema
  (adds `watch.airports`); every existing key is preserved.
- **Discord-compatible stdout.** The script prints stable summary lines including
  `searched_at`, `best_total_usd`, `airline`, `route`, `search_scope`,
  `source_url`, `price_dropped`, `materially_good_fare`, and `caveats`.
- **Partial airport failure is non-fatal.** If HND succeeds and NRT has a backend
  failure such as a transient `401 no token provided`, the run records HND,
  sets status `ok`, and reports the NRT error as a caveat.

> Alert thresholds are now aligned to the live Hermes watcher: good fare at/below
> `$3,000`; material drop requires both at least `$100` and at least `5%` down
> from the previous same-airport observation.

## Usage

```bash
pip install -r watcher/requirements.txt

# Dry run — prints the Discord summary that WOULD be delivered; touches nothing:
python3 watcher/fare_watch.py --dry-run

# Real run — fast-forward syncs, rewrites data/state.json, commits/pushes data only:
python3 watcher/fare_watch.py

# Local write test without git push:
python3 watcher/fare_watch.py --no-push
```

Configuration (origin, airports, dates, passengers, thresholds) is the `CONFIG`
dict at the top of `fare_watch.py`. Adding another Tokyo airport is a one-line
edit to `airports`.

## Tests

```bash
python3 watcher/test_logic.py
```

Covers the pure logic with mock flights (no network): schema mapping, the
cross-airport `current`/`best` stats, alert flags, Hermes threshold semantics,
partial airport backend failures, and the nonstop-skip path.
The live `fast-flights` fetch is isolated in `search_airport()` and is the only
part not exercised by the tests — verify it with `--dry-run` on the target host.

## Deploying (manual, deliberate)

1. `--dry-run` on the target host; confirm a sane Discord-ready summary and at
   least one successful nonstop/direct airport. NRT backend failure is acceptable
   when HND succeeds, but it should remain visible in caveats.
2. Confirm the alert thresholds still match Hermes.
3. Only then point the Hermes cron job at this copy. This PR does not change cron.
