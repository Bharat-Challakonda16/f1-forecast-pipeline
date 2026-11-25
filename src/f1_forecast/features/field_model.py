"""Joint field model: separates the car (and its upgrades) from the driver, and finds what a
driver does at a particular track beyond what their car was capable of at that moment.

One observation = one driver in one dry qualifying session (best lap) or race (median clean
lap): ``pace = 100 * ln(time / session median)`` (% of lap time, lower = faster)::

    pace[i] ~ StudentT(nu, mu[i], sigma_obs)
    mu[i]   = session[j]                       # how the field ran that day (absorbs the median)
            + car[team-season, round]           # car speed AT THAT ROUND: a random walk through
                                                #   the season, so upgrades show up as steps
            + car_type[team-season, track type] # this car's strength on this kind of track
            + skill[driver, season]             # the driver: career random walk (as before)
            + driver_type[driver, track type]   # driver strength on this kind of track
            + driver_track[driver, circuit]     # driver strength at this very circuit

Teammates share ``car`` and ``car_type`` in every session, so the car is measured by both
drivers and a driver's numbers are always net of the car he had *that weekend*. Drivers who
change teams tie the car scales together across the grid.

Every effect has a learned spread with a prior centred on zero (hierarchical shrinkage): a
track bonus only grows when a driver beats what his car and his general level predict at
that circuit again and again, over seasons and against different teammates. With few visits
it stays near zero and uncertain. Track and track-type bonuses have heavy-tailed priors
(Student-t, 3 d.f.): most are squeezed to ~0, but a genuine specialist isn't flattened by the
many drivers with nothing special at a circuit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from f1_forecast.features.teammate_model import career_keys, career_random_walk

TRACK_TYPES = ("high", "medium", "low", "mixed")


@dataclass
class FieldData:
    obs: pd.DataFrame  # filtered observations with integer index columns
    sessions: pd.Index
    car_keys: pd.DataFrame  # one row per (team_season, round), random-walk bookkeeping
    car_types: pd.Index  # (team_season, type)
    skill_keys: pd.DataFrame  # one row per (driver, season)
    driver_types: pd.Index  # (driver, type)
    driver_tracks: pd.Index  # (driver, circuit)
    meta: dict[str, Any] = field(default_factory=dict)


def _index(values: pd.Series | pd.MultiIndex) -> tuple[pd.Index, np.ndarray]:
    uniq = pd.Index(pd.unique(values))
    return uniq, uniq.get_indexer(values)


def _typed_index(d: pd.DataFrame, col: str, typed: pd.Series) -> pd.MultiIndex:
    """(col, speed_profile) pairs seen on circuits with a known type; others map to -1."""
    t = d.loc[typed, [col, "speed_profile"]].drop_duplicates()
    return pd.MultiIndex.from_frame(t)


def prepare(df: pd.DataFrame) -> FieldData:
    """``df`` columns: season, round, circuit_id, speed_profile, driver_id, constructor_id,
    pace_pct (already filtered to dry, non-outlier sessions before the cutoff)."""
    d = df.dropna(subset=["pace_pct", "driver_id", "constructor_id"]).copy()
    d = d.astype({"season": int, "round": int, "pace_pct": float})
    d["speed_profile"] = d["speed_profile"].where(d["speed_profile"].isin(TRACK_TYPES), "unknown")
    d["team_season"] = d["constructor_id"] + ":" + d["season"].astype(str)
    d = d.sort_values(["season", "round"]).reset_index(drop=True)

    sessions, d["j"] = _index(d["season"] * 100 + d["round"])

    # car: random walk over the rounds of each team-season (career_keys walks over "season")
    car = career_keys(
        d[["team_season", "round"]]
        .drop_duplicates()
        .rename(columns={"team_season": "driver_id", "round": "season"})
    )
    pos = {(t, r): i for i, (t, r) in enumerate(zip(car["driver_id"], car["season"], strict=True))}
    d["car_i"] = [pos[(t, r)] for t, r in zip(d["team_season"], d["round"], strict=True)]

    typed = d["speed_profile"] != "unknown"
    car_types = _typed_index(d, "team_season", typed)
    d["ct_i"] = car_types.get_indexer(
        pd.MultiIndex.from_arrays([d["team_season"], d["speed_profile"]])
    )

    skill = career_keys(d[["driver_id", "season"]].drop_duplicates())
    spos = {
        (a, s): i for i, (a, s) in enumerate(zip(skill["driver_id"], skill["season"], strict=True))
    }
    d["skill_i"] = [spos[(a, s)] for a, s in zip(d["driver_id"], d["season"], strict=True)]

    driver_types = _typed_index(d, "driver_id", typed)
    d["dt_i"] = driver_types.get_indexer(
        pd.MultiIndex.from_arrays([d["driver_id"], d["speed_profile"]])
    )
    driver_tracks, d["dk_i"] = _index(pd.MultiIndex.from_arrays([d["driver_id"], d["circuit_id"]]))

    return FieldData(
        obs=d,
        sessions=sessions,
        car_keys=car,
        car_types=car_types,
        skill_keys=skill,
        driver_types=driver_types,
        driver_tracks=driver_tracks,
        meta={
            "n_observations": len(d),
            "n_sessions": len(sessions),
            "n_car_rounds": len(car),
            "n_driver_seasons": len(skill),
            "n_driver_tracks": len(driver_tracks),
        },
    )


def build_model(data: FieldData):
    import pymc as pm
    import pytensor.tensor as pt

    o = data.obs
    with pm.Model() as model:
        sigma_obs = pm.HalfNormal("sigma_obs", 1.0)
        nu = pm.Gamma("nu", alpha=2, beta=0.1)
        session = pm.Normal("session", 0, 0.5, shape=len(data.sessions))

        sigma_car = pm.HalfNormal("sigma_car", 1.5)  # spread of car pace at season start
        sigma_upgrade = pm.HalfNormal("sigma_upgrade", 0.2)  # change per round
        car = career_random_walk(data.car_keys, sigma_car, sigma_upgrade, name="car")

        sigma_car_type = pm.HalfNormal("sigma_car_type", 0.3)
        z_ct = pm.Normal("z_car_type", 0, 1, shape=len(data.car_types))
        car_type = pm.Deterministic("car_type", sigma_car_type * z_ct)

        sigma_driver = pm.HalfNormal("sigma_driver", 1.0)
        sigma_walk = pm.HalfNormal("sigma_walk", 0.3)
        skill = career_random_walk(data.skill_keys, sigma_driver, sigma_walk, name="skill")

        sigma_driver_type = pm.HalfNormal("sigma_driver_type", 0.15)
        z_dt = pm.StudentT("z_driver_type", nu=3, mu=0, sigma=1, shape=len(data.driver_types))
        driver_type = pm.Deterministic("driver_type", sigma_driver_type * z_dt)

        sigma_track = pm.HalfNormal("sigma_track", 0.15)
        # heavy tails: most drivers have ~no track bonus, a few real specialists have a big one
        z_dk = pm.StudentT("z_driver_track", nu=3, mu=0, sigma=1, shape=len(data.driver_tracks))
        driver_track = pm.Deterministic("driver_track", sigma_track * z_dk)

        ct_i = o["ct_i"].to_numpy()
        dt_i = o["dt_i"].to_numpy()
        ct = pt.concatenate([car_type, pt.zeros(1)])[np.where(ct_i < 0, len(data.car_types), ct_i)]
        dt = pt.concatenate([driver_type, pt.zeros(1)])[
            np.where(dt_i < 0, len(data.driver_types), dt_i)
        ]
        mu = (
            session[o["j"].to_numpy()]
            + car[o["car_i"].to_numpy()]
            + ct
            + skill[o["skill_i"].to_numpy()]
            + dt
            + driver_track[o["dk_i"].to_numpy()]
        )
        pm.StudentT("pace", nu=nu, mu=mu, sigma=sigma_obs, observed=o["pace_pct"].to_numpy())
    return model


HYPER = [
    "sigma_obs",
    "nu",
    "sigma_car",
    "sigma_upgrade",
    "sigma_car_type",
    "sigma_driver",
    "sigma_walk",
    "sigma_driver_type",
    "sigma_track",
]


def _summ(samples: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    return samples.mean(axis=1), samples.std(axis=1)


def fit(
    df: pd.DataFrame,
    *,
    draws: int = 500,
    tune: int = 500,
    chains: int = 4,
    seed: int = 2027,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fit; returns a long frame of effects (effect, key columns, value, std_err) plus
    diagnostics (hyper-parameters included, e.g. sigma_upgrade for forecasting ahead)."""
    import arviz as az
    import pymc as pm

    data = prepare(df)
    with build_model(data):
        idata = pm.sample(
            draws=draws,
            tune=tune,
            chains=chains,
            nuts_sampler="nutpie",
            random_seed=seed,
            progressbar=False,
        )

    def frame(name: str, cols: dict[str, Any]) -> pd.DataFrame:
        m, s = _summ(idata.posterior[name].stack(sample=("chain", "draw")).values)
        out = pd.DataFrame(cols)
        out["effect"], out["value"], out["std_err"] = name, m, s
        return out

    ck = data.car_keys
    team, season = ck["driver_id"].str.split(":", expand=True).T.to_numpy()
    parts = [
        frame("car", {"constructor_id": team, "season": season.astype(int), "round": ck["season"]}),
        frame(
            "car_type",
            {
                "constructor_id": [t.split(":")[0] for t, _ in data.car_types],
                "season": [int(t.split(":")[1]) for t, _ in data.car_types],
                "speed_profile": [p for _, p in data.car_types],
            },
        ),
        frame(
            "skill",
            {"driver_id": data.skill_keys["driver_id"], "season": data.skill_keys["season"]},
        ),
        frame(
            "driver_type",
            {
                "driver_id": [a for a, _ in data.driver_types],
                "speed_profile": [p for _, p in data.driver_types],
            },
        ),
        frame(
            "driver_track",
            {
                "driver_id": [a for a, _ in data.driver_tracks],
                "circuit_id": [c for _, c in data.driver_tracks],
            },
        ),
    ]
    # visits per driver-circuit, so readers can see how much evidence a track bonus has
    visits = data.obs.groupby(["driver_id", "circuit_id"])["season"].nunique()
    parts[-1]["n_samples"] = visits.reindex(
        pd.MultiIndex.from_frame(parts[-1][["driver_id", "circuit_id"]])
    ).to_numpy()
    effects = pd.concat(parts, ignore_index=True)

    summary = az.summary(idata, var_names=HYPER)
    diagnostics = {
        **data.meta,
        "hyper": summary[["mean", "sd", "r_hat", "ess_bulk"]].round(4).to_dict("index"),
        "max_rhat": float(
            np.nanmax(
                [
                    float(az.rhat(idata, var_names=[v])[v].max())
                    for v in ("car", "skill", "driver_track")
                ]
            )
        ),
        "divergences": int(idata.sample_stats["diverging"].sum()),
        "draws": draws,
        "chains": chains,
    }
    return effects, diagnostics
