"""Tune simulator settings on one season's races (e.g. 2025), to be tested on the next.

For every completed round the event inputs are loaded as of the cutoff (race - 3 days, the
same fits the backtest stored), then the race is simulated from the *actual* grid
("post_quali") and from a simulated grid ("pre_weekend").

Default mode, a grid over:

- ``quali_to_race``: how much of a team's qualifying surprise carries into race pace
- ``pace_scale``: multiplier on race-pace uncertainty (car estimate, weekend form, driver)

``--blend``: with the current defaults, the weight ``w`` of the grid-slot prior in
``p = (1 - w) * simulated + w * prior`` (see ``sim/blend.py``), per stage.

Scored by the winner's log loss and the win Brier score (lower is better).

    python -m f1_forecast.backtest.tune --season 2025 [--sims 2000] [--blend]
"""

from __future__ import annotations

import argparse
import itertools
import logging
import pickle
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from f1_forecast.backtest.metrics import brier, log_loss_of_outcome
from f1_forecast.sim.blend import blend, grid_prior
from f1_forecast.sim.inputs import load_event_inputs
from f1_forecast.sim.quali import observed_team_surprise, simulate_qualifying
from f1_forecast.sim.race import QUALI_TO_RACE, RACE_PACE_SCALE, calibrate_pass_k, simulate_race

log = logging.getLogger(__name__)

GRID_Q2R = (0.3, 0.6, 1.0)
GRID_SCALE = (1.0, 0.7, 0.5)
GRID_W = tuple(np.round(np.arange(0, 1.0001, 0.05), 2))


def load_round(season: int, rnd: int, race_date: date, results: pd.DataFrame, cache: Path):
    f = cache / f"inputs_{season}_{rnd:02d}.pkl"
    if f.exists():
        return pickle.loads(f.read_bytes())
    as_of = race_date - timedelta(days=3)
    ev = load_event_inputs(season, rnd, as_of)
    actual = results[results["round"] == rnd].set_index("driver_id")
    ev.drivers = ev.drivers[ev.drivers["driver_id"].isin(actual.index)].reset_index(drop=True)
    ev.teams = ev.teams[ev.teams["constructor_id"].isin(ev.drivers["constructor_id"])]
    ev.teams = ev.teams.reset_index(drop=True)
    a = actual.reindex(ev.drivers["driver_id"])
    out = (ev, a)
    f.write_bytes(pickle.dumps(out))
    return out


def probs(r) -> pd.DataFrame:
    """Per-driver probabilities in input driver order (RaceResult.summary sorts)."""
    fin = ~r.dnf
    return pd.DataFrame(
        {
            "p_win": ((r.position == 1) & fin).mean(axis=0),
            "p_podium": ((r.position <= 3) & fin).mean(axis=0),
            "p_points": ((r.position <= 10) & fin).mean(axis=0),
        }
    )


def simulate_both(ev, a: pd.DataFrame, sims: int, q2r: float, scale: float, calib=None):
    """(actual grid, post-quali probs, simulated grids, pre-weekend probs, calibration)."""
    grid = a["grid"].where(a["grid"] > 0, 99).rank(method="first").to_numpy().astype(int)
    best = a["quali_best_ms"].astype(float)
    obs = observed_team_surprise(
        ev.drivers, ev.teams, 100 * np.log(best / best.median()), ev.circuit["speed_profile"]
    )
    grids = np.tile(grid, (sims, 1))
    if calib is None:
        calib = calibrate_pass_k(ev.drivers, ev.teams, ev.tyres, ev.circuit, grids, sims=500)
    rng = np.random.default_rng(5)
    car_z = rng.standard_normal((sims, len(ev.teams)))
    skill_z = rng.standard_normal((sims, len(ev.drivers)))
    common = dict(
        n_replays=0,
        car_z=car_z,
        skill_z=skill_z,
        quali_to_race=q2r,
        pace_scale=scale,
        **calib.race_kwargs(),
    )
    post = simulate_race(
        ev.drivers, ev.teams, ev.tyres, ev.circuit, grids, quali_surprise=obs, seed=7, **common
    )
    q = simulate_qualifying(
        ev.drivers,
        ev.teams,
        speed_profile=ev.circuit["speed_profile"],
        sims=sims,
        car_z=car_z,
        skill_z=skill_z,
    )
    pre = simulate_race(
        ev.drivers,
        ev.teams,
        ev.tyres,
        ev.circuit,
        q.grid,
        quali_surprise=q.team_surprise,
        seed=8,
        **common,
    )
    return grid, probs(post), q.grid, probs(pre), calib


