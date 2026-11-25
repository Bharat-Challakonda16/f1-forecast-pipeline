"""Bayesian teammate-comparison model for continuous driver traits.

Teammates drive the same car, so the gap between them is (mostly) the driver. Chaining
teammates across seasons (A vs B, B vs C, ...) puts every driver on one scale.

Model, for each observation ``i`` = one session where teammates ``a`` and ``b`` both set a
time (``gap = 100 * ln(t_a / t_b)``, positive = ``a`` slower)::

    gap_i       ~ StudentT(nu, skill[a, season] - skill[b, season], sigma_obs)
    skill[d, s] = skill[d, previous season] + sigma_walk * sqrt(years apart) * z
    skill[d, first season] ~ Normal(0, sigma_driver)

* ``skill`` is a time deficit in % of lap time (lower = faster) relative to an average
  driver. Its season-to-season random walk lets form change over a career and makes recent
  seasons count most for the current estimate, while older seasons still stabilise it.
* Drivers with few observations (rookies) stay close to the population prior with wide
  uncertainty - the hierarchical shrinkage the brief asks for.
* The Student-t likelihood keeps a crash or a car failure from dragging an estimate.

Inputs are plain pandas frames, so the same code runs locally on samples and on CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd


@dataclass
class TeammateData:
    """Arrays the model needs, derived from a gaps frame."""

    keys: pd.DataFrame  # one row per (driver_id, season): career order + first flag + gap
    idx_a: np.ndarray
    idx_b: np.ndarray
    gap: np.ndarray
    start_idx: np.ndarray  # position of each row's career start in ``keys``
    year_gap: np.ndarray  # years since the driver's previous modelled season (1 if first)
    is_first: np.ndarray
    n_obs: np.ndarray  # observations per (driver, season)
    meta: dict[str, Any] = field(default_factory=dict)


def career_keys(driver_seasons: pd.DataFrame) -> pd.DataFrame:
    """Order (driver_id, season[, ...]) rows by career and add random-walk bookkeeping.

    Adds ``is_first`` (career start), ``year_gap`` (seasons since the previous modelled
    season, 1 at career start) and ``start_idx`` (row of this driver's career start).
    """
    keys = driver_seasons.sort_values(["driver_id", "season"], kind="stable").reset_index(drop=True)
    keys["is_first"] = keys["driver_id"].ne(keys["driver_id"].shift())
    prev = keys.groupby("driver_id")["season"].shift()
    keys["year_gap"] = (keys["season"] - prev).fillna(1).astype(int)
    keys["start_idx"] = keys.index.to_series().where(keys["is_first"]).ffill().astype(int)
    return keys


def career_random_walk(keys: pd.DataFrame, sigma_driver, sigma_walk, name: str = "skill"):
    """Inside a PyMC model: one value per row of ``keys`` that starts each career at
    ``Normal(0, sigma_driver)`` and moves ``Normal(0, sigma_walk * sqrt(years apart))`` per
    season (non-centred, built as one segmented cumulative sum)."""
    import pymc as pm
    import pytensor.tensor as pt

    z = pm.Normal(f"z_{name}", 0, 1, shape=len(keys))
    is_first = keys["is_first"].to_numpy()
    year_gap = keys["year_gap"].to_numpy().astype(float)
    step = pt.where(is_first, sigma_driver * z, sigma_walk * pt.sqrt(year_gap) * z)
    csum = pt.cumsum(step)
    offset = pt.concatenate([pt.zeros(1), csum])[keys["start_idx"].to_numpy()]
    return pm.Deterministic(name, csum - offset)


def prepare(
    gaps: pd.DataFrame,
    *,
    season_col: str = "season",
    a_col: str = "driver_a",
    b_col: str = "driver_b",
    gap_col: str = "gap_pct",
) -> TeammateData:
    """Index driver-seasons in career order and map each observation onto them."""
    df = gaps[[season_col, a_col, b_col, gap_col]].dropna()
    df = df.astype({season_col: int, gap_col: float})

    ds = pd.concat(
        [
            df[[a_col, season_col]].rename(columns={a_col: "driver_id", season_col: "season"}),
            df[[b_col, season_col]].rename(columns={b_col: "driver_id", season_col: "season"}),
        ]
    )
    keys = career_keys(ds.value_counts().rename("n_obs").reset_index())

    pos = {
        (d, s): i for i, (d, s) in enumerate(zip(keys["driver_id"], keys["season"], strict=True))
    }
    idx_a = np.array([pos[(d, s)] for d, s in zip(df[a_col], df[season_col], strict=True)])
    idx_b = np.array([pos[(d, s)] for d, s in zip(df[b_col], df[season_col], strict=True)])
    return TeammateData(
        keys=keys,
        idx_a=idx_a,
        idx_b=idx_b,
        gap=df[gap_col].to_numpy(),
        start_idx=keys["start_idx"].to_numpy(),
        year_gap=keys["year_gap"].to_numpy(),
        is_first=keys["is_first"].to_numpy(),
        n_obs=keys["n_obs"].to_numpy(),
        meta={"n_observations": len(df), "n_driver_seasons": len(keys)},
    )


def build_model(data: TeammateData, *, prior_scale: float = 1.0):
    """PyMC model (see module docstring). ``prior_scale`` sets the % scale of driver spread."""
    import pymc as pm

    coords = {"driver_season": np.arange(len(data.keys)), "obs": np.arange(len(data.gap))}
    with pm.Model(coords=coords) as model:
        sigma_driver = pm.HalfNormal("sigma_driver", prior_scale)
        sigma_walk = pm.HalfNormal("sigma_walk", 0.3 * prior_scale)
        sigma_obs = pm.HalfNormal("sigma_obs", prior_scale)
        nu = pm.Gamma("nu", alpha=2, beta=0.1)

        skill = career_random_walk(data.keys, sigma_driver, sigma_walk)

        mu = skill[data.idx_a] - skill[data.idx_b]
        pm.StudentT("gap", nu=nu, mu=mu, sigma=sigma_obs, observed=data.gap, dims="obs")
    return model


def fit(
    gaps: pd.DataFrame,
    *,
    draws: int = 1000,
    tune: int = 1000,
    chains: int = 4,
    seed: int = 2027,
    prior_scale: float = 1.0,
    **prepare_kw: Any,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fit the model; returns one row per (driver_id, season) plus sampler diagnostics."""
    import arviz as az
    import pymc as pm

    data = prepare(gaps, **prepare_kw)
    with build_model(data, prior_scale=prior_scale):
        idata = pm.sample(
            draws=draws,
            tune=tune,
            chains=chains,
            nuts_sampler="nutpie",
            random_seed=seed,
            progressbar=False,
        )
    skill = idata.posterior["skill"].stack(sample=("chain", "draw")).values  # (ds, samples)
    out = data.keys[["driver_id", "season", "n_obs"]].copy()
    out["value"] = skill.mean(axis=1)
    out["std_err"] = skill.std(axis=1)
    out["q05"], out["q95"] = np.quantile(skill, [0.05, 0.95], axis=1)

    summary = az.summary(idata, var_names=["sigma_driver", "sigma_walk", "sigma_obs", "nu"])
    rhat_skill = az.rhat(idata, var_names=["skill"])["skill"].values
    diagnostics = {
        **data.meta,
        "hyper": summary[["mean", "sd", "r_hat", "ess_bulk"]].round(4).to_dict("index"),
        "max_rhat_skill": float(np.nanmax(rhat_skill)),
        "divergences": int(idata.sample_stats["diverging"].sum()),
        "draws": draws,
        "chains": chains,
        "prior_scale": prior_scale,
    }
    return out, diagnostics
