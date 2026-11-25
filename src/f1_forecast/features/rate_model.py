"""Bayesian model for rare per-race events per driver (e.g. driver-error retirements).

For each driver-season ``ds`` with ``exposure`` races and ``events`` occurrences::

    events_ds   ~ Poisson(exposure_ds * rate_ds)
    log rate_ds = season_level[season] + skill[ds]
    skill       = career random walk (shared with the teammate model)
    season_level[s] ~ Normal(mu, sigma_season)

* ``season_level`` absorbs what changes for everyone (safer cars and circuits, fewer
  retirements over time, and how completely a season's causes are recorded).
* ``skill`` is the driver's own tendency on the log scale; with few races it stays near
  the season level with wide uncertainty.

The reported value is the rate per race for that driver in that season.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from f1_forecast.features.teammate_model import career_keys, career_random_walk


def prepare(df: pd.DataFrame) -> tuple[pd.DataFrame, np.ndarray]:
    """``df``: driver_id, season, events, exposure -> career-ordered keys + season index."""
    d = df[["driver_id", "season", "events", "exposure"]].dropna()
    d = d.astype({"season": int, "events": int, "exposure": int})
    d = d[d["exposure"] > 0]
    keys = career_keys(d)
    seasons = np.sort(keys["season"].unique())
    season_idx = np.searchsorted(seasons, keys["season"].to_numpy())
    return keys, season_idx


def build_model(keys: pd.DataFrame, season_idx: np.ndarray, *, base_rate: float = 0.07):
    import pymc as pm
    import pytensor.tensor as pt

    n_seasons = int(season_idx.max()) + 1
    with pm.Model() as model:
        mu = pm.Normal("mu", np.log(base_rate), 0.5)
        sigma_season = pm.HalfNormal("sigma_season", 0.3)
        z_season = pm.Normal("z_season", 0, 1, shape=n_seasons)
        season_level = pm.Deterministic("season_level", mu + sigma_season * z_season)

        sigma_driver = pm.HalfNormal("sigma_driver", 0.5)
        sigma_walk = pm.HalfNormal("sigma_walk", 0.2)
        skill = career_random_walk(keys, sigma_driver, sigma_walk)

        rate = pm.Deterministic("rate", pt.exp(season_level[season_idx] + skill))
        pm.Poisson(
            "events",
            mu=keys["exposure"].to_numpy() * rate,
            observed=keys["events"].to_numpy(),
        )
    return model


def fit(
    df: pd.DataFrame,
    *,
    draws: int = 1000,
    tune: int = 1000,
    chains: int = 4,
    seed: int = 2027,
    base_rate: float = 0.07,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    import arviz as az
    import pymc as pm

    keys, season_idx = prepare(df)
    with build_model(keys, season_idx, base_rate=base_rate):
        idata = pm.sample(
            draws=draws,
            tune=tune,
            chains=chains,
            nuts_sampler="nutpie",
            random_seed=seed,
            progressbar=False,
        )
    rate = idata.posterior["rate"].stack(sample=("chain", "draw")).values
    out = keys[["driver_id", "season"]].copy()
    out["n_obs"] = keys["exposure"].to_numpy()
    out["value"] = rate.mean(axis=1)
    out["std_err"] = rate.std(axis=1)
    out["q05"], out["q95"] = np.quantile(rate, [0.05, 0.95], axis=1)

    summary = az.summary(idata, var_names=["mu", "sigma_season", "sigma_driver", "sigma_walk"])
    diagnostics = {
        "n_observations": int(keys["exposure"].sum()),
        "n_events": int(keys["events"].sum()),
        "n_driver_seasons": len(keys),
        "hyper": summary[["mean", "sd", "r_hat", "ess_bulk"]].round(4).to_dict("index"),
        "max_rhat_rate": float(np.nanmax(az.rhat(idata, var_names=["rate"])["rate"].values)),
        "divergences": int(idata.sample_stats["diverging"].sum()),
        "draws": draws,
        "chains": chains,
    }
    return out, diagnostics
