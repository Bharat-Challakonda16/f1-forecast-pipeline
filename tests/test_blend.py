from __future__ import annotations

import numpy as np
import pandas as pd

from f1_forecast.sim.blend import blend, blend_many, grid_prior

TABLE = pd.DataFrame(
    {
        "rank": [1, 2, 3, 4],
        "p_win": [0.6, 0.25, 0.1, 0.05],
        "p_podium": [0.9, 0.7, 0.5, 0.2],
        "p_points": [1.0, 0.95, 0.9, 0.8],
    }
)


def test_fixed_grid_prior_is_the_table():
    p = grid_prior(np.array([2, 1, 4, 3]), TABLE)
    assert np.allclose(p["p_win"], [0.25, 0.6, 0.05, 0.1])
    assert np.isclose(p["p_win"].sum(), 1)


def test_simulated_grid_prior_averages_and_clips_slots():
    grids = np.array([[1, 2, 9], [2, 1, 9]])  # slot 9 uses the last row
    p = grid_prior(grids, TABLE)
    assert np.isclose(p["p_win"].iloc[0], p["p_win"].iloc[1])
    assert p["p_podium"].iloc[2] == 0.2


def test_blend_weights_and_keeps_other_columns():
    sim = pd.DataFrame(
        {
            "p_win": [0.2, 0.8],
            "p_podium": [0.5, 0.9],
            "p_points": [0.9, 1.0],
            "driver_id": ["a", "b"],
        }
    )
    prior = pd.DataFrame({"p_win": [0.6, 0.4], "p_podium": [0.9, 0.7], "p_points": [1.0, 1.0]})
    out = blend(sim, prior, 0.5)
    assert np.allclose(out["p_win"], [0.4, 0.6])
    assert list(out["driver_id"]) == ["a", "b"]
    assert blend(sim, prior, 0.0)["p_win"].equals(sim["p_win"])


def test_blend_many_weights_and_missing_prior():
    sim = pd.DataFrame({"p_pole": [0.5, 0.5], "p_top3_grid": [0.8, 0.8]})
    pq = pd.DataFrame({"p_pole": [1.0, 0.0], "p_top3_grid": [1.0, 0.6]})
    cols = ("p_pole", "p_top3_grid")
    out = blend_many(
        sim, {"previous_quali": pq}, {"previous_quali": 0.5, "championship": 0.3}, cols
    )
    # championship weight is unused (no prior) and stays with the simulation
    assert np.allclose(out["p_pole"], [0.75, 0.25])
    assert np.allclose(out["p_top3_grid"], [0.9, 0.7])
