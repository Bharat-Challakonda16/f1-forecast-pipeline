"""Turn a joint field-model fit (``gold.field_effects``) into simulator inputs for one event.

Per kind (``quali`` / ``race``):

    car pace      = car[team, latest round fitted]        (+ one round of upgrade uncertainty)
    track adj     = car_type[team, season, this track type]
    driver pace   = skill[driver, latest season] + driver_type[driver, type]
                  + driver_track[driver, this circuit]

Everything is % of lap time, lower = faster, on the same scale (relative to the session
median), so ``car + track adj + driver pace`` is a driver's expected pace this weekend.
A new season (no rounds yet) starts from last season's final car with wide uncertainty.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

NEW_SEASON_CAR_SD = 0.6  # % lap: how far a car can move over a winter (rule changes: more)
ROOKIE_SKILL_SD = 0.3
# Remaining weekend-to-weekend team form once car-per-round and car x track type are
# modelled: teammates' shared deviation (dry 2014-2026) is 0.34% (quali) / 0.28% (race)
# including upgrades and track type; the leftover is smaller.
FORM_SD = {"quali": 0.28, "race": 0.22}


def _hyper(e: pd.DataFrame, name: str, default: float) -> float:
    v = e.loc[e["effect"] == f"hyper:{name}", "value"]
    return float(v.iloc[0]) if len(v) else default


def team_inputs(e: pd.DataFrame, teams: list[str], season: int, speed_profile: str | None):
    """(car, car_se, track_adj, source) per team for one kind's effects ``e``."""
    car = e[e["effect"] == "car"]
    ctype = e[e["effect"] == "car_type"]
    upgrade = _hyper(e, "sigma_upgrade", 0.15)
    rows = []
    for t in teams:
        c = car[(car["constructor_id"] == t) & (car["season"] <= season)]
        if c.empty:
            rows.append((t, np.nan, np.nan, 0.0, "missing"))
            continue
        last = c.sort_values(["season", "round"]).iloc[-1]
        same = int(last["season"]) == season
        se = np.hypot(last["std_err"], upgrade if same else NEW_SEASON_CAR_SD)
        ct = ctype[
            (ctype["constructor_id"] == t)
            & (ctype["season"] == last["season"])
            & (ctype["speed_profile"] == speed_profile)
        ]
        adj = float(ct["value"].iloc[0]) if same and len(ct) else 0.0
        rows.append((t, float(last["value"]), float(se), adj, "fitted" if same else "last_season"))
    out = pd.DataFrame(rows, columns=["constructor_id", "car", "car_se", "track_adj", "source"])
    missing = out["car"].isna()
    if missing.any():  # new team: field-average car, very uncertain
        out.loc[missing, "car"] = out.loc[~missing, "car"].mean() if (~missing).any() else 0.0
        out.loc[missing, "car_se"] = 1.0
    return out


def driver_inputs(
    e: pd.DataFrame, drivers: list[str], season: int, circuit_id: str, speed_profile: str | None
):
    """(pace, pace_se, skill, type bonus, track bonus, track visits) per driver."""
    skill = e[e["effect"] == "skill"]
    dtype = e[e["effect"] == "driver_type"].set_index(["driver_id", "speed_profile"])
    dtrack = e[e["effect"] == "driver_track"].set_index(["driver_id", "circuit_id"])
    walk = _hyper(e, "sigma_walk", 0.1)
    rows = []
    for d in drivers:
        s = skill[(skill["driver_id"] == d) & (skill["season"] <= season)]
        if s.empty:
            sk, sk_se = 0.0, ROOKIE_SKILL_SD
        else:
            last = s.loc[s["season"].idxmax()]
            gap = season - int(last["season"])
            sk, sk_se = float(last["value"]), float(np.hypot(last["std_err"], walk * np.sqrt(gap)))
        ty, ty_se = _pick(dtype, (d, speed_profile))
        tr, tr_se = _pick(dtrack, (d, circuit_id))
        visits = dtrack["n_samples"].get((d, circuit_id), 0.0) if "n_samples" in dtrack else 0.0
        pace = sk + ty + tr
        se = float(np.sqrt(sk_se**2 + ty_se**2 + tr_se**2))
        rows.append((d, pace, se, sk, ty, tr, float(visits or 0.0)))
    return pd.DataFrame(
        rows,
        columns=[
            "driver_id",
            "pace",
            "pace_se",
            "skill",
            "track_type_bonus",
            "track_bonus",
            "track_visits",
        ],
    )


def _pick(frame: pd.DataFrame, key) -> tuple[float, float]:
    if key in frame.index:
        r = frame.loc[key]
        if isinstance(r, pd.DataFrame):
            r = r.iloc[0]
        return float(r["value"]), float(r["std_err"])
    return 0.0, 0.0


def apply_field_effects(
    effects: pd.DataFrame,
    drivers: pd.DataFrame,
    teams: pd.DataFrame,
    *,
    season: int,
    circuit_id: str,
    speed_profile: str | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Overwrite car / driver pace columns of the simulator frames with the field model:
    teams: car_quali(+_se), car_race(+_se), track_adj_quali, track_adj_race, form_sd,
    form_sd_race; drivers: quali_pace(+_se), race_pace(+_se) and the track bonus parts."""
    teams, drivers = teams.copy(), drivers.copy()
    for kind in ("quali", "race"):
        e = effects[effects["kind"] == kind]
        if e.empty:
            continue
        ti = team_inputs(e, teams["constructor_id"].tolist(), season, speed_profile)
        ti = ti.set_index("constructor_id").reindex(teams["constructor_id"])
        teams[f"car_{kind}"] = ti["car"].to_numpy()
        teams[f"car_{kind}_se"] = ti["car_se"].to_numpy()
        teams[f"track_adj_{kind}"] = ti["track_adj"].to_numpy()
        di = driver_inputs(e, drivers["driver_id"].tolist(), season, circuit_id, speed_profile)
        di = di.set_index("driver_id").reindex(drivers["driver_id"])
        drivers[f"{kind}_pace"] = di["pace"].to_numpy()
        drivers[f"{kind}_pace_se"] = di["pace_se"].to_numpy()
        for col in ("track_type_bonus", "track_bonus", "track_visits"):
            drivers[f"{kind}_{col}"] = di[col].to_numpy()
    teams["form_sd"] = FORM_SD["quali"]
    teams["form_sd_race"] = FORM_SD["race"]
    return drivers, teams
