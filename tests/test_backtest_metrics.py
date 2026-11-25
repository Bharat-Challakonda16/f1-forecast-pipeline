from __future__ import annotations

import numpy as np
import pandas as pd

from f1_forecast.backtest.metrics import (
    brier,
    calibration_table,
    event_scores,
    log_loss_of_outcome,
    rank_baseline_probs,
    top_k_hits,
)


def test_brier_and_log_loss():
    assert brier([1, 0, 0], [1, 0, 0]) == 0
    assert np.isclose(brier([0.5, 0.5, 0], [1, 0, 0]), 0.5)
    assert np.isclose(log_loss_of_outcome([0.5, 0.5], [1, 0]), np.log(2))
    assert log_loss_of_outcome([0.0, 1.0], [1, 0]) == -np.log(1e-3)  # clipped, not infinite


def test_top_k_hits():
    scores = np.array([0.9, 0.8, 0.7, 0.1])
    assert top_k_hits(scores, np.array([1, 2, 4, 3]), 3) == 2 / 3


def test_event_scores_race_perfect_prediction():
    pred = pd.DataFrame(
        {
            "actual_rank": [1, 2, 3, 4],
            "p_win": [1.0, 0, 0, 0],
            "p_podium": [1.0, 1.0, 1.0, 0],
            "p_points": [1.0, 1.0, 1.0, 1.0],
            "expected_position": [1, 2, 3, 4],
        }
    )
    s = event_scores(pred)
    assert s["winner_hit"] == 1 and s["podium_hits"] == 1 and s["brier_win"] == 0
    assert np.isclose(s["spearman"], 1.0)


def test_calibration_table_bins():
    p = np.array([0.05, 0.05, 0.95, 0.95])
    y = np.array([0, 0, 1, 1])
    t = calibration_table(p, y)
    assert t["n"].sum() == 4 and set(t["observed"]) == {0.0, 1.0}


def test_rank_baseline_probs_uses_history_and_renormalises():
    table = pd.DataFrame(
        {
            "rank": [1, 2, 3],
            "p_win": [0.5, 0.3, 0.1],
            "p_podium": [0.8, 0.6, 0.4],
            "p_points": [0.95, 0.9, 0.8],
        }
    )
    out = rank_baseline_probs(np.array([3, 1, 2, 7]), table)
    assert np.isclose(out["p_win"].sum(), 1.0)
    assert out.loc[1, "p_win"] > out.loc[2, "p_win"] > out.loc[0, "p_win"]
    assert out.loc[3, "p_podium"] == 0.4  # beyond the table: last row
