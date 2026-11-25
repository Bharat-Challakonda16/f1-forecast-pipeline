from __future__ import annotations

import numpy as np
import pandas as pd

from f1_forecast.sim.inputs import car_gaps, ewma, latest_trait_values, sane_compound_pace
from f1_forecast.sim.quali import simulate_qualifying


def field(car_fast=0.0, car_slow=1.0, se=0.0, form=0.0):
    teams = pd.DataFrame(
        {
            "constructor_id": ["fast", "slow"],
            "car_quali": [car_fast, car_slow],
            "car_quali_se": [se, se],
            "form_sd": [form, form],
        }
    )
    drivers = pd.DataFrame(
        {
            "driver_id": ["a", "b", "c", "d"],
            "constructor_id": ["fast", "fast", "slow", "slow"],
            "quali_pace": [-0.2, 0.0, -0.3, 0.1],
            "quali_pace_se": [0.0, 0.0, 0.0, 0.0],
        }
    )
    return drivers, teams


def test_no_noise_gives_deterministic_order():
    d, t = field()
    res = simulate_qualifying(d, t, sims=100, execution_sd=0.0)
    # a: -0.2, b: 0.0, c: 0.7, d: 1.1
    assert (res.grid[:, 0] == 1).all() and (res.grid[:, 3] == 4).all()


def test_probabilities_are_consistent():
    d, t = field(car_slow=0.3, se=0.1, form=0.2)
    s = simulate_qualifying(d, t, sims=4000).summary()
    assert np.isclose(s["p_pole"].sum(), 1.0)
    assert np.allclose(np.stack(s["grid_distribution"]).sum(axis=1), 1.0)
    assert np.allclose(np.stack(s["grid_distribution"]).sum(axis=0), 1.0)
    assert s.iloc[0]["driver_id"] == "a"  # fastest combination is most likely on pole
    assert (s["p_top3"] >= s["p_front_row"]).all() and (s["p_front_row"] >= s["p_pole"]).all()


def test_uncertainty_spreads_outcomes():
    d, t = field(car_slow=0.6)
    sure = simulate_qualifying(d, t, sims=4000, execution_sd=0.05).summary().set_index("driver_id")
    t2 = t.assign(car_quali_se=0.5, form_sd=0.3)
    unsure = (
        simulate_qualifying(d, t2, sims=4000, execution_sd=0.05).summary().set_index("driver_id")
    )
    assert unsure.loc["c", "p_pole"] > sure.loc["c", "p_pole"]


def test_ewma_weights_recent_races_more():
    mean, n_eff = ewma(np.array([2.0, 2.0, 2.0, 0.0]), half_life=1.0)
    assert mean < 1.0  # the latest race dominates with a 1-race half-life
    assert 1 < n_eff < 4


def test_car_gap_removes_driver_skill():
    events = pd.DataFrame(
        {
            "constructor_id": ["x"] * 3,
            "season": [2026] * 3,
            "round": [1, 2, 3],
            "quali_gap_pct": [0.5, 0.5, 0.5],
            "race_gap_pct": [0.4, 0.4, 0.4],
        }
    )
    lineup = pd.DataFrame(
        {"constructor_id": ["x"], "mean_quali_skill": [-0.3], "mean_race_skill": [-0.2]}
    )
    cars = car_gaps(events, lineup).iloc[0]
    assert np.isclose(cars.car_quali, 0.8) and np.isclose(cars.car_race, 0.6)


def test_trait_values_fall_back_to_prior_and_widen_when_stale():
    traits = pd.DataFrame(
        {
            "trait": ["quali_pace", "quali_pace"],
            "driver_id": ["vet", "vet"],
            "as_of_season": [2024, 2025],
            "value": [-0.3, -0.4],
            "std_err": [0.1, 0.1],
        }
    )
    out = latest_trait_values(traits, ["vet", "rookie"], 2026).set_index("driver_id")
    assert out.loc["vet", "quali_pace"] == -0.4
    assert out.loc["vet", "quali_pace_se"] > 0.1  # one season stale: wider
    assert out.loc["rookie", "quali_pace"] == 0.0 and out.loc["rookie", "trait_source"] == "prior"


def test_impossible_compound_order_uses_global_values():
    tyres = pd.DataFrame(
        {"compound": ["SOFT", "MEDIUM", "HARD"], "fresh_pace_vs_medium_pct": [-0.3, 0.0, -0.2]}
    )
    glob = pd.DataFrame(
        {"compound": ["SOFT", "MEDIUM", "HARD"], "global_offset": [-0.19, 0.0, 0.14]}
    )
    out = sane_compound_pace(tyres, glob).set_index("compound")
    assert out.loc["HARD", "fresh_pace_vs_medium_pct"] == 0.14
    assert out.loc["SOFT", "pace_source"] == "global"
    ok = sane_compound_pace(tyres.assign(fresh_pace_vs_medium_pct=[-0.3, 0.0, 0.2]), glob)
    assert (ok["pace_source"] == "circuit").all()
