"""Season simulator: championship odds from the current standings plus every remaining
Grand Prix and sprint, each simulated with the qualifying and race engines.

Within one simulated season, the uncertainty of our *estimates* (is this car or driver
faster than we think?) is drawn once and shared by all remaining events; weekend form,
strategy and luck are drawn per event. Treating each race independently would make the
favourite's title odds overconfident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from f1_forecast.sim.inputs import CATALOG, EventInputs, load_event_inputs
from f1_forecast.sim.quali import simulate_qualifying
from f1_forecast.sim.race import POINTS, calibrate_pass_k, simulate_race

SPRINT_POINTS = np.array([8, 7, 6, 5, 4, 3, 2, 1], float)
SPRINT_DISTANCE_SHARE = 1 / 3  # sprints are ~100 km


@dataclass
class SeasonResult:
    season: int
    as_of: date
    drivers: pd.DataFrame
    constructors: pd.DataFrame
    events: list[dict] = field(default_factory=list)  # per-event race summaries


def points_for(position: np.ndarray, dnf: np.ndarray, table: np.ndarray) -> np.ndarray:
    idx = np.clip(position - 1, 0, len(table) - 1)
    return np.where(~dnf & (position <= len(table)), table[idx], 0.0)


def championship(
    entity_ids: list[str],
    current: np.ndarray,
    future: np.ndarray,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """``future``: (sims, entities) points still to come. Ties broken at random."""
    total = current[None, :] + future
    jitter = rng.random(total.shape) * 1e-6
    rank = (-(total + jitter)).argsort(axis=1).argsort(axis=1) + 1
    n = len(entity_ids)
    dist = np.stack([(rank == p).mean(axis=0) for p in range(1, n + 1)], axis=1)
    out = pd.DataFrame(
        {
            "id": entity_ids,
            "current_points": current,
            "expected_points": total.mean(axis=0),
            "points_p05": np.percentile(total, 5, axis=0),
            "points_p95": np.percentile(total, 95, axis=0),
            "p_title": (rank == 1).mean(axis=0),
            "p_top3": (rank <= 3).mean(axis=0),
            "expected_rank": rank.mean(axis=0),
        }
    )
    out["rank_distribution"] = list(dist)
    return out.sort_values("expected_points", ascending=False).reset_index(drop=True)


def simulate_event(
    ev: EventInputs,
    car_z_race: np.ndarray,
    car_z_quali: np.ndarray,
    skill_z_race: np.ndarray,
    skill_z_quali: np.ndarray,
    *,
    sims: int,
    seed: int,
    sprint: bool,
) -> tuple[np.ndarray, np.ndarray, pd.DataFrame]:
    """One weekend: (GP points, sprint points) per (sim, driver in ev.drivers order)."""
    kw = {"speed_profile": ev.circuit["speed_profile"], "sims": sims}
    q = simulate_qualifying(
        ev.drivers, ev.teams, seed=seed, car_z=car_z_quali, skill_z=skill_z_quali, **kw
    )
    calib = calibrate_pass_k(ev.drivers, ev.teams, ev.tyres, ev.circuit, q.grid)
    race = simulate_race(
        ev.drivers,
        ev.teams,
        ev.tyres,
        ev.circuit,
        q.grid,
        seed=seed + 1,
        car_z=car_z_race,
        skill_z=skill_z_race,
        n_replays=0,
        **calib.race_kwargs(),
    )
    gp_points = points_for(race.position, race.dnf, POINTS)
    sprint_points = np.zeros_like(gp_points)
    if sprint:
        sq = simulate_qualifying(
            ev.drivers, ev.teams, seed=seed + 2, car_z=car_z_quali, skill_z=skill_z_quali, **kw
        )
        short = {**ev.circuit, "laps": max(10, round(ev.circuit["laps"] * SPRINT_DISTANCE_SHARE))}
        sr = simulate_race(
            ev.drivers,
            ev.teams,
            ev.tyres,
            short,
            sq.grid,
            seed=seed + 3,
            car_z=car_z_race,
            skill_z=skill_z_race,
            n_replays=0,
            sprint=True,
            **calib.race_kwargs(),
        )
        sprint_points = points_for(sr.position, sr.dnf, SPRINT_POINTS)
    return gp_points, sprint_points, race.summary()


def current_standings(w, wh, season: int, as_of: date) -> tuple[pd.DataFrame, pd.DataFrame]:
    from f1_forecast.features.run_traits import query_df

    res = query_df(
        w,
        wh,
        f"""
        SELECT r.driver_id, r.constructor_id, r.points
        FROM {CATALOG}.silver.results r
        JOIN {CATALOG}.silver.events e ON e.season = r.season AND e.round = r.round
        WHERE r.season = {season} AND e.race_date < DATE'{as_of}'""",
    ).astype({"points": float})
    drivers = res.groupby("driver_id")["points"].sum()
    teams = res.groupby("constructor_id")["points"].sum()
    return drivers.rename("points").reset_index(), teams.rename("points").reset_index()


def remaining_events(w, wh, season: int, as_of: date) -> pd.DataFrame:
    from f1_forecast.features.run_traits import query_df

    return query_df(
        w,
        wh,
        f"""
        SELECT round, event_name, is_sprint, race_date FROM {CATALOG}.silver.events
        WHERE season = {season} AND status <> 'cancelled' AND race_date >= DATE'{as_of}'
        ORDER BY round""",
    ).astype({"round": int})


def simulate_season(
    season: int, as_of: date, *, sims: int = 10_000, seed: int = 2027
) -> SeasonResult:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    wh = next(iter(w.warehouses.list())).id
    rng = np.random.default_rng(seed)

    standings_d, standings_t = current_standings(w, wh, season, as_of)
    todo = remaining_events(w, wh, season, as_of)
    events = [load_event_inputs(season, int(r), as_of) for r in todo["round"]]

    driver_ids = sorted(
        set(standings_d["driver_id"]).union(*(set(e.drivers["driver_id"]) for e in events))
    )
    team_ids = sorted(
        set(standings_t["constructor_id"]).union(*(set(e.teams["constructor_id"]) for e in events))
    )
    d_pos = {d: i for i, d in enumerate(driver_ids)}
    t_pos = {t: i for i, t in enumerate(team_ids)}

    # shared estimate-uncertainty draws for this whole simulated season
    z_car_race = rng.standard_normal((sims, len(team_ids)))
    z_car_quali = 0.8 * z_car_race + 0.6 * rng.standard_normal((sims, len(team_ids)))
    z_skill_race = rng.standard_normal((sims, len(driver_ids)))
    z_skill_quali = rng.standard_normal((sims, len(driver_ids)))

    future_d = np.zeros((sims, len(driver_ids)))
    future_t = np.zeros((sims, len(team_ids)))
    summaries = []
    for i, (ev, row) in enumerate(zip(events, todo.itertuples(), strict=True)):
        di = ev.drivers["driver_id"].map(d_pos).to_numpy()
        ti = ev.teams["constructor_id"].map(t_pos).to_numpy()
        gp, sp, summary = simulate_event(
            ev,
            z_car_race[:, ti],
            z_car_quali[:, ti],
            z_skill_race[:, di],
            z_skill_quali[:, di],
            sims=sims,
            seed=seed + 100 * (i + 1),
            sprint=bool(row.is_sprint in (True, "true")),
        )
        pts = gp + sp
        np.add.at(future_d, (slice(None), di), pts)
        team_of = ev.drivers["constructor_id"].map(t_pos).to_numpy()
        for j, tj in enumerate(team_of):
            future_t[:, tj] += pts[:, j]
        summaries.append(
            {"round": int(row.round), "event_name": row.event_name, "summary": summary}
        )

    cur_d = standings_d.set_index("driver_id")["points"].reindex(driver_ids).fillna(0).to_numpy()
    cur_t = standings_t.set_index("constructor_id")["points"].reindex(team_ids).fillna(0).to_numpy()
    drivers = championship(driver_ids, cur_d, future_d, rng).rename(columns={"id": "driver_id"})
    constructors = championship(team_ids, cur_t, future_t, rng).rename(
        columns={"id": "constructor_id"}
    )
    return SeasonResult(season, as_of, drivers, constructors, summaries)
