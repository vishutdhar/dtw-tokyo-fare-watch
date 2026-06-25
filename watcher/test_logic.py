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
T1, T2 = "2026-06-25T12:00:00+00:00", "2026-06-25T14:00:00+00:00"


class MockFlight:
    def __init__(self, name, price, dep="d", arr="a", dur="13 hr 45 min", stops=0):
        self.name, self.price = name, price
        self.departure, self.arrival, self.duration, self.stops = dep, arr, dur, stops


def _obs(airport, price, band="high", t=T2):
    return fw.build_observation(cfg, airport, MockFlight("Delta", f"${price:,}"), band, t)


def test_build_observation_schema():
    o = _obs("HND", 4231)
    assert o["price_total_usd"] == 4231 and o["carrier"] == "Delta"
    assert o["airport"] == "HND" and o["google_price_band"] == "high"


def test_recompute_current_is_cheapest_latest_per_airport():
    obs = [_obs("HND", 4231, t=T1), _obs("NRT", 4400, t=T1),
           _obs("HND", 4031, t=T2), _obs("NRT", 3851, "typical", t=T2)]  # NRT now cheapest
    stats = fw.recompute(cfg, obs)
    assert stats["current_price_total_usd"] == 3851      # cheapest of the latest run
    assert stats["last_airport"] == "NRT"                # via airport agrees with price
    assert stats["best_price_total_usd"] == 3851         # global min
    assert stats["observation_count"] == 4
    assert obs[3]["price_dropped"] and obs[3]["material_price_drop"]   # 4400 -> 3851 = $549 / 12%
    assert not obs[3]["materially_good_fare"]


def test_good_fare_alert():
    cheap = _obs("HND", 2950, "low")
    fw.recompute(cfg, [cheap])
    assert cheap["materially_good_fare"] and cheap["alert"]


def test_material_drop_requires_dollars_and_pct():
    def material(p1, p2):
        obs = [_obs("HND", p1, t=T1), _obs("HND", p2, t=T2)]
        fw.recompute(cfg, obs)
        return obs[1]["material_price_drop"], obs[1]["price_dropped"]
    assert material(4000, 3880) == (False, True)   # $120 / 3%  -> drop yes, material no (fails pct)
    assert material(4000, 3750) == (True, True)    # $250 / 6.25% -> material
    assert material(1500, 1400) == (True, True)    # $100 / 6.67% -> material (both thresholds met)
    assert material(4000, 3910) == (False, True)   # $90        -> material no (fails dollars)
    assert material(4000, 4000) == (False, False)  # no change


def test_skipped_airport_not_treated_as_current():
    """An airport skipped in the latest run must not surface as current via a stale row."""
    obs = [_obs("NRT", 2800, "low", t=T1), _obs("HND", 4100, t=T1),
           _obs("HND", 4000, t=T2)]  # latest run: HND only
    stats = fw.recompute(cfg, obs)
    assert stats["current_price_total_usd"] == 4000   # not the stale 2800
    assert stats["last_airport"] == "HND"
    assert stats["best_price_total_usd"] == 2800       # lowest-ever still records it
    assert stats["last_checked_at"] == T2


def test_nonstop_skip_is_not_an_error():
    """No nonstop result for an airport -> skipped, not counted as a failure."""
    def fake(c, airport, now):
        return None if airport == "NRT" else _obs("HND", 4031, t=now)
    orig, fw.search_airport = fw.search_airport, fake
    try:
        state, new_obs, info = fw.run(cfg, {"observations": []}, T2)
    finally:
        fw.search_airport = orig
    assert [o["airport"] for o in new_obs] == ["HND"]
    assert info["degraded"] is False and info["errored"] == []
    assert state["stats"]["consecutive_errors"] == 0 and state["status"] == "ok"


