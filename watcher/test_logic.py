"""Unit tests for the watcher's pure logic (no network / no fast-flights needed).

Run:  python3 watcher/test_logic.py
Exits non-zero on any failure.
"""
import json
import os
import fare_watch as fw

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(REPO_ROOT, "data", "state.json")
cfg = fw.CONFIG


class MockFlight:
    def __init__(self, name, price, dep="d", arr="a", dur="13 hr 45 min", stops=0):
        self.name, self.price = name, price
        self.departure, self.arrival, self.duration, self.stops = dep, arr, dur, stops


def test_build_observation_schema():
    f = MockFlight("Delta", "$4,231")
    o = fw.build_observation(cfg, "HND", f, "high", "2026-06-25T14:00:00+00:00")
    assert o["price_total_usd"] == 4231 and o["carrier"] == "Delta"
    assert o["airport"] == "HND" and o["google_price_band"] == "high"


def test_recompute_current_is_cheapest_latest_per_airport():
    t1, t2 = "2026-06-25T12:00:00+00:00", "2026-06-25T14:00:00+00:00"
    obs = [
        fw.build_observation(cfg, "HND", MockFlight("Delta", "$4,231"), "high", t1),
        fw.build_observation(cfg, "NRT", MockFlight("Delta", "$4,400"), "high", t1),
        fw.build_observation(cfg, "HND", MockFlight("Delta", "$4,031"), "high", t2),
        fw.build_observation(cfg, "NRT", MockFlight("Delta", "$3,851"), "typical", t2),
    ]
    stats = fw.recompute(cfg, obs)
    assert stats["current_price_total_usd"] == 3851          # cheapest of latest-per-airport
    assert stats["last_airport"] == "NRT"                    # via airport agrees with price
    assert stats["best_price_total_usd"] == 3851             # global min
    assert stats["observation_count"] == 4
    nrt_latest = obs[3]
    assert nrt_latest["price_dropped"] and nrt_latest["material_price_drop"]
    assert not nrt_latest["materially_good_fare"]


def test_good_fare_alert():
    t = "2026-06-25T14:00:00+00:00"
    cheap = fw.build_observation(cfg, "HND", MockFlight("Delta", "$2,950"), "low", t)
    fw.recompute(cfg, [cheap])
    assert cheap["materially_good_fare"] and cheap["alert"]


def test_nonstop_skip_is_not_an_error():
    """No nonstop result for an airport -> skipped, not counted as a backend error."""
    def fake_search(c, airport, now):
        if airport == "NRT":
            return None  # simulate no nonstop/direct priced result
        return fw.build_observation(c, airport, MockFlight("Delta", "$4,031"), "high", now)
    orig = fw.search_airport
    fw.search_airport = fake_search
    try:
        state, new_obs, errors = fw.run(cfg, {"observations": []}, "2026-06-25T14:00:00+00:00")
    finally:
        fw.search_airport = orig
    assert [o["airport"] for o in new_obs] == ["HND"]
    assert errors == 0
    assert state["stats"]["last_airport"] == "HND"


def test_source_url_built_and_preserved():
    u = fw.search_url(cfg, "HND")
    assert u.startswith("https://www.google.com/travel/flights/search?q=")
    assert "HND" in u and "nonstop" in u
    t = "2026-06-25T14:00:00+00:00"
    # new observations carry the search URL -> top-level reflects it
    obs = [fw.build_observation(cfg, "HND", MockFlight("Delta", "$4,031"), "high", t, source_url=u)]
    s1 = fw.build_state(cfg, {"observations": []}, obs, t)
    assert s1["source_url"] == u and s1["observations"][-1]["source_url"] == u
    # a quiet run (no new obs) must not null out a previously-known URL
    prior = {"observations": [fw.build_observation(cfg, "HND", MockFlight("Delta", "$4,031"), "high", t)],
             "source_url": u}
    s2 = fw.build_state(cfg, prior, [], t)
    assert s2["source_url"] == u


def test_build_state_matches_existing_schema():
    """Every key the dashboard's data/state.json uses must still be produced."""
    if not os.path.exists(STATE_PATH):
        return  # nothing to compare against in this checkout
    real = json.load(open(STATE_PATH))
    t = "2026-06-25T14:00:00+00:00"
    obs = [fw.build_observation(cfg, "HND", MockFlight("Delta", "$4,031"), "high", t)]
    state = fw.build_state(cfg, {"observations": []}, obs, t, errors=0)
    assert not (set(real) - set(state)), set(real) - set(state)
    assert not (set(real["observations"][0]) - set(state["observations"][0]))
    assert not (set(real["stats"]) - set(state["stats"]))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("ALL TESTS PASSED")
