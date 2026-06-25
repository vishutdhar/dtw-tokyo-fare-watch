#!/usr/bin/env python3
"""DTW -> Tokyo nonstop fare watcher.

Searches each configured Tokyo airport (HND, NRT) on Google Flights via the
`fast-flights` library and writes the dashboard's data/state.json. Records only;
it does not book, hold, or modify any reservation.

Usage:
  python3 watcher/fare_watch.py               # search, write state.json, print summary, publish
  python3 watcher/fare_watch.py --dry-run     # print state.json + summary; write nothing, publish nothing
  python3 watcher/fare_watch.py --no-publish  # write state.json but do NOT git commit/push
  python3 watcher/fare_watch.py --summary-only # just print the summary from the existing state.json

Behavior notes (per maintainer guidance):
- Nonstop/direct only — connecting flights are never recorded.
- Partial failure is tolerated: if a non-required airport (e.g. NRT) fails while a
  required airport (HND) succeeds, the run is NOT marked degraded and the error
  streak does not advance.
- Writes data/state.json only. The optional publish step commits ONLY that file
  (never index.html or anything else). It does not post to Discord — it prints
  concise summary lines on stdout for the cron caller to forward.
- Alert thresholds below are meant to match the existing Hermes semantics:
  good fare at/below $3,000; "material" drop = down >= $100 AND >= 5% vs the prior
  same-airport check. Confirm against Hermes before pointing live cron here.
"""
from __future__ import annotations
import json, os, re, subprocess, sys
from datetime import datetime, timezone

# ---- config -----------------------------------------------------------------
CONFIG = {
    "origin": "DTW",
    "destination": "Tokyo",
    "airports": ["HND", "NRT"],          # all tracked Tokyo airports
    "required_airports": ["HND"],        # a run is "degraded" only if one of these fails
    "dates": {"depart": "2026-11-20", "return": "2026-11-29"},
    "adults": 2,
    "seat": "economy",
    "nonstop_direct_only": True,
    # fast-flights fetch mode. If the hosted "fallback" returns 401 "no token
    # provided", try "local" (needs Playwright installed) or "common".
    "fetch_mode": "fallback",
    # alert semantics (match Hermes):
    "good_fare_total": 3000,             # good fare at/below this total
    "material_min_drop_usd": 100,        # material drop = down >= $100 ...
    "material_min_drop_pct": 0.05,       # ... AND down >= 5% vs the prior same-airport check
}
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(REPO_ROOT, "data", "state.json")
DASHBOARD_URL = "https://vishutdhar.github.io/dtw-tokyo-fare-watch/"
CAVEATS = [
    "Google Flights via fast-flights",
    "source_url is a search URL, not a booking link",
    "prices and availability can change",
    "no booking or purchase action",
]

def _money_to_int(s):
    digits = re.sub(r"[^\d]", "", str(s) or "")
    return int(digits) if digits else None

def search_url(cfg, airport):
    """Google Flights *search* URL (not a booking link) for one airport."""
    from urllib.parse import quote_plus
    d = cfg["dates"]
    q = (f"Google Flights {cfg['origin']} to {airport} round trip "
         f"{d['depart']} {d['return']} {cfg['adults']} adults nonstop {cfg['seat']}")
    return "https://www.google.com/travel/flights/search?q=" + quote_plus(q)

def search_airport(cfg, airport, now_iso):
    """Live search for one airport.

    Returns an observation dict, or None when there is no valid nonstop/direct
    priced result (we never substitute a connecting flight). Raises only on an
    actual fetch error (handled as a per-airport failure by the caller).
    """
    from fast_flights import FlightData, Passengers, get_flights  # lazy import so tests don't need it
    o, dates = cfg["origin"], cfg["dates"]
    legs = [
        FlightData(date=dates["depart"], from_airport=o, to_airport=airport, max_stops=0),
        FlightData(date=dates["return"], from_airport=airport, to_airport=o, max_stops=0),
    ]
    res = get_flights(flight_data=legs, trip="round-trip", seat=cfg["seat"],
                      passengers=Passengers(adults=cfg["adults"]),
                      fetch_mode=cfg.get("fetch_mode", "fallback"))
    # Strictly nonstop, with a parseable price. No fallback to connecting flights.
    nonstop = [f for f in res.flights
               if getattr(f, "stops", 0) in (0, None) and _money_to_int(getattr(f, "price", None))]
    if not nonstop:
        return None
    best = min(nonstop, key=lambda f: _money_to_int(f.price))
    return build_observation(cfg, airport, best, getattr(res, "current_price", None), now_iso,
                             source_url=search_url(cfg, airport))