def test_partial_failure_optional_airport_not_degraded():
    """NRT (optional) failing while HND (required) succeeds must NOT degrade the run."""
    def fake(c, airport, now):
        if airport == "NRT":
            raise RuntimeError("401 no token provided")
        return _obs("HND", 4031, t=now)
    orig, fw.search_airport = fw.search_airport, fake
    try:
        state, new_obs, info = fw.run(cfg, {"observations": [], "stats": {"consecutive_errors": 3}}, T2)
    finally:
        fw.search_airport = orig
    assert [o["airport"] for o in new_obs] == ["HND"]
    assert info["degraded"] is False and info["errored"] == ["NRT"]
    assert state["status"] == "ok"
    assert state["stats"]["consecutive_errors"] == 0   # streak resets — HND is healthy


def test_required_airport_no_result_is_degraded():
    """A required airport returning no nonstop result (not just an exception) degrades the run."""
    def fake(c, airport, now):
        return None if airport == "HND" else _obs("NRT", 3851, "typical", t=now)
    orig, fw.search_airport = fw.search_airport, fake
    try:
        state, new_obs, info = fw.run(cfg, {"observations": [], "stats": {"consecutive_errors": 1}}, T2)
    finally:
        fw.search_airport = orig
    assert info["degraded"] is True and info["missing_required"] == ["HND"]
    assert state["status"] == "degraded"
    assert state["stats"]["consecutive_errors"] == 2   # prior 1 + 1


def test_required_failure_is_degraded_and_increments():
    """HND (required) failing degrades the run and advances the error streak."""
    def fake(c, airport, now):
        if airport == "HND":
            raise RuntimeError("boom")
        return _obs("NRT", 3851, "typical", t=now)
    orig, fw.search_airport = fw.search_airport, fake
    try:
        state, new_obs, info = fw.run(cfg, {"observations": [], "stats": {"consecutive_errors": 2}}, T2)
    finally:
        fw.search_airport = orig
    assert info["degraded"] is True and info["errored"] == ["HND"]
    assert state["status"] == "degraded"
    assert state["stats"]["consecutive_errors"] == 3   # prior 2 + 1


def test_summary_neutral_and_alert():
    # neutral run (no alert)
    state = fw.build_state(cfg, {"observations": []}, [_obs("HND", 4031), _obs("NRT", 4200)], T2)
    lines = fw.summarize(cfg, state)
    joined = "\n".join(lines)
    assert "HND $4,031" in joined and "NRT $4,200" in joined
    assert "best $4,031 via HND" in joined and "over target" in joined
    assert "ALERT" not in joined
    # good-fare run (alert line present)
    state2 = fw.build_state(cfg, {"observations": []}, [_obs("NRT", 2900, "low")], T2)
    j2 = "\n".join(fw.summarize(cfg, state2))
    assert "ALERT" in j2 and "good fare" in j2 and "at/below target" in j2


def test_source_url_built_and_preserved():
    u = fw.search_url(cfg, "HND")
    assert u.startswith("https://www.google.com/travel/flights/search?q=")
    assert "HND" in u and "nonstop" in u
    obs = [fw.build_observation(cfg, "HND", MockFlight("Delta", "$4,031"), "high", T2, source_url=u)]
    s1 = fw.build_state(cfg, {"observations": []}, obs, T2)
    assert s1["source_url"] == u and s1["observations"][-1]["source_url"] == u
    # a quiet run (no new obs) must not null out a previously-known URL
    prior = {"observations": [_obs("HND", 4031)], "source_url": u}
    s2 = fw.build_state(cfg, prior, [], T2)
    assert s2["source_url"] == u


def test_build_state_matches_existing_schema():
    """Every key the dashboard's data/state.json uses must still be produced."""
    if not os.path.exists(STATE_PATH):
        return
    real = json.load(open(STATE_PATH))
    state = fw.build_state(cfg, {"observations": []}, [_obs("HND", 4031)], T2)
    assert not (set(real) - set(state)), set(real) - set(state)
    assert not (set(real["observations"][0]) - set(state["observations"][0]))
    assert not (set(real["stats"]) - set(state["stats"]))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("ALL TESTS PASSED")
