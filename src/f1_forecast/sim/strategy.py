"""Pit-stop strategy candidates and their expected cost.

Tyre model (per compound c, fresh offset o_c and wear w_c in % lap time per lap of age)::

    lap penalty at tyre age a = o_c + w_c * a

A stint of n laps therefore costs ``o_c * n + w_c * n * (n - 1) / 2`` (% of a lap). For each
compound sequence we choose the stint lengths that minimise total time (subject to each
compound's practical maximum stint), add the pit losses, and turn the totals into choice
probabilities with a softmax: near-optimal plans are common, clearly worse ones rare. Dry
races must use at least two different compounds.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
import pandas as pd

COMPOUNDS = ("SOFT", "MEDIUM", "HARD")
STRATEGY_TEMPERATURE_S = 4.0  # seconds: plans within a few seconds of the best stay plausible


@dataclass(frozen=True)
class Plan:
    compounds: tuple[int, ...]  # indices into COMPOUNDS, one per stint
    pit_laps: tuple[int, ...]  # lap after which each stop happens
    cost_s: float  # expected race time vs. an ideal no-wear race, seconds

    @property
    def stops(self) -> int:
        return len(self.pit_laps)

    def label(self) -> str:
        return "-".join(COMPOUNDS[c][0] for c in self.compounds)


def stint_cost(offset: float, wear: float, n: int) -> float:
    """% of a lap summed over an n-lap stint on fresh tyres."""
    return offset * n + wear * n * (n - 1) / 2


def best_split(
    seq: tuple[int, ...], laps: int, offset: np.ndarray, wear: np.ndarray, max_stint: np.ndarray
) -> tuple[tuple[int, ...], float] | None:
    """Stint lengths minimising total cost for a compound sequence (exhaustive, small)."""
    best = None
    k = len(seq)
    ranges = [range(3, laps - 3 * (k - 1) + 1)] * (k - 1)
    for cut in itertools.product(*ranges):
        lengths = [*cut, laps - sum(cut)]
        if min(lengths) < 3:
            continue
        if any(n > max_stint[c] for n, c in zip(lengths, seq, strict=True)):
            continue
        cost = sum(stint_cost(offset[c], wear[c], n) for n, c in zip(lengths, seq, strict=True))
        if best is None or cost < best[1]:
            best = (tuple(np.cumsum(lengths)[:-1].tolist()), cost)
    return best


def candidate_plans(
    tyres: pd.DataFrame,
    *,
    laps: int,
    base_lap_ms: float,
    pit_loss_s: float,
    track_temp_c: float = 35.0,
    max_stops: int = 2,
) -> list[Plan]:
    t = tyres.set_index("compound").reindex(list(COMPOUNDS))
    offset = t["fresh_pace_vs_medium_pct"].fillna(0.0).to_numpy(float)
    wear = (
        t["wear_pct_per_lap_35c"].to_numpy(float)
        + t["wear_pct_per_lap_per_10c"].fillna(0.0).to_numpy(float) * (track_temp_c - 35.0) / 10
    ).clip(min=0.002)
    # teams run up to the observed longest stints, with some margin over the p90
    max_stint = (t["max_stint_laps"].fillna(laps).to_numpy(float) * 1.15).clip(min=10)
    plans = []
    for stops in range(1, max_stops + 1):
        for seq in itertools.product(range(3), repeat=stops + 1):
            if len(set(seq)) < 2:  # dry rule: at least two compounds
                continue
            split = best_split(seq, laps, offset, wear, max_stint)
            if split is None:
                continue
            pit_laps, cost_pct = split
            cost_s = cost_pct / 100 * base_lap_ms / 1000 + stops * pit_loss_s
            plans.append(Plan(seq, pit_laps, cost_s))
    if not plans:
        raise ValueError("no feasible strategy for these tyre limits")
    return sorted(plans, key=lambda p: p.cost_s)


def plan_probabilities(
    plans: list[Plan], temperature_s: float = STRATEGY_TEMPERATURE_S
) -> np.ndarray:
    cost = np.array([p.cost_s for p in plans])
    z = np.exp(-(cost - cost.min()) / temperature_s)
    return z / z.sum()