def scores(p: pd.DataFrame, a: pd.DataFrame) -> dict:
    won = (a["position"] == 1).to_numpy()
    podium = (a["position"] <= 3).to_numpy()
    return {
        "log_loss_win": log_loss_of_outcome(p["p_win"], won),
        "brier_win": brier(p["p_win"], won),
        "brier_podium": brier(p["p_podium"], podium) / 3,
    }


def score_settings(ev, a: pd.DataFrame, sims: int) -> list[dict]:
    rows, calib = [], None
    for q2r, s in itertools.product(GRID_Q2R, GRID_SCALE):
        grid, post, _, pre, calib = simulate_both(ev, a, sims, q2r, s, calib)
        for stage, p in (("post_quali", post), ("pre_weekend", pre)):
            rows.append(
                {
                    "quali_to_race": q2r,
                    "pace_scale": s,
                    "stage": stage,
                    **scores(p, a),
                    "p_win_pole": float(p["p_win"].iloc[int(np.argmin(grid))]),
                }
            )
    return rows


def score_blend(ev, a: pd.DataFrame, sims: int, table: pd.DataFrame) -> list[dict]:
    grid, post, sim_grids, pre, _ = simulate_both(ev, a, sims, QUALI_TO_RACE, RACE_PACE_SCALE)
    rows = []
    for stage, p, g in (("post_quali", post, grid), ("pre_weekend", pre, sim_grids)):
        prior = grid_prior(g, table)
        for w in GRID_W:
            rows.append({"stage": stage, "w": w, **scores(blend(p, prior, w), a)})
    return rows


def run(season: int, sims: int, cache: Path, *, mode_blend: bool = False) -> pd.DataFrame:
    from databricks.sdk import WorkspaceClient

    from f1_forecast.backtest.race_backtest import CATALOG, Ctx, frequency_tables, history

    w = WorkspaceClient()
    ctx = Ctx(w, next(iter(w.warehouses.list())).id)
    events = ctx.q(f"""
        SELECT round, race_date FROM {CATALOG}.silver.events
        WHERE season = {season} AND status = 'completed' ORDER BY round""")
    hist = history(ctx, season)
    table = frequency_tables(hist, season)["grid"]
    res = hist[(hist["season"] == season) & (hist["session_code"] == "R")]
    cache.mkdir(parents=True, exist_ok=True)
    rows = []
    for e in events.itertuples():
        rnd = int(e.round)
        ev, a = load_round(season, rnd, date.fromisoformat(str(e.race_date)[:10]), res, cache)
        out = score_blend(ev, a, sims, table) if mode_blend else score_settings(ev, a, sims)
        rows += [{"round": rnd, **r} for r in out]
        log.info("round %d done", rnd)
    df = pd.DataFrame(rows)
    keys = ["stage", "w"] if mode_blend else ["stage", "quali_to_race", "pace_scale"]
    return df.drop(columns="round").groupby(keys).mean().reset_index()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--season", type=int, required=True)
    p.add_argument("--sims", type=int, default=2000)
    p.add_argument("--cache", type=Path, default=Path(".cache/tune"))
    p.add_argument("--blend", action="store_true", help="learn the grid-prior blend weights")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = run(args.season, args.sims, args.cache, mode_blend=args.blend)
    with pd.option_context("display.max_rows", 200):
        print(out.round(3).to_string())


if __name__ == "__main__":
    main()
