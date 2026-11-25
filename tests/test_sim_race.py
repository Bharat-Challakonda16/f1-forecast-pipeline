from __future__ import annotations

import numpy as np
import pandas as pd

from f1_forecast.sim.race import POINTS, simulate_race
from f1_forecast.sim.strategy import candidate_plans, plan_probabilities, stint_cost

TYRES = pd.DataFrame(
    {
        "compound": ["SOFT", "MEDIUM", "HARD"],
        "wear_pct_per_lap_35c": [0.08, 0.05, 0.035],
        "wear_pct_per_lap_per_10c": [0.0, 0.0, 0.0],
        "fresh_pace_vs_medium_pct": [-0.25, 0.0, 0.2],
        "max_stint_laps": [25, 35, 45],
    }
)


def circuit(**kw):
    c = {
        "laps": 50,
        "base_lap_ms": 90_000.0,
        "pit_loss_s": 22.0,
        "track_temp_c": 35.0,
        "passes_per_green_lap": 0.5,
        "sc_prob": 0.0,
        "vsc_prob": 0.0,
        "red_flag_prob": 0.0,
        "lap1_incident_prob": 0.0,
        "speed_profile": None,
    }
    c.update(kw)
    return c


def field(n=6, pace_gap=0.3, reliability=0.0, error=0.0, se=0.0, form=0.0):
    teams = pd.DataFrame(
        {
            "constructor_id": [f"t{i}" for i in range(n // 2)],
            "car_race": [i * pace_gap for i in range(n // 2)],
            "car_race_se": se,
            "form_sd": form,
            "reliability_dnf_rate": reliability,
        }
    )
    drivers = pd.DataFrame(
        {
            "driver_id": [f"d{i}" for i in range(n)],
            "constructor_id": [f"t{i // 2}" for i in range(n)],
            "race_pace": 0.0,
            "race_pace_se": 0.0,
            "consistency": 0.0,
            "tyre_wear": 0.0,
            "starts": 0.0,
            "error_rate": error,
        }
    )
    return drivers, teams


def grid_in_order(sims, n):
    return np.tile(np.arange(1, n + 1), (sims, 1))


def test_stint_cost_and_plans_respect_two_compound_rule():
    assert stint_cost(0.0, 0.1, 3) == 0.1 * 3  # ages 0,1,2
    plans = candidate_plans(TYRES, laps=50, base_lap_ms=90_000, pit_loss_s=22)
    assert all(len(set(p.compounds)) >= 2 for p in plans)
    assert all(p.pit_laps == tuple(sorted(p.pit_laps)) for p in plans)
    probs = plan_probabilities(plans)
    assert np.isclose(probs.sum(), 1) and probs[0] == probs.max()


def test_fastest_car_wins_most_and_points_add_up():
    d, t = field(pace_gap=0.6)
    r = simulate_race(d, t, TYRES, circuit(), grid_in_order(2000, 6), n_replays=0)
    s = r.summary().set_index("driver_id")
    assert s["p_win"].idxmax() in ("d0", "d1")
    assert np.isclose(s["p_win"].sum(), 1.0)
    pts = np.where(r.position <= 10, POINTS[np.clip(r.position - 1, 0, 9)], 0).sum(axis=1)
    assert np.allclose(pts, POINTS[:6].sum())  # 6 finishers every race


def test_every_finisher_uses_two_compounds_and_stops():
    d, t = field()
    r = simulate_race(d, t, TYRES, circuit(), grid_in_order(500, 6), n_replays=0)
    assert (r.stops[~r.dnf] >= 1).all()
    assert all(len(set(lab.split("-"))) >= 2 for lab in np.unique(r.strategy))


def test_hard_to_pass_track_protects_the_car_in_front():
    # a faster car starts last: it gains more places where overtaking is easy
    d, t = field(n=6, pace_gap=0.0)
    d.loc[d.driver_id == "d5", "race_pace"] = -0.8
    g = grid_in_order(1500, 6)
    easy = simulate_race(d, t, TYRES, circuit(passes_per_green_lap=1.5), g, n_replays=0)
    hard = simulate_race(d, t, TYRES, circuit(passes_per_green_lap=0.05), g, n_replays=0)
    assert easy.position[:, 5].mean() < hard.position[:, 5].mean() - 0.5
    assert easy.passes_per_green_lap > hard.passes_per_green_lap


def test_unreliable_cars_retire():
    d, t = field(reliability=1.0)
    r = simulate_race(d, t, TYRES, circuit(), grid_in_order(300, 6), n_replays=0)
    assert r.dnf.all() and (r.dnf_cause == 1).all()


def test_safety_car_happens_at_the_given_rate():
    d, t = field()
    r = simulate_race(d, t, TYRES, circuit(sc_prob=0.5), grid_in_order(2000, 6), n_replays=0)
    assert 0.45 < (r.sc_laps > 0).mean() < 0.55


def test_replays_record_positions_every_lap():
    d, t = field()
    r = simulate_race(d, t, TYRES, circuit(), grid_in_order(10, 6), n_replays=2)
    assert len(r.replays) == 2 and len(r.replays[0]["positions"]) == 49
    assert sorted(r.replays[0]["positions"][-1]) == list(range(6))


def test_calibration_hits_the_measured_overtaking_rate():
    from f1_forecast.sim.race import calibrate_pass_k

    d, t = field(n=8, pace_gap=0.15, form=0.2)
    g = np.tile(np.random.default_rng(0).permutation(8) + 1, (600, 1))
    for target in (0.10, 0.40):
        c = calibrate_pass_k(d, t, TYRES, circuit(), g, target=target, sims=600)
        assert abs(c.achieved - target) / target < 0.15
        assert c.slipstream == 0.0  # reachable without a tow
        r = simulate_race(d, t, TYRES, circuit(), g, seed=5, n_replays=0, **c.race_kwargs())
        assert abs(r.passes_per_green_lap - target) / target < 0.25  # holds on a new seed


def test_unreachable_rate_switches_on_the_slipstream():
    from f1_forecast.sim.race import calibrate_pass_k

    d, t = field(n=8, pace_gap=0.05)
    g = np.tile(np.arange(1, 9), (400, 1))
    c = calibrate_pass_k(d, t, TYRES, circuit(), g, target=3.0, sims=400, iterations=6)
    assert c.slipstream > 0 and c.threshold_s < 0.2 and c.dirty_air_pct < 0.25


def test_strategy_consensus_shares_stop_count():
    from f1_forecast.sim.race import strategy_choices
    from f1_forecast.sim.strategy import Plan

    plans = [Plan((0, 1), (20,), 0.0), Plan((1, 2), (25,), 1.0), Plan((0, 1, 1), (15, 35), 0.5)]
    probs = np.array([0.4, 0.2, 0.4])
    rng = np.random.default_rng(0)
    c = strategy_choices(plans, probs, 500, 10, rng, follow=1.0)
    stops = np.array([p.stops for p in plans])[c]
    assert (stops == stops[:, :1]).all()  # everyone follows the race's stop count
    free = strategy_choices(plans, probs, 500, 10, rng, follow=0.0)
    assert not (np.array([p.stops for p in plans])[free] == 1).all(axis=1).all()
