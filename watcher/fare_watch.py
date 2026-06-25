#!/usr/bin/env python3
"""DTW -> Tokyo nonstop fare watcher.

Searches each configured Tokyo airport (HND, NRT) on Google Flights via the
`fast-flights` library and writes the dashboard's data/state.json. Records only;
it does not book, hold, or modify any reservation.

Run from cron, e.g.:  python3 watcher/fare_watch.py
Dry run (no writes):  python3 watcher/fare_watch.py --dry-run

Scope notes (per maintainer guidance):
- Nonstop/direct only — connecting flights are never recorded.
- Writes data/state.json, prints a concise Discord-ready summary, and can
  commit/push the data update for GitHub Pages without overwriting index.html.
- GOOD_FARE_TOTAL / MATERIAL_DROP thresholds below match the existing Hermes
  watcher semantics before this copy is pointed at live cron.
"""
from __future__ import annotations
import json, os, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

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
    "material_drop_usd": 100,            # Hermes threshold: drop from last >= $100
    "material_drop_pct": 0.05,           # Hermes threshold: and >= 5%
}
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(REPO_ROOT, "data", "state.json")
DASHBOARD_URL = "https://vishutdhar.github.io/dtw-tokyo-fare-watch/"
GITHUB_REPO_URL = "https://github.com/vishutdhar/dtw-tokyo-fare-watch"
GITHUB_HOME = os.environ.get("DTW_TOKYO_GITHUB_HOME", "/Users/openclaw")
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
    good = cfg["good_fare_total"]
    material = cfg["material_drop_usd"]
    material_pct = cfg.get("material_drop_pct", 0.0)
    valid = [o for o in observations if isinstance(o.get("price_total_usd"), (int, float))]
    # per-observation signals: compare each obs to the previous obs of the SAME airport
    seen_prev = {}
    for o in valid:
        a = o.get("airport")
        prev = seen_prev.get(a)
        price = o["price_total_usd"]
        drop_abs = (prev - price) if prev is not None and price < prev else 0
        drop_pct = (drop_abs / prev) if prev else 0.0
        o["price_dropped"] = bool(prev is not None and price < prev)
        o["drop_abs_usd"] = drop_abs
        o["drop_pct"] = drop_pct
        o["material_price_drop"] = bool(o["price_dropped"] and drop_abs >= material and drop_pct >= material_pct)
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

def build_state(cfg, prior_state, new_observations, now_iso, errors=0):
    """Pure: merge new observations into prior state and rebuild the file."""
    observations = list(prior_state.get("observations", [])) + list(new_observations)
    stats = recompute(cfg, observations)
    stats["backend_errors_this_run"] = errors
    stats["consecutive_errors"] = 0 if new_observations else errors
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
        "status": "ok" if new_observations else ("ok" if errors == 0 else "degraded"),
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

def money(n):
    return "unknown" if n is None else f"${int(n):,}"


def yes_no(value):
    return "yes" if value else "no"


def latest_observation(state):
    stats = state.get("stats", {})
    ts = stats.get("last_checked_at")
    price = stats.get("current_price_total_usd")
    airport = stats.get("last_airport")
    candidates = [o for o in state.get("observations", [])
                  if o.get("checked_at") == ts and o.get("price_total_usd") == price and o.get("airport") == airport]
    if candidates:
        return candidates[-1]
    return state.get("observations", [])[-1] if state.get("observations") else None


def price_dropped_text(obs):
    if not obs or not obs.get("price_dropped"):
        return "no"
    drop = int(obs.get("drop_abs_usd") or 0)
    pct = float(obs.get("drop_pct") or 0.0)
    material = "material" if obs.get("material_price_drop") else "below alert threshold"
    return f"yes ({money(drop)} / {pct:.1%} from previous same-airport check; {material})"


def route_text(cfg, obs=None):
    airport = (obs or {}).get("airport") or "/".join(cfg["airports"])
    d = cfg["dates"]
    return f"{cfg['origin']}⇄{airport} nonstop/direct, {d['depart']}→{d['return']}, {cfg['adults']} adults, {cfg['seat']}"


