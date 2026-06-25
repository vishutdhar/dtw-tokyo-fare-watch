#!/usr/bin/env python3
"""DTW -> Tokyo nonstop fare watcher.

Searches each configured Tokyo airport (HND, NRT) on Google Flights via the
`fast-flights` library and writes the dashboard's data/state.json. Records only;
it does not book, hold, or modify any reservation.

Run from cron, e.g.:  python3 watcher/fare_watch.py
Dry run (no writes):  python3 watcher/fare_watch.py --dry-run

Scope notes (per maintainer guidance):
- Nonstop/direct only — connecting flights are never recorded.
- Writes data/state.json only; it does NOT send Discord alerts or touch cron.
  Whatever consumes the alert flags (e.g. Hermes -> Discord) keeps its semantics
  as long as those flags are preserved, which they are.
- GOOD_FARE_TOTAL / MATERIAL_DROP thresholds below drive the alert flags; confirm
  they match the existing Hermes values before pointing live cron at this copy.
"""
from __future__ import annotations
import json, os, re, sys
from datetime import datetime, timezone

# ---- config -----------------------------------------------------------------
CONFIG = {
    "origin": "DTW",
    "destination": "Tokyo",
    "airports": ["HND", "NRT"],          # add/remove Tokyo airports here
    "dates": {"depart": "2026-11-20", "return": "2026-11-29"},
    "adults": 2,
    "seat": "economy",
    "nonstop_direct_only": True,
    "good_fare_total": 3000,             # alert at/below this total
    "material_drop_usd": 150,            # "material" drop threshold vs prior same-airport check
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
    actual fetch error (counted as a backend error by the caller).
    """
    from fast_flights import FlightData, Passengers, get_flights  # lazy import so tests don't need it
    o, dates = cfg["origin"], cfg["dates"]
    legs = [
        FlightData(date=dates["depart"], from_airport=o, to_airport=airport, max_stops=0),
        FlightData(date=dates["return"], from_airport=airport, to_airport=o, max_stops=0),
    ]
    res = get_flights(flight_data=legs, trip="round-trip", seat=cfg["seat"],
                      passengers=Passengers(adults=cfg["adults"]), fetch_mode="fallback")
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
    good = cfg["good_fare_total"]; material = cfg["material_drop_usd"]
    valid = [o for o in observations if isinstance(o.get("price_total_usd"), (int, float))]
    # per-observation signals: compare each obs to the previous obs of the SAME airport
    seen_prev = {}
    for o in valid:
        a = o.get("airport")
        prev = seen_prev.get(a)
        price = o["price_total_usd"]
        o["price_dropped"] = bool(prev is not None and price < prev)
        o["material_price_drop"] = bool(prev is not None and (prev - price) >= material)
        o["materially_good_fare"] = bool(price <= good)
        o["alert"] = bool(o["material_price_drop"] or o["materially_good_fare"])
        seen_prev[a] = price
    if not valid:
        return {"observation_count": len(observations), "consecutive_errors": 0}
    # latest observation per airport -> cheapest of those = "current best"
    latest_by_airport = {}
    for o in valid:
        latest_by_airport[o["airport"]] = o  # valid is in chronological order
    current = min(latest_by_airport.values(), key=lambda o: o["price_total_usd"])
    best = min(valid, key=lambda o: o["price_total_usd"])
    return {
        "current_price_total_usd": current["price_total_usd"],
        "best_price_total_usd": best["price_total_usd"],
        "best_observed_at": best["checked_at"],
        "last_airport": current["airport"],
        "last_carrier": current["carrier"],
        "last_google_price_band": current["google_price_band"],
        "last_checked_at": max(o["checked_at"] for o in valid),
        "observation_count": len(observations),
    }

def build_state(cfg, prior_state, new_observations, now_iso, errors=0):
    """Pure: merge new observations into prior state and rebuild the file."""
    observations = list(prior_state.get("observations", [])) + list(new_observations)
    stats = recompute(cfg, observations)
    stats["consecutive_errors"] = errors
    # Keep the fare-search links alive: newest observation with a URL, else the
    # prior state's, else any observation's — never null them out on a quiet run.
    top_source = (next((o.get("source_url") for o in reversed(observations) if o.get("source_url")), None)
                  or prior_state.get("source_url"))
    return {
        "caveats": CAVEATS,
        "dashboard_url": DASHBOARD_URL,
        "generated_at": now_iso,
        "observations": observations,
        "source_url": top_source,
        "stats": stats,
        "status": "ok" if errors == 0 else "degraded",
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
    """Search every configured airport and return (state, new_obs, errors)."""
    new_obs, errors = [], 0
    for airport in cfg["airports"]:
        try:
            obs = search_airport(cfg, airport, now_iso)
        except Exception as exc:  # a real fetch error — one airport failing shouldn't abort
            errors += 1
            print(f"[warn] {airport} search failed: {exc}", file=sys.stderr)
            continue
        if obs is None:           # no valid nonstop/direct result — skip, not an error
            print(f"[info] {airport}: no nonstop/direct priced result this run; skipped.", file=sys.stderr)
            continue
        new_obs.append(obs)
    if not new_obs and errors:    # nothing recorded and at least one hard failure → carry the streak
        errors += prior.get("stats", {}).get("consecutive_errors", 0)
    state = build_state(cfg, prior, new_obs, now_iso, errors=errors)
    return state, new_obs, errors

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    prior = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as fh:
            prior = json.load(fh)
    state, new_obs, errors = run(CONFIG, prior, now_iso)
    payload = json.dumps(state, indent=2, sort_keys=True)
    if dry_run:
        # Show what would be written; touch nothing.
        print(payload)
        print(f"[dry-run] {len(new_obs)} new obs; "
              f"current={state['stats'].get('current_price_total_usd')} "
              f"via {state['stats'].get('last_airport')}; errors={errors}", file=sys.stderr)
        return
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as fh:
        fh.write(payload + "\n")
    print(f"Wrote {STATE_PATH}: {len(new_obs)} new obs, "
          f"current={state['stats'].get('current_price_total_usd')} "
          f"via {state['stats'].get('last_airport')}")

if __name__ == "__main__":
    main()
