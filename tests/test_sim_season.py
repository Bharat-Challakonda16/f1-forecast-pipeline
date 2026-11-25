from __future__ import annotations

import numpy as np

from f1_forecast.sim.race import POINTS
from f1_forecast.sim.season import SPRINT_POINTS, championship, points_for
from f1_forecast.sim.strategy import candidate_plans
from tests.test_sim_race import TYRES  # shared tyre fixture


def test_points_tables():
    pos = np.array([[1, 2, 3, 11]])
    dnf = np.array([[False, False, True, False]])
    assert points_for(pos, dnf, POINTS).tolist() == [[25, 18, 0, 0]]
    assert points_for(np.array([[1, 8, 9]]), np.zeros((1, 3), bool), SPRINT_POINTS).tolist() == [
        [8, 1, 0]
    ]


def test_decided_title_is_certain_and_open_title_is_shared():
    rng = np.random.default_rng(0)
    future = rng.uniform(0, 50, (5000, 3))
    decided = championship(["a", "b", "c"], np.array([200.0, 100.0, 90.0]), future, rng)
    assert decided.set_index("id").loc["a", "p_title"] == 1.0
    open_ = championship(["a", "b", "c"], np.array([100.0, 100.0, 100.0]), future, rng)
    p = open_.set_index("id")["p_title"]
    assert np.isclose(p.sum(), 1.0) and (p > 0.25).all()
    dist = np.stack(open_["rank_distribution"])
    assert np.allclose(dist.sum(axis=0), 1) and np.allclose(dist.sum(axis=1), 1)


def test_sprint_strategies_allow_no_stop():
    plans = candidate_plans(TYRES, laps=19, base_lap_ms=90_000, pit_loss_s=22, sprint=True)
    assert plans[0].stops == 0  # no stop is fastest over a sprint
    gp = candidate_plans(TYRES, laps=50, base_lap_ms=90_000, pit_loss_s=22)
    assert min(p.stops for p in gp) >= 1