def summary_text(cfg, state, errors, dry_run=False):
    stats = state.get("stats", {})
    obs = latest_observation(state)
    heading = "DTW→Tokyo fare watch summary"
    if obs and obs.get("alert"):
        heading = "🚨 DTW→Tokyo FARE ALERT"
    if dry_run:
        heading = "DRY RUN — " + heading
    source = (obs or {}).get("source_url") or state.get("source_url") or search_url(cfg, cfg["airports"][0])
    lines = [
        heading,
        f"searched_at: {state.get('generated_at')}",
        f"best_total_usd: {stats.get('current_price_total_usd', 'unknown')}",
        f"airline: {(obs or {}).get('carrier') or stats.get('last_carrier') or 'unknown'}",
        f"route: {route_text(cfg, obs)}",
        f"search_scope: {cfg['origin']}⇄{','.join(cfg['airports'])} nonstop/direct only; no connecting fare fallback",
        f"source_url: <{source}>",
        f"dashboard_url: <{DASHBOARD_URL}>",
        f"github_url: <{GITHUB_REPO_URL}>",
        f"price_dropped: {price_dropped_text(obs)}",
        f"materially_good_fare: {yes_no(bool((obs or {}).get('materially_good_fare')))}",
    ]
    if obs and obs.get("alert"):
        if obs.get("materially_good_fare"):
            reason = f"total is <= {money(cfg['good_fare_total'])}"
        else:
            reason = "material drop from previous same-airport check"
        lines.append(f"alert_reason: {reason}")
    caveats = list(CAVEATS)
    if errors and obs:
        caveats.append(f"secondary_backend_errors={errors}; successful airport recorded, run status remains ok")
    elif errors:
        caveats.append(f"backend_errors={errors}")
    lines.append("caveats: " + "; ".join(caveats) + ".")
    return "\n".join(lines)


def git_cmd(args, check=False):
    env = os.environ.copy()
    env["HOME"] = GITHUB_HOME
    return subprocess.run(args, cwd=REPO_ROOT, env=env, text=True, capture_output=True, check=check)


def has_git_remote():
    if not (Path(REPO_ROOT) / ".git").exists():
        return False
    res = git_cmd(["git", "remote", "get-url", "origin"])
    return res.returncode == 0 and bool(res.stdout.strip())


def sync_origin_ff_only():
    if not has_git_remote():
        return "no-remote"
    dirty = git_cmd(["git", "status", "--porcelain"])
    if dirty.stdout.strip():
        return "dirty-skip"
    fetch = git_cmd(["git", "fetch", "origin", "main"])
    if fetch.returncode != 0:
        return "fetch-failed"
    pull = git_cmd(["git", "pull", "--ff-only", "origin", "main"])
    return "synced" if pull.returncode == 0 else "sync-failed"


def commit_and_push_data():
    if not has_git_remote():
        return "no-remote"
    git_cmd(["git", "add", "data/state.json"])
    diff = git_cmd(["git", "diff", "--cached", "--quiet", "--", "data/state.json"])
    if diff.returncode == 0:
        return "no-change"
    commit = git_cmd(["git", "commit", "-m", "data: update fare watch state"])
    if commit.returncode != 0:
        return "commit-failed"
    push = git_cmd(["git", "push", "origin", "main"])
    return "pushed" if push.returncode == 0 else "push-failed"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv
    no_push = "--no-push" in argv or os.environ.get("DTW_TOKYO_DASHBOARD_DISABLE") == "1"
    sync_status = "dry-run" if dry_run else ("disabled" if no_push else sync_origin_ff_only())
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    prior = {}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as fh:
            prior = json.load(fh)
    state, new_obs, errors = run(CONFIG, prior, now_iso)
    payload = json.dumps(state, indent=2, sort_keys=True)
    if dry_run:
        print(summary_text(CONFIG, state, errors, dry_run=True))
        print(f"[dry-run] sync={sync_status}; writes=0; new_obs={len(new_obs)}; errors={errors}", file=sys.stderr)
        return 0
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as fh:
        fh.write(payload + "\n")
    push_status = "disabled" if no_push else commit_and_push_data()
    print(summary_text(CONFIG, state, errors, dry_run=False))
    print(f"publish_status: sync={sync_status}; push={push_status}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
