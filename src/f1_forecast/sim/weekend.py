"""One race weekend, forecast at every stage the data allows.

Shared by the backtest and the live forecasts so both run exactly the same model:

- ``pre_weekend``: qualifying and race simulated from the model alone
- ``post_practice``: car / driver pace shifted by practice (``sim/practice.py``)
- ``post_quali``: the race from the actual grid, with the observed qualifying surprise

The uncertainty of our estimates (is this car or driver faster than we think?) is drawn
once and shared by every stage and by qualifying and the race. Race probabilities are then
blended with grid-slot history (``sim/blend.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from f1_forecast.sim.blend import GRID_BLEND, blend, grid_prior
from f1_forecast.sim.inputs import EventInputs
from f1_forecast.sim.practice import apply_practice, practice_surprise
from f1_forecast.sim.quali import QualiResult, observed_team_surprise, simulate_qualifying
from f1_forecast.sim.race import RaceResult, calibrate_pass_k, simulate_race


@dataclass
class WeekendForecast:
    pred: pd.DataFrame  # one row per driver per stage: race (+ qualifying) probabilities
    races: dict[str, RaceResult] = field(default_factory=dict)
    qualis: dict[str, QualiResult] = field(default_factory=dict)
    pass_k: float = 0.0
    slipstream: float = 0.0


def _quali_summary(q: QualiResult) -> pd.DataFrame:
    s = q.summary().drop(columns="grid_distribution")
    return s.rename(columns={"p_top3": "p_top3_grid"})


def simulate_weekend(
    ev: EventInputs,
    sims: int,
    *,
    practice: pd.DataFrame | None = None,
    grid: np.ndarray | None = None,
    quali_pct: pd.Series | None = None,
    grid_table: pd.DataFrame | None = None,
    seed: int = 2027,
) -> WeekendForecast:
    """``practice``: this weekend's rows of ``gold.driver_practice_pace`` (None: no
    post_practice stage). ``grid``: actual grid slot per driver in ``ev.drivers`` order and
    ``quali_pct``: qualifying pace (% vs the median) by driver id (None: no post_quali
    stage). ``grid_table``: grid-slot frequencies for the blend (None: no blend)."""
    rng = np.random.default_rng(seed)
    car_z = rng.standard_normal((sims, len(ev.teams)))
    skill_z = rng.standard_normal((sims, len(ev.drivers)))
    sp = ev.circuit["speed_profile"]

    q = simulate_qualifying(
        ev.drivers, ev.teams, speed_profile=sp, sims=sims, car_z=car_z, skill_z=skill_z
    )
    calib = calibrate_pass_k(ev.drivers, ev.teams, ev.tyres, ev.circuit, q.grid)
    common = dict(n_replays=0, car_z=car_z, skill_z=skill_z, **calib.race_kwargs())
    pre = simulate_race(
        ev.drivers, ev.teams, ev.tyres, ev.circuit, q.grid, quali_surprise=q.team_surprise, **common
    )
    stages: list[tuple[str, RaceResult, np.ndarray, QualiResult | None]] = [
        ("pre_weekend", pre, q.grid, q)
    ]

    if practice is not None:
        surprise = practice_surprise(ev.drivers, ev.teams, practice)
        dp, tp = apply_practice(ev.drivers, ev.teams, surprise)
        qp = simulate_qualifying(dp, tp, speed_profile=sp, sims=sims, car_z=car_z, skill_z=skill_z)
        pp = simulate_race(
            dp, tp, ev.tyres, ev.circuit, qp.grid, seed=9, quali_surprise=qp.team_surprise, **common
        )
        stages.append(("post_practice", pp, qp.grid, qp))

    if grid is not None and quali_pct is not None:
        obs = observed_team_surprise(ev.drivers, ev.teams, quali_pct, sp)
        post = simulate_race(
            ev.drivers,
            ev.teams,
            ev.tyres,
            ev.circuit,
            np.tile(np.asarray(grid, int), (sims, 1)),
            seed=7,
            quali_surprise=obs,
            **common,
        )
        stages.append(("post_quali", post, np.asarray(grid, int), None))

    rows = []
    for stage, res, g, qr in stages:
        s = res.summary()
        if grid_table is not None:
            prior = grid_prior(g, grid_table)
            prior["driver_id"] = res.driver_ids
            prior = prior.set_index("driver_id").reindex(s["driver_id"]).reset_index()
            s = blend(s, prior, GRID_BLEND[stage])
        if qr is not None:
            s = s.merge(_quali_summary(qr), on="driver_id", how="left")
        s["stage"] = stage
        rows.append(s)
    return WeekendForecast(
        pred=pd.concat(rows, ignore_index=True),
        races={st: r for st, r, _, _ in stages},
        qualis={st: qr for st, _, _, qr in stages if qr is not None},
        pass_k=calib.k,
        slipstream=calib.slipstream,
    )
