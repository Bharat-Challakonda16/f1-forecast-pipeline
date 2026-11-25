from __future__ import annotations

import pandas as pd

from f1_forecast.backtest.race_backtest import baseline_ranks


def test_tied_standings_rank_the_same_in_any_order():
    hist = pd.DataFrame(
        {
            "season": [2025] * 4,
            "round": [1] * 4,
            "session_code": ["R"] * 4,
            "driver_id": ["b", "a", "c", "d"],
            "grid": [1, 2, 3, 4],
            "position": [1, 2, 3, 4],
            "points": [10, 10, 5, 0],  # a and b tied
            "quali_position": [1, 2, 3, 4],
            "quali_best_ms": [1, 2, 3, 4],
        }
    )
    r1 = baseline_ranks(hist, 2025, 2, ["b", "a", "c", "d"])["championship"]
    r2 = baseline_ranks(hist, 2025, 2, ["a", "b", "d", "c"])["championship"]
    assert dict(zip("bacd", r1, strict=True)) == dict(zip("abdc", r2, strict=True))
    assert dict(zip("bacd", r1, strict=True))["a"] == 1  # tie broken by driver id
