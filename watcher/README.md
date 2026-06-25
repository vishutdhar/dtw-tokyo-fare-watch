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
- **Discord untouched.** This script only writes `state.json`. It does not send
  alerts. Whatever consumes the alert flags (`alert`, `materially_good_fare`,
  `material_price_drop`, `price_dropped`) keeps its semantics as long as those
  flags are preserved — which they are.

> ⚠️ **Confirm thresholds before going live.** `GOOD_FARE_TOTAL` (3000) and
> `MATERIAL_DROP` (150) in `fare_watch.py` drive the alert flags. Make sure they
> match the existing Hermes values so Discord alerting behaves identically.

## Usage

```bash
pip install -r watcher/requirements.txt

# Dry run — prints the state.json that WOULD be written; touches nothing:
python3 watcher/fare_watch.py --dry-run

# Real run — rewrites data/state.json:
python3 watcher/fare_watch.py
```

Configuration (origin, airports, dates, passengers, thresholds) is the `CONFIG`
dict at the top of `fare_watch.py`. Adding another Tokyo airport is a one-line
edit to `airports`.

## Tests

```bash
python3 watcher/test_logic.py
```

Covers the pure logic with mock flights (no network): schema mapping, the
cross-airport `current`/`best` stats, alert flags, and the nonstop-skip path.
The live `fast-flights` fetch is isolated in `search_airport()` and is the only
part not exercised by the tests — verify it with `--dry-run` on the target host.

## Deploying (manual, deliberate)

1. `--dry-run` on the target host; confirm a sane `state.json` and that NRT
   returns nonstop results.
2. Confirm the alert thresholds match Hermes.
3. Only then point the Hermes cron job at this copy. This PR does not change cron.