def build_observation(cfg, airport, flight, price_band, now_iso, source_url=None):
    """Pure: turn a flight result into an observation dict (matches existing schema)."""
    return {
        "airport": airport,
        "carrier": getattr(flight, "name", None),
        "price_total_usd": _money_to_int(getattr(flight, "price", None)),
        "departure": getattr(flight, "departure", None),
        "arrival": getattr(flight, "arrival", None),
        "duration": getattr(flight, "duration", None),
        "google_price_band": price_band,
        "checked_at": now_iso,
        "source_url": source_url,
        # alert flags filled in by recompute()
        "alert": False, "material_price_drop": False,
        "materially_good_fare": False, "price_dropped": False,
    }

def recompute(cfg, observations):
    """Pure: given the full observation history (with this run's rows appended),
    set per-observation alert flags and return the aggregate stats block."""
    good = cfg["good_fare_total"]
    min_usd = cfg.get("material_min_drop_usd", 100)
    min_pct = cfg.get("material_min_drop_pct", 0.05)
    valid = [o for o in observations if isinstance(o.get("price_total_usd"), (int, float))]
    # per-observation signals: compare each obs to the previous obs of the SAME airport
    seen_prev = {}
    for o in valid:
        a = o.get("airport")
        prev = seen_prev.get(a)
        price = o["price_total_usd"]
        drop = (prev - price) if prev is not None else 0
        o["price_dropped"] = bool(prev is not None and price < prev)
        # "material" = down by at least $min_usd AND at least min_pct of the prior fare
        o["material_price_drop"] = bool(prev is not None and drop >= min_usd and drop >= min_pct * prev)
        o["materially_good_fare"] = bool(price <= good)
        o["alert"] = bool(o["material_price_drop"] or o["materially_good_fare"])
        seen_prev[a] = price
    if not valid:
        return {"observation_count": len(observations), "consecutive_errors": 0}
    # "current" reflects only the most recent run (max checked_at), so an airport
    # skipped this run never lingers as current via a stale, cheaper historical row.
    latest_ts = max(o["checked_at"] for o in valid)
    current_pool = [o for o in valid if o["checked_at"] == latest_ts]
    current = min(current_pool, key=lambda o: o["price_total_usd"])
    best = min(valid, key=lambda o: o["price_total_usd"])  # lowest-ever, any run/airport
    return {
        "current_price_total_usd": current["price_total_usd"],
        "best_price_total_usd": best["price_total_usd"],
        "best_observed_at": best["checked_at"],
        "last_airport": current["airport"],
        "last_carrier": current["carrier"],
        "last_google_price_band": current["google_price_band"],
        "last_checked_at": latest_ts,
        "observation_count": len(observations),
    }

def build_state(cfg, prior_state, new_observations, now_iso, consecutive_errors=0, degraded=False):
    """Pure: merge new observations into prior state and rebuild the file."""
    observations = list(prior_state.get("observations", [])) + list(new_observations)
    stats = recompute(cfg, observations)
    stats["consecutive_errors"] = consecutive_errors
    # Keep the fare-search links alive: newest observation with a URL, else the
    # prior state's — never null them out on a quiet run.
    top_source = (next((o.get("source_url") for o in reversed(observations) if o.get("source_url")), None)
                  or prior_state.get("source_url"))
    return {
        "caveats": CAVEATS,
        "dashboard_url": DASHBOARD_URL,
        "generated_at": now_iso,
        "observations": observations,
        "source_url": top_source,
        "stats": stats,
        "status": "degraded" if degraded else "ok",
        "watch": {
            "adults": cfg["adults"],
            "dates": cfg["dates"],
            "destination": cfg["destination"],
            "nonstop_direct_only": cfg["nonstop_direct_only"],
            "origin": cfg["origin"],
            "preferred_airport": cfg["airports"][0],
            "airports": cfg["airports"],
            "seat": cfg["seat"],
            "trip": "round-trip",
        },
    }

