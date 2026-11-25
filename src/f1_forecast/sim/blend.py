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
# 1.364 simulation only, 1.116 grid only, 1.105 at 0.85); before the weekend it doesn't
# help once young drivers' development is modelled (1.727 at 0, worse above).
GRID_BLEND = {"post_quali": 0.85, "pre_weekend": 0.0, "post_practice": 0.0}


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


QUALI_COLS = ("p_pole", "p_top3_grid", "p_top10")
# Pole chances per stage: weights of the simple qualifying baselines (frequencies by last
# qualifying position / championship position), learned on 2025 (tune.py --quali).
# Pole log loss on 2025: before the weekend 1.89, after practice 1.84 (1.91 unblended).
QUALI_BLEND = {
    "pre_weekend": {"previous_quali": 0.40, "championship": 0.25},
    "post_practice": {"previous_quali": 0.30, "championship": 0.25},
}


def blend_many(
    sim: pd.DataFrame, priors: dict[str, pd.DataFrame], weights: dict[str, float], cols
) -> pd.DataFrame:
    """``(1 - sum w) * sim + sum w_k * prior_k`` for ``cols`` (first column = a single
    winner, renormalised). Rows in the same driver order; a missing prior's weight goes back
    to ``sim``."""
    out = sim.copy()
    used = {k: weights.get(k, 0.0) for k in priors}
    for c in cols:
        v = (1 - sum(used.values())) * sim[c].to_numpy(float)
        for k, p in priors.items():
            v = v + used[k] * p[c].to_numpy(float)
        out[c] = v
    out[cols[0]] = out[cols[0]] / out[cols[0]].sum()
    return out


def blend(sim: pd.DataFrame, prior: pd.DataFrame, w: float) -> pd.DataFrame:
    """Weighted average of ``sim`` and ``prior`` for p_win / p_podium / p_points (rows in the
    same driver order). Other columns are kept from ``sim``."""
    out = sim.copy()
    for c in COLS:
        out[c] = (1 - w) * sim[c].to_numpy(float) + w * prior[c].to_numpy(float)
    out["p_win"] = out["p_win"] / out["p_win"].sum()
    return out
