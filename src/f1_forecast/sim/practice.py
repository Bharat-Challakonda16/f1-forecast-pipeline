"""After-practice update: shift the model's car and driver pace by what practice showed.

Practice lap times (``gold.driver_practice_pace``) are noisy (unknown fuel loads, engine
modes, programmes) but they are this car, this driver, this track, this weekend. For each
driver the *practice surprise* is

    surprise = practice pace - pace the model expected     (both centred on the field)

split into the team part (mean of the teammates: the car) and the driver part (difference
from the teammate). A learned share of each carries into qualifying and race pace::

    car_quali  += A_QUALI_TEAM  * team surprise      quali_pace += A_QUALI_DRIVER * driver part
    car_race   += A_RACE_TEAM   * team surprise      race_pace  += A_RACE_DRIVER  * driver part
                + A_RACE_LONG   * team long-run surprise

and the left-over weekend form spread shrinks by the share of it practice explained. The
shares are learned on one season (``backtest/tune.py --practice``) and tested on the next.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PracticeCoefs:
    quali_team: float
    quali_driver: float
    race_team: float
    race_driver: float
    race_long: float
    # left-over weekend form spread relative to before practice (sqrt of unexplained share)
    quali_form_scale: float = 1.0
    race_form_scale: float = 1.0


# Learned on the 2025 season (backtest/tune.py --practice; 210 team-weekends), tested on
# 2026. Practice says a lot about qualifying (55% of the car surprise carries over, 29% of
# the team-level spread explained) and a little about the race (long runs count most).
COEFS = PracticeCoefs(
    quali_team=0.55,
    quali_driver=0.18,
    race_team=0.105,
    race_driver=0.20,
    race_long=0.27,
    quali_form_scale=0.84,
    race_form_scale=0.96,
)


def practice_summary(fp: pd.DataFrame) -> pd.DataFrame:
    """``fp``: rows of driver_practice_pace for one weekend (non-outlier). Returns per driver
    ``best`` (mean over sessions of best-lap pace) and ``long`` (laps-weighted long-run
    pace, NaN if none), each centred on the drivers present."""
    if fp.empty:
        return pd.DataFrame(columns=["driver_id", "best", "long"])
    f = fp.copy()
    best = f.groupby("driver_id")["best_pct"].mean()
    lr = f.dropna(subset=["long_pct"])
    lr = lr[lr["long_laps"] > 0]
    if len(lr):
        w = lr.assign(x=lr["long_pct"] * lr["long_laps"]).groupby("driver_id")
        long = w["x"].sum() / w["long_laps"].sum()
    else:
        long = pd.Series(dtype=float)
    out = pd.DataFrame({"best": best}).join(long.rename("long"))
    out["best"] = out["best"] - out["best"].mean()
    if out["long"].notna().any():
        out["long"] = out["long"] - out["long"].mean()
    return out.reset_index()


def expected_pace(drivers: pd.DataFrame, teams: pd.DataFrame, kind: str) -> np.ndarray:
    """The model's expected pace per driver (car + car-track adjustment + driver), centred."""
    t = teams.set_index("constructor_id")
    adj = t.get(f"track_adj_{kind}", 0.0)
    car = (t[f"car_{kind}"] + adj).reindex(drivers["constructor_id"]).to_numpy(float)
    e = car + drivers[f"{kind}_pace"].to_numpy(float)
    return e - np.nanmean(e)


def practice_surprise(drivers: pd.DataFrame, teams: pd.DataFrame, fp: pd.DataFrame):
    """Per driver (same order as ``drivers``): team_q / driver_q (best-lap surprise vs the
    expected qualifying pace, team mean and teammate difference) and team_long (long-run
    surprise vs the expected race pace, team mean). 0 where practice is missing."""
    s = practice_summary(fp).set_index("driver_id").reindex(drivers["driver_id"])
    out = pd.DataFrame({"driver_id": drivers["driver_id"].to_numpy()})
    team = drivers["constructor_id"].to_numpy()
    for col, kind, name in (("best", "quali", "q"), ("long", "race", "long")):
        obs = s[col].to_numpy(float) if col in s else np.full(len(drivers), np.nan)
        has = np.isfinite(obs)
        if has.sum() < 4:
            out[f"team_{name}"] = 0.0
            if name == "q":
                out["driver_q"] = 0.0
            continue
        exp = expected_pace(drivers, teams, kind)
        # centre both on the drivers who have practice data
        surprise = (obs - obs[has].mean()) - (exp - exp[has].mean())
        sr = pd.Series(np.where(has, surprise, np.nan))
        team_mean = sr.groupby(team).transform("mean").fillna(0.0).to_numpy()
        out[f"team_{name}"] = team_mean
        if name == "q":
            out["driver_q"] = np.where(has, sr.fillna(0.0).to_numpy() - team_mean, 0.0)
    return out


def apply_practice(
    drivers: pd.DataFrame,
    teams: pd.DataFrame,
    surprise: pd.DataFrame,
    coefs: PracticeCoefs = COEFS,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Copies of the simulator frames with car / driver pace shifted by practice."""
    d, t = drivers.copy(), teams.copy()
    s = surprise.set_index("driver_id").reindex(d["driver_id"])
    d["quali_pace"] = d["quali_pace"] + coefs.quali_driver * s["driver_q"].to_numpy()
    d["race_pace"] = d["race_pace"] + coefs.race_driver * s["driver_q"].to_numpy()
    per_team = s.assign(constructor_id=d["constructor_id"].to_numpy()).groupby("constructor_id")
    tq = per_team["team_q"].first().reindex(t["constructor_id"]).fillna(0.0).to_numpy()
    tl = per_team["team_long"].first().reindex(t["constructor_id"]).fillna(0.0).to_numpy()
    t["car_quali"] = t["car_quali"] + coefs.quali_team * tq
    t["car_race"] = t["car_race"] + coefs.race_team * tq + coefs.race_long * tl
    if "form_sd" in t:
        t["form_sd"] = t["form_sd"] * coefs.quali_form_scale
    if "form_sd_race" in t:
        t["form_sd_race"] = t["form_sd_race"] * coefs.race_form_scale
    return d, t