def run(cfg, prior, now_iso):
    """Search every airport, tolerating partial failure.

    Returns (state, new_obs, info). A run is "degraded" only when a *required*
    airport hard-fails; an optional airport failing (or returning no nonstop
    result) is recorded but never advances the error streak.
    """
    new_obs, errored = [], []
    for airport in cfg["airports"]:
        try:
            obs = search_airport(cfg, airport, now_iso)
        except Exception as exc:  # a real fetch error (e.g. fast-flights 401)
            errored.append(airport)
            print(f"[warn] {airport} search failed: {exc}", file=sys.stderr)
            continue
        if obs is None:           # no valid nonstop/direct result — skip, not an error
            print(f"[info] {airport}: no nonstop/direct priced result this run; skipped.", file=sys.stderr)
            continue
        new_obs.append(obs)
    required = set(cfg.get("required_airports") or [cfg["airports"][0]])
    degraded = bool(required & set(errored))     # a required airport hard-failed
    prior_streak = prior.get("stats", {}).get("consecutive_errors", 0)
    consecutive_errors = (prior_streak + 1) if degraded else 0
    state = build_state(cfg, prior, new_obs, now_iso,
                        consecutive_errors=consecutive_errors, degraded=degraded)
    info = {"new": len(new_obs), "errored": errored, "degraded": degraded}
    return state, new_obs, info

def summarize(cfg, state):
    """Concise, Discord-ready summary lines for the cron caller to forward.

    Neutral status every run; a strong ALERT line only on a material drop or a
    good fare (mirrors the existing Hermes posture)."""
    stats = state.get("stats", {})
    o, d = cfg["origin"], cfg["dates"]
    lines = [f"{o} ⇄ Tokyo {d['depart']} → {d['return']} · "
             f"{cfg['adults']} adults · {cfg['seat']} · nonstop"]
    valid = [x for x in state.get("observations", []) if isinstance(x.get("price_total_usd"), (int, float))]
    if not valid:
        lines.append("No priced nonstop results yet.")
        return lines
    latest_ts = max(x["checked_at"] for x in valid)
    run_obs = sorted((x for x in valid if x["checked_at"] == latest_ts), key=lambda x: x["price_total_usd"])
    per = " · ".join(f"{x['airport']} ${x['price_total_usd']:,} ({x.get('google_price_band') or '-'})"
                          for x in run_obs)
    cur, best = stats.get("current_price_total_usd"), stats.get("best_price_total_usd")
    lines.append(f"{per}  →  best ${cur:,} via {stats.get('last_airport')}")
    gft = cfg["good_fare_total"]
    if isinstance(cur, (int, float)):
        delta = cur - gft
        tail = f"${delta:,} over target" if delta > 0 else "at/below target ✅"
        lines.append(f"lowest observed ${best:,} · target ${gft:,} ({tail})")
    flagged = [x for x in run_obs if x.get("alert")]
    if flagged:
        f = flagged[0]
        why = []
        if f.get("materially_good_fare"): why.append("good fare")
        if f.get("material_price_drop"): why.append("material drop")
        lines.append(f"\U0001f6a8 ALERT {f['airport']} ${f['price_total_usd']:,} — {' & '.join(why)}")
    if state.get("status") == "degraded":
        lines.append(f"⚠️ degraded run (consecutive_errors={stats.get('consecutive_errors')})")
    return lines

def publish(commit_msg):
    """Commit & push ONLY data/state.json (never index.html or anything else)."""
    def git(*args):
        return subprocess.run(["git", "-C", REPO_ROOT, *args], capture_output=True, text=True)
    git("add", "--", "data/state.json")
    if git("diff", "--cached", "--quiet", "--", "data/state.json").returncode == 0:
        print("[publish] no data change to commit", file=sys.stderr)
        return
    c = git("commit", "-m", commit_msg)
    if c.returncode != 0:
        print(f"[publish] commit failed: {c.stderr.strip()}", file=sys.stderr)
        return
    p = git("push")
    if p.returncode != 0:
        print(f"[publish] push failed: {p.stderr.strip()}", file=sys.stderr)
        return
    print("[publish] pushed data/state.json", file=sys.stderr)

def _load_prior():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as fh:
            return json.load(fh)
    return {}

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv
    do_publish = ("--no-publish" not in argv) and not dry_run
    prior = _load_prior()
    if "--summary-only" in argv:
        for line in summarize(CONFIG, prior):
            print(line)
        return
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    state, new_obs, info = run(CONFIG, prior, now_iso)
    payload = json.dumps(state, indent=2, sort_keys=True)
    summary = summarize(CONFIG, state)
    if dry_run:
        print(payload)
        print("\n--- summary (cron/Discord) ---", file=sys.stderr)
        for line in summary:
            print(line, file=sys.stderr)
        print(f"[dry-run] new={info['new']} errored={info['errored']} "
              f"degraded={info['degraded']}; wrote nothing", file=sys.stderr)
        return
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as fh:
        fh.write(payload + "\n")
    # concise summary lines to stdout for the cron caller to forward to Discord
    for line in summary:
        print(line)
    if do_publish:
        publish(f"data: fare update {now_iso}")

if __name__ == "__main__":
    main()
