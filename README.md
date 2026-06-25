# DTW → Tokyo Fare Watch

Static GitHub Pages dashboard for the Hermes DTW→Tokyo fare watcher.

- Live URL target: https://vishutdhar.github.io/dtw-tokyo-fare-watch/
- Source history: `/Users/openclaw/.hermes-discord/profiles/leo/cron/state/dtw_tokyo_fare_watch.json`
- Dashboard data: `data/state.json`

The watcher records fare observations only. It does not book, purchase, hold, or modify travel reservations.

## Update safety

Future frontend/design updates should edit `index.html` in this repo. The Hermes publisher now preserves `index.html` and only refreshes `data/state.json` unless the template rebuild override is explicitly enabled.

Before calling an update done, run:

```bash
/Users/openclaw/.hermes-discord/profiles/leo/scripts/dtw_tokyo_fare_dashboard_check.py --local --verbose
/Users/openclaw/.hermes-discord/profiles/leo/scripts/dtw_tokyo_fare_dashboard_check.py --live --verbose
```

The live check must pass after GitHub Pages redeploys. A separate Hermes watchdog also runs this check silently and only posts if it fails.
