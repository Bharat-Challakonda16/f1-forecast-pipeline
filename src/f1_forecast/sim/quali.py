"""Monte Carlo qualifying: pole and grid-position probabilities.

For each simulated session ``s`` and driver ``i`` (team ``k``)::

    lap_pct[s, i] = car_quali[k] ~ N(estimate, se)          # what we know about the car
                  + form[s, k]   ~ N(0, team form sd)        # how it works on this weekend
                  + track_type_adj[k]                        # strength at this kind of track
                  + skill[i]     ~ N(trait value, trait se)  # the driver
                  + execution[s, i] ~ N(0, EXECUTION_SD)     # nailing the lap (or not)

The grid is the order of ``lap_pct``. Every draw also samples the *uncertainty of the
estimates*, so a team or driver we know little about gets a wider spread of outcomes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

EXECUTION_SD = 0.15  # % lap: driver-level variation in a single qualifying lap
TRACK_TYPE_WEIGHT = 0.5  # track-type strength is noisy: use half of the measured offset


@dataclass
class QualiResult:
    driver_ids: list[str]
    grid: np.ndarray  # (sims, drivers): grid position (1 = pole) of each driver per sim
    pace: np.ndarray  # (sims, drivers): simulated lap deficit, %
    # (sims, teams): how much faster/slower than expected each team qualified (weekend form +
    # lap execution, not the estimate error), which carries into the race (QUALI_TO_RACE)
    team_surprise: np.ndarray | None = None

    def summary(self) -> pd.DataFrame:
        g = self.grid
        n = g.shape[1]
        out = pd.DataFrame(
            {
                "driver_id": self.driver_ids,
                "p_pole": (g == 1).mean(axis=0),
                "p_front_row": (g <= 2).mean(axis=0),
                "p_top3": (g <= 3).mean(axis=0),
                "p_top10": (g <= 10).mean(axis=0),
                "expected_grid": g.mean(axis=0),
            }
        )
        dist = np.stack([(g == p).mean(axis=0) for p in range(1, n + 1)], axis=1)
        out["grid_distribution"] = list(dist)
        return out.sort_values("expected_grid").reset_index(drop=True)


def track_type_adjustment(
    teams: pd.DataFrame, speed_profile: str | None, kind: str = "quali"
) -> pd.Series:
    """Car strength at this kind of track (% lap, lower = faster). Prefers the field model's
    car x track-type effect (``track_adj_<kind>``); else half the season-to-date index."""
    if f"track_adj_{kind}" in teams:
        return pd.to_numeric(teams[f"track_adj_{kind}"]).fillna(0.0)
    col = {
        "high": "high_speed_track_idx",
        "low": "low_speed_track_idx",
        "medium": "medium_speed_track_idx",
        "mixed": "medium_speed_track_idx",
    }.get(speed_profile or "")
    if col is None or col not in teams:
        return pd.Series(0.0, index=teams.index)
    return TRACK_TYPE_WEIGHT * pd.to_numeric(teams[col]).fillna(0.0)


def simulate_qualifying(
    drivers: pd.DataFrame,
    teams: pd.DataFrame,
    *,
    speed_profile: str | None = None,
    sims: int = 10_000,
    seed: int = 2027,
    execution_sd: float = EXECUTION_SD,
    car_z: np.ndarray | None = None,
    skill_z: np.ndarray | None = None,
) -> QualiResult:
    """``drivers``: driver_id, constructor_id, quali_pace, quali_pace_se.
    ``teams``: constructor_id, car_quali, car_quali_se, form_sd (+ optional track idx).

    ``car_z`` (sims, teams) and ``skill_z`` (sims, drivers) are optional standard-normal draws
    for the *estimate* uncertainty, so a season simulation can keep "the car is faster than
    we think" consistent across all its events (weekend form stays independent)."""
    rng = np.random.default_rng(seed)
    t = teams.reset_index(drop=True)
    team_idx = {c: i for i, c in enumerate(t["constructor_id"])}
    k = drivers["constructor_id"].map(team_idx).to_numpy()
    if np.isnan(k.astype(float)).any():
        raise ValueError("every driver's constructor_id must be in teams")
    k = k.astype(int)

    adj = track_type_adjustment(t, speed_profile).to_numpy()
    if car_z is None:
        car_z = rng.standard_normal((sims, len(t)))
    if skill_z is None:
        skill_z = rng.standard_normal((sims, len(drivers)))
    car = t["car_quali"].to_numpy(float) + t["car_quali_se"].to_numpy(float) * car_z
    form = rng.normal(0.0, t["form_sd"].to_numpy(float), (sims, len(t)))
    skill = (
        drivers["quali_pace"].to_numpy(float) + drivers["quali_pace_se"].to_numpy(float) * skill_z
    )
    execution = rng.normal(0.0, execution_sd, (sims, len(drivers)))
    pace = (car + form + adj)[:, k] + skill + execution
    grid = pace.argsort(axis=1).argsort(axis=1) + 1
    counts = np.bincount(k, minlength=len(t)).clip(min=1)
    team_exec = (
        np.stack([np.bincount(k, weights=row, minlength=len(t)) for row in execution]) / counts
    )
    surprise = form + team_exec
    surprise -= surprise.mean(axis=1, keepdims=True)  # only relative pace matters
    return QualiResult(drivers["driver_id"].tolist(), grid, pace, surprise)


def observed_team_surprise(
    drivers: pd.DataFrame,
    teams: pd.DataFrame,
    quali_pace_pct: pd.Series,
    speed_profile: str | None = None,
) -> np.ndarray:
    """(teams,) actual qualifying surprise per team: its drivers' measured pace this weekend
    (``quali_pace_pct`` by driver_id, % vs session median) minus what the estimates expected,
    both centred on the field. Teams without a time get 0."""
    t = teams.reset_index(drop=True)
    d = drivers.assign(obs=drivers["driver_id"].map(quali_pace_pct))
    adj = track_type_adjustment(t, speed_profile)
    d["exp"] = (
        d["constructor_id"].map(pd.Series((t["car_quali"] + adj).to_numpy(), t["constructor_id"]))
        + d["quali_pace"]
    )
    d = d.dropna(subset=["obs"])
    d = d[(d["obs"] - d["obs"].median()).abs() <= 5]  # crashes / no lap: no information
    if d.empty:
        return np.zeros(len(t))
    per = d.groupby("constructor_id")[["obs", "exp"]].mean()
    s = (per["obs"] - per["obs"].mean()) - (per["exp"] - per["exp"].mean())
    return s.reindex(t["constructor_id"]).fillna(0.0).to_numpy()
