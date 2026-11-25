from __future__ import annotations

import numpy as np
import pandas as pd

from f1_forecast.sim.practice import PracticeCoefs, apply_practice, practice_surprise


def frames():
    drivers = pd.DataFrame(
        {
            "driver_id": ["a1", "a2", "b1", "b2", "c1", "c2"],
            "constructor_id": ["a", "a", "b", "b", "c", "c"],
            "quali_pace": [0.0] * 6,
            "race_pace": [0.0] * 6,
        }
    )
    teams = pd.DataFrame(
        {
            "constructor_id": ["a", "b", "c"],
            "car_quali": [-0.5, 0.0, 0.5],
            "car_race": [-0.5, 0.0, 0.5],
            "track_adj_quali": [0.0] * 3,
            "track_adj_race": [0.0] * 3,
            "form_sd": [0.28] * 3,
            "form_sd_race": [0.22] * 3,
        }
    )
    return drivers, teams


def practice(best, long=None):
    n = len(best)
    return pd.DataFrame(
        {
            "driver_id": ["a1", "a2", "b1", "b2", "c1", "c2"][:n],
            "session_code": "FP2",
            "best_pct": best,
            "long_pct": long if long is not None else [np.nan] * n,
            "long_laps": [10] * n if long is not None else [0] * n,
        }
    )


def test_as_expected_means_no_surprise():
    d, t = frames()
    s = practice_surprise(d, t, practice([-0.5, -0.5, 0.0, 0.0, 0.5, 0.5]))
    assert np.allclose(s[["team_q", "driver_q", "team_long"]], 0)


def test_team_and_driver_parts():
    d, t = frames()
    # team c is 0.6 faster than expected; c1 0.2 faster than c2
    s = practice_surprise(d, t, practice([-0.5, -0.5, 0.0, 0.0, 0.0, 0.2]))
    s = s.set_index("driver_id")
    assert s.loc["c1", "team_q"] < s.loc["a1", "team_q"]
    assert np.isclose(s.loc["c1", "driver_q"] - s.loc["c2", "driver_q"], -0.2)
    assert np.isclose(s["team_q"].groupby(d["constructor_id"].to_numpy()).first().sum(), 0)


def test_missing_practice_is_zero_and_apply_shifts():
    d, t = frames()
    s = practice_surprise(d, t, practice([]))
    assert (s[["team_q", "driver_q", "team_long"]] == 0).all().all()
    s = practice_surprise(d, t, practice([-0.5, -0.5, 0.0, 0.0, 0.0, 0.0]))
    c = PracticeCoefs(0.5, 0.0, 0.1, 0.0, 0.0, quali_form_scale=0.8)
    d2, t2 = apply_practice(d, t, s, c)
    tc = t2.set_index("constructor_id")
    shift = s.set_index("driver_id").loc["c1", "team_q"]
    assert np.isclose(tc.loc["c", "car_quali"], 0.5 + 0.5 * shift)
    assert np.isclose(tc.loc["c", "car_race"], 0.5 + 0.1 * shift)
    assert np.isclose(tc.loc["c", "form_sd"], 0.28 * 0.8)
    assert d2["quali_pace"].eq(0).all()
