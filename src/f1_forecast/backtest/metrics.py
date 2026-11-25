"""Scoring rules for race / qualifying predictions (pure functions, unit-tested).

For one event, ``probs`` are per-driver probabilities of an outcome (e.g. winning) and
``actual`` the 0/1 outcome per driver.

- **Brier score**: sum over drivers of (p - y)^2. 0 is perfect; lower is better.
- **Log loss**: -ln(p of what actually happened), for the winner (or pole). Lower is better;
  punishes confident misses hard.
- **Hit rates**: did the favourite win? How many of the predicted top 3 made the podium?
- **Spearman**: rank correlation between predicted order and actual order.
- **Calibration**: when we say 30%, does it happen ~30% of the time?
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

EPS = 1e-3


def brier(probs: np.ndarray, actual: np.ndarray) -> float:
    return float(np.sum((np.asarray(probs, float) - np.asarray(actual, float)) ** 2))


def log_loss_of_outcome(probs: np.ndarray, actual: np.ndarray) -> float:
    p = np.asarray(probs, float)[np.asarray(actual, bool)]
    return float(-np.log(np.clip(p, EPS, 1.0)).sum())


def top_k_hits(scores: np.ndarray, actual_rank: np.ndarray, k: int) -> float:
    """Share of the k highest-scoring drivers that finished in the actual top k."""
    predicted = set(np.argsort(-np.asarray(scores, float))[:k])
    real = set(np.flatnonzero(np.asarray(actual_rank) <= k))
    return len(predicted & real) / k


def spearman(predicted_order: np.ndarray, actual_rank: np.ndarray) -> float:
    rho = spearmanr(predicted_order, actual_rank).statistic
    return float(rho) if np.isfinite(rho) else 0.0


def event_scores(pred: pd.DataFrame, *, kind: str = "race") -> dict[str, float]:
    """``pred`` columns: actual_rank, plus p_win/p_podium/p_points/expected_position (race)
    or p_pole/p_top3_grid/expected_grid (qualifying)."""
    r = pred["actual_rank"].to_numpy()
    if kind == "race":
        won = r == 1
        return {
            "winner_hit": float(np.argmax(pred["p_win"].to_numpy()) == np.argmax(won)),
            "podium_hits": top_k_hits(pred["p_podium"].to_numpy(), r, 3),
            "points_hits": top_k_hits(pred["p_points"].to_numpy(), r, 10),
            "brier_win": brier(pred["p_win"], won),
            "brier_podium": brier(pred["p_podium"], r <= 3) / 3,
            "log_loss_win": log_loss_of_outcome(pred["p_win"], won),
            "spearman": spearman(pred["expected_position"].to_numpy(), r),
        }
    pole = r == 1
    return {
        "pole_hit": float(np.argmax(pred["p_pole"].to_numpy()) == np.argmax(pole)),
        "top3_hits": top_k_hits(pred["p_top3_grid"].to_numpy(), r, 3),
        "brier_pole": brier(pred["p_pole"], pole),
        "log_loss_pole": log_loss_of_outcome(pred["p_pole"], pole),
        "spearman": spearman(pred["expected_grid"].to_numpy(), r),
    }


def calibration_table(probs: np.ndarray, actual: np.ndarray, bins: int = 10) -> pd.DataFrame:
    """Pooled reliability table: mean predicted vs observed frequency per probability bin."""
    p = np.asarray(probs, float)
    y = np.asarray(actual, float)
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges) - 1, 0, bins - 1)
    rows = []
    for b in range(bins):
        m = idx == b
        if m.any():
            rows.append(
                (edges[b], edges[b + 1], int(m.sum()), float(p[m].mean()), float(y[m].mean()))
            )
    return pd.DataFrame(rows, columns=["bin_low", "bin_high", "n", "mean_predicted", "observed"])


def rank_baseline_probs(ranks: np.ndarray, table: pd.DataFrame) -> pd.DataFrame:
    """Turn a baseline ranking into probabilities with historical frequencies:
    ``table`` has rank, p_win, p_podium, p_points (estimated on earlier seasons), so e.g.
    'the championship leader wins 38% of the time'. Ranks beyond the table use its last row."""
    t = table.set_index("rank").sort_index()
    r = np.minimum(np.asarray(ranks, int), int(t.index.max()))
    out = t.loc[r, ["p_win", "p_podium", "p_points"]].reset_index(drop=True)
    for col in ("p_win",):  # one winner per race: renormalise
        out[col] = out[col] / out[col].sum()
    out["expected_position"] = np.asarray(ranks, float)
    return out
