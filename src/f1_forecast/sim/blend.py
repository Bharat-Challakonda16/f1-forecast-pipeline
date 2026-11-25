"""Blend simulated race probabilities with what the starting position usually means.

The simulator knows the cars, drivers and tracks, but it tends to make races more open than
they are: the pole sitter wins ~55% of races since 2014, more than the simulation gives.
Historical frequencies by grid slot ("P1 wins x%, P2 y%, ...") capture that, so the final
probability is a weighted average of the two:

    p = (1 - w) * simulated + w * grid prior

Before the weekend the grid is unknown, so the prior is averaged over the simulated grids:
P(win) = mean over simulations of P(win | the grid slot drawn in that simulation).
The weights are learned on one season (``backtest/tune.py --blend``) and tested on the next.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

COLS = ("p_win", "p_podium", "p_points")
# Learned on the 2025 season (backtest/tune.py --blend, lowest winner log loss), tested on
# 2026. From the actual grid the slot history carries most of the information (log loss
# 1.398 simulation only, 1.116 grid only, 1.104 at 0.85); before the weekend, a little helps.
GRID_BLEND = {"post_quali": 0.85, "pre_weekend": 0.15}


def grid_prior(grid: np.ndarray, table: pd.DataFrame) -> pd.DataFrame:
    """``grid``: (drivers,) or (sims, drivers) grid slots (1 = pole). ``table``: rank,
    p_win, p_podium, p_points (frequencies by grid slot from earlier seasons).
    Returns one row per driver (same order) with the prior probabilities."""
    g = np.atleast_2d(np.asarray(grid, int))
    t = table.set_index("rank").sort_index()
    g = np.clip(g, int(t.index.min()), int(t.index.max()))
    out = {}
    for c in COLS:
        lookup = t[c].reindex(range(int(t.index.max()) + 1)).ffill().bfill().to_numpy()
        out[c] = lookup[g].mean(axis=0)
    df = pd.DataFrame(out)
    df["p_win"] = df["p_win"] / df["p_win"].sum()
    return df


def blend(sim: pd.DataFrame, prior: pd.DataFrame, w: float) -> pd.DataFrame:
    """Weighted average of ``sim`` and ``prior`` for p_win / p_podium / p_points (rows in the
    same driver order). Other columns are kept from ``sim``."""
    out = sim.copy()
    for c in COLS:
        out[c] = (1 - w) * sim[c].to_numpy(float) + w * prior[c].to_numpy(float)
    out["p_win"] = out["p_win"] / out["p_win"].sum()
    return out
