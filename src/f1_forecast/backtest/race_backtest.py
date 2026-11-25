"""Season backtest: predict every completed race with only the data known before it.

For each round (cutoff = race date - 3 days, i.e. before the weekend starts):

1. Make sure all driver traits are fitted with exactly that ``data_cutoff`` (fit if not).
2. Load event inputs as of the cutoff (circuit parameters from earlier races only).
3. Predict qualifying, the race from the simulated grid ("pre_weekend"), and the race
   from the actual grid ("post_quali"). Store all predictions in ``gold.predictions``.
4. Score them, and three simple baselines, against what happened.

Baselines turn a ranking into probabilities with frequencies from the four seasons before
(e.g. "the championship leader wins x% of races"), so they're scored with the same metrics:

- ``championship``: order of the drivers' standings before the race
- ``previous_race``: order of the previous race's result
- ``grid``: the actual starting grid (only for the post-qualifying comparison)

    python -m f1_forecast.backtest.race_backtest --season 2026 [--rounds 5 6] [--sims 10000]
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd

from f1_forecast.backtest.metrics import calibration_table, event_scores, rank_baseline_probs
from f1_forecast.features.run_field import MODEL_VERSION as FIELD_VERSION
from f1_forecast.features.run_field import run as fit_field
from f1_forecast.features.run_traits import CATALOG, OUTPUT_VOLUME, TRAITS, execute, query_df
from f1_forecast.features.run_traits import run as fit_trait
from f1_forecast.sim.inputs import load_event_inputs
from f1_forecast.sim.quali import observed_team_surprise, simulate_qualifying
from f1_forecast.sim.race import calibrate_pass_k, simulate_race

log = logging.getLogger(__name__)
# sim_v2: shared estimate draws for qualifying and race, qualifying surprise carried into
# race pace, race-wide strategy consensus and undercut cover.
# sim_v3: + joint field model (car per round incl. upgrades, driver x track bonuses).
# sim_v4: + lap-1 spread growing down the grid, retirement rates scaled to the field level.
MODEL_VERSION = "sim_v4"


@dataclass
class Ctx:
    w: object
    wh: str

    def q(self, sql: str) -> pd.DataFrame:
        return query_df(self.w, self.wh, sql)

    def x(self, sql: str) -> None:
        execute(self.w, self.wh, sql)


# --------------------------------------------------------------------------- data


def ensure_traits(ctx: Ctx, as_of: date, draws: int) -> list[str]:
    have = ctx.q(
        f"SELECT DISTINCT trait FROM {CATALOG}.gold.driver_traits WHERE data_cutoff = DATE'{as_of}'"
    )["trait"].tolist()
    fitted = []
    for name in TRAITS:  # dict order: wet_skill last (uses the dry traits)
        if name not in have:
            fit_trait(name, as_of, draws=draws, tune=draws, chains=4)
            fitted.append(name)
    return fitted


def ensure_field(ctx: Ctx, as_of: date) -> list[str]:
    """Fit the joint field model at exactly this cutoff if it isn't stored yet."""
    exists = ctx.q(f"SHOW TABLES IN {CATALOG}.gold LIKE 'field_effects'")
    have = []
    if not exists.empty:
        have = ctx.q(f"""
            SELECT DISTINCT kind FROM {CATALOG}.gold.field_effects
            WHERE model_version = '{FIELD_VERSION}' AND data_cutoff = DATE'{as_of}'""")[
            "kind"
        ].tolist()
    missing = [k for k in ("quali", "race") if k not in have]
    if missing:
        fit_field(as_of, missing)
    return [f"field_{k}" for k in missing]


def history(ctx: Ctx, season: int) -> pd.DataFrame:
    """Race and sprint results for the season and the four before (for baselines)."""
    df = ctx.q(f"""
        SELECT r.season, r.round, r.session_code, r.driver_id, r.grid, r.position, r.points,
               q.position AS quali_position, least(q.q1_ms, q.q2_ms, q.q3_ms) AS quali_best_ms
        FROM {CATALOG}.silver.results r
        LEFT JOIN {CATALOG}.silver.qualifying q
          ON q.season = r.season AND q.round = r.round AND q.session_code = 'Q'
         AND q.driver_id = r.driver_id AND r.session_code = 'R'
        WHERE r.season BETWEEN {season - 4} AND {season}""")
    for c in ("season", "round", "grid", "position", "points", "quali_position", "quali_best_ms"):
        df[c] = pd.to_numeric(df[c])
    return df


def baseline_ranks(hist: pd.DataFrame, season: int, rnd: int, drivers: list[str]) -> dict:
    """Per baseline, a rank (1 = favourite) for each driver in ``drivers``."""
    races = hist[hist["session_code"] == "R"]
    before = hist[(hist["season"] < season) | ((hist["season"] == season) & (hist["round"] < rnd))]
    if rnd == 1:  # standings carry over from the end of last season
        standings = before[before["season"] == season - 1].groupby("driver_id")["points"].sum()
    else:
        standings = before[before["season"] == season].groupby("driver_id")["points"].sum()
    key = races["season"] * 100 + races["round"]
    this_key = season * 100 + rnd
    prev_races = races[key < this_key]
    last = (
        prev_races[key[key < this_key] == key[key < this_key].max()]
        if len(prev_races)
        else prev_races
    )
    this = races[key == this_key].set_index("driver_id")

    def rank_by(score: pd.Series, ascending: bool) -> np.ndarray:
        s = score.reindex(drivers).astype(float)
        if s.isna().all():
            s = pd.Series(0.0, index=drivers)  # nothing known: everyone equal
        s = s.fillna(s.max() + 1 if ascending else s.min() - 1)
        return s.rank(ascending=ascending, method="first").to_numpy().astype(int)

    out = {
        "championship": rank_by(standings, ascending=False),
        "previous_race": rank_by(last.set_index("driver_id")["position"], ascending=True),
    }
    grid = this["grid"].where(this["grid"] > 0, 99) if len(this) else pd.Series(dtype=float)
    out["grid"] = rank_by(grid, ascending=True)
    prev_q = prev_races[prev_races["quali_position"].notna()]
    if len(prev_q):
        qkey = prev_q["season"] * 100 + prev_q["round"]
        lq = prev_q[qkey == qkey.max()]
        out["previous_quali"] = rank_by(lq.set_index("driver_id")["quali_position"], ascending=True)
    return out


def frequency_tables(hist: pd.DataFrame, season: int) -> dict[str, pd.DataFrame]:
    """P(win / podium / points | baseline rank) from the four seasons before ``season``."""
    tables: dict[str, list] = {
        "championship": [],
        "previous_race": [],
        "grid": [],
        "previous_quali": [],
    }
    races = hist[
        (hist["session_code"] == "R") & (hist["season"] < season) & (hist["season"] >= season - 4)
    ]
    for (s, r), g in races.groupby(["season", "round"]):
        drivers = g["driver_id"].tolist()
        ranks = baseline_ranks(hist, int(s), int(r), drivers)
        pos = g["position"].to_numpy()
        qpos = g["quali_position"].to_numpy()
        for name, rk in ranks.items():
            if name == "previous_quali":
                tables[name] += [
                    (k, float(q == 1), float(q <= 3), float(q <= 10))
                    for k, q in zip(rk, qpos, strict=True)
                    if q == q
                ]
            else:
                tables[name] += [
                    (k, float(p == 1), float(p <= 3), float(p <= 10))
                    for k, p in zip(rk, pos, strict=True)
                ]
    out = {}
    for name, rows in tables.items():
        df = pd.DataFrame(rows, columns=["rank", "p_win", "p_podium", "p_points"])
        out[name] = df.groupby("rank").mean().reset_index()
    return out


# --------------------------------------------------------------------------- predict


def predict_round(
    ctx: Ctx,
    season: int,
    rnd: int,
    as_of: date,
    sims: int,
    actual: pd.DataFrame,
    use_field: bool = True,
):
    ev = load_event_inputs(season, rnd, as_of, use_field=use_field)
    # Score only drivers who actually raced (the entry list is known before a weekend).
    ev.drivers = ev.drivers[ev.drivers["driver_id"].isin(actual["driver_id"])].reset_index(
        drop=True
    )
    ev.teams = ev.teams[ev.teams["constructor_id"].isin(ev.drivers["constructor_id"])].reset_index(
        drop=True
    )
    # One draw of "how wrong are our estimates" per simulation, shared by qualifying and the
    # race (same car, same drivers).
    rng = np.random.default_rng(2027)
    car_z = rng.standard_normal((sims, len(ev.teams)))
    skill_z = rng.standard_normal((sims, len(ev.drivers)))
    q = simulate_qualifying(
        ev.drivers,
        ev.teams,
        speed_profile=ev.circuit["speed_profile"],
        sims=sims,
        car_z=car_z,
        skill_z=skill_z,
    )
    calib = calibrate_pass_k(ev.drivers, ev.teams, ev.tyres, ev.circuit, q.grid)
    pre = simulate_race(
        ev.drivers,
        ev.teams,
        ev.tyres,
        ev.circuit,
        q.grid,
        n_replays=0,
        car_z=car_z,
        skill_z=skill_z,
        quali_surprise=q.team_surprise,
        **calib.race_kwargs(),
    )
    best = actual.set_index("driver_id")["quali_best_ms"].astype(float)
    quali_pct = 100 * np.log(best / best.median())
    surprise = observed_team_surprise(ev.drivers, ev.teams, quali_pct, ev.circuit["speed_profile"])
    grid_actual = (
        actual.set_index("driver_id")["grid"]
        .reindex(ev.drivers["driver_id"])
        .fillna(99)
        .replace(0, 99)
        .rank(method="first")
        .to_numpy()
        .astype(int)
    )
    post = simulate_race(
        ev.drivers,
        ev.teams,
        ev.tyres,
        ev.circuit,
        np.tile(grid_actual, (sims, 1)),
        seed=7,
        n_replays=0,
        car_z=car_z,
        skill_z=skill_z,
        quali_surprise=surprise,
        **calib.race_kwargs(),
    )
    qs = q.summary().drop(columns="grid_distribution").rename(columns={"p_top3": "p_top3_grid"})
    rows = []
    for stage, res in (("pre_weekend", pre), ("post_quali", post)):
        s = res.summary().merge(
            qs if stage == "pre_weekend" else qs[["driver_id"]], on="driver_id", how="left"
        )
        s["stage"] = stage
        rows.append(s)
    pred = pd.concat(rows, ignore_index=True)
    pred["season"], pred["round"], pred["data_cutoff"] = season, rnd, as_of
    pred["circuit_id"] = ev.circuit["circuit_id"]
    pred["pass_k"], pred["slipstream"] = calib.k, calib.slipstream
    return pred, ev


def store(ctx: Ctx, df: pd.DataFrame, table: str, keys: list[str], name: str) -> None:
    ctx.x(f"CREATE VOLUME IF NOT EXISTS {CATALOG}.gold.model_outputs")
    path = f"{OUTPUT_VOLUME}/backtest/{table}/{name}.parquet"
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    ctx.w.files.upload(path, io.BytesIO(buf.getvalue()), overwrite=True)
    src = f"read_files('{path}', format => 'parquet')"
    exists = ctx.q(f"SHOW TABLES IN {CATALOG}.gold LIKE '{table}'")
    if exists.empty:
        ctx.x(f"CREATE TABLE {CATALOG}.gold.{table} AS SELECT * FROM {src}")
        return
    on = " AND ".join(f"t.{k} <=> s.{k}" for k in keys)
    ctx.x(
        f"MERGE INTO {CATALOG}.gold.{table} t USING (SELECT * FROM {src}) s ON {on} "
        "WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *"
    )


# --------------------------------------------------------------------------- run


def run(
    season: int,
    rounds: list[int] | None,
    sims: int,
    fit_draws: int,
    *,
    use_field: bool = True,
    model_version: str = MODEL_VERSION,
) -> dict:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    ctx = Ctx(w, next(iter(w.warehouses.list())).id)
    events = ctx.q(f"""
        SELECT round, race_date FROM {CATALOG}.silver.events
        WHERE season = {season} AND status = 'completed' ORDER BY round""").astype({"round": int})
    if rounds:
        events = events[events["round"].isin(rounds)]
    hist = history(ctx, season)
    freq = frequency_tables(hist, season)
    created = datetime.now(UTC).replace(tzinfo=None)
    run_id = uuid.uuid4().hex[:8]

    all_scores, all_preds = [], []
    for row in events.itertuples():
        t0 = time.monotonic()
        rnd = int(row.round)
        as_of = date.fromisoformat(str(row.race_date)[:10]) - timedelta(days=3)
        fitted = ensure_traits(ctx, as_of, fit_draws)
        if use_field:
            fitted += ensure_field(ctx, as_of)
        races = hist[
            (hist["season"] == season) & (hist["round"] == rnd) & (hist["session_code"] == "R")
        ]
        actual = races[["driver_id", "grid", "position", "quali_position", "quali_best_ms"]].copy()
        pred, ev = predict_round(ctx, season, rnd, as_of, sims, actual, use_field)
        pred["snapshot_id"] = pred["stage"] + f":{season}_{rnd:02d}:{as_of}:{model_version}"
        pred["model_version"], pred["created_at"] = model_version, created
        all_preds.append(pred)

        a = actual.set_index("driver_id")
        for stage in ("pre_weekend", "post_quali"):
            p = pred[pred["stage"] == stage].copy()
            p["actual_rank"] = a["position"].reindex(p["driver_id"]).to_numpy()
            all_scores.append(
                {"round": rnd, "stage": stage, "method": "model", "kind": "race", **event_scores(p)}
            )
            drivers = p["driver_id"].tolist()
            ranks = baseline_ranks(hist, season, rnd, drivers)
            for name in ("championship", "previous_race") + (
                ("grid",) if stage == "post_quali" else ()
            ):
                b = rank_baseline_probs(ranks[name], freq[name])
                b["actual_rank"] = p["actual_rank"].to_numpy()
                all_scores.append(
                    {
                        "round": rnd,
                        "stage": stage,
                        "method": name,
                        "kind": "race",
                        **event_scores(b),
                    }
                )
        # qualifying (pre-weekend only)
        p = pred[pred["stage"] == "pre_weekend"].copy()
        p["actual_rank"] = a["quali_position"].reindex(p["driver_id"]).to_numpy()
        if p["actual_rank"].notna().sum() >= 10:
            p["actual_rank"] = p["actual_rank"].fillna(99).rank(method="first")
            all_scores.append(
                {
                    "round": rnd,
                    "stage": "qualifying",
                    "method": "model",
                    "kind": "quali",
                    **event_scores(p, kind="quali"),
                }
            )
            ranks = baseline_ranks(hist, season, rnd, p["driver_id"].tolist())
            for name in ("championship", "previous_quali"):
                if name in ranks:
                    b = rank_baseline_probs(ranks[name], freq[name]).rename(
                        columns={
                            "p_win": "p_pole",
                            "p_podium": "p_top3_grid",
                            "expected_position": "expected_grid",
                        }
                    )
                    b["actual_rank"] = p["actual_rank"].to_numpy()
                    all_scores.append(
                        {
                            "round": rnd,
                            "stage": "qualifying",
                            "method": name,
                            "kind": "quali",
                            **event_scores(b, kind="quali"),
                        }
                    )
        log.info(
            "round %d (%s) done in %.0fs; fitted %s",
            rnd,
            ev.circuit["circuit_id"],
            time.monotonic() - t0,
            fitted or "nothing",
        )

    preds = pd.concat(all_preds, ignore_index=True)
    keep = [
        "snapshot_id",
        "model_version",
        "stage",
        "season",
        "round",
        "circuit_id",
        "data_cutoff",
        "created_at",
        "driver_id",
        "constructor_id",
        "p_pole",
        "p_front_row",
        "p_top3_grid",
        "p_top10",
        "expected_grid",
        "p_win",
        "p_podium",
        "p_points",
        "p_dnf",
        "expected_position",
        "expected_points",
        "pass_k",
        "slipstream",
    ]
    preds = preds[[c for c in keep if c in preds.columns]]
    store(ctx, preds, "predictions", ["snapshot_id", "driver_id"], f"predictions_{season}_{run_id}")

    scores = pd.DataFrame(all_scores)
    long = scores.melt(
        id_vars=["round", "stage", "method", "kind"], var_name="metric", value_name="value"
    ).dropna()
    long["season"], long["model_version"], long["created_at"] = season, model_version, created
    store(
        ctx,
        long,
        "backtest_events",
        ["model_version", "season", "round", "stage", "method", "metric"],
        f"events_{season}_{run_id}",
    )
    summary = (
        long.groupby(["stage", "method", "metric"])["value"].agg(["mean", "count"]).reset_index()
    )
    summary = summary.rename(columns={"mean": "value", "count": "n_events"})
    summary["season"], summary["model_version"], summary["created_at"] = (
        season,
        model_version,
        created,
    )
    store(
        ctx,
        summary,
        "backtest_results",
        ["model_version", "season", "stage", "method", "metric"],
        f"results_{season}_{run_id}",
    )

    cal = []
    for stage, col, outcome in (
        ("pre_weekend", "p_podium", 3),
        ("pre_weekend", "p_win", 1),
        ("post_quali", "p_podium", 3),
    ):
        p = preds[preds["stage"] == stage].merge(
            hist[(hist["season"] == season) & (hist["session_code"] == "R")][
                ["round", "driver_id", "position"]
            ],
            on=["round", "driver_id"],
        )
        t = calibration_table(p[col].to_numpy(), (p["position"] <= outcome).to_numpy())
        t["stage"], t["prediction"] = stage, col
        cal.append(t)
    calib = pd.concat(cal, ignore_index=True)
    calib["season"], calib["model_version"], calib["created_at"] = season, model_version, created
    store(
        ctx,
        calib,
        "backtest_calibration",
        ["model_version", "season", "stage", "prediction", "bin_low"],
        f"calibration_{season}_{run_id}",
    )
    return {"rounds": len(events), "predictions": len(preds), "summary": summary.to_dict("records")}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--season", type=int, required=True)
    p.add_argument("--rounds", type=int, nargs="+")
    p.add_argument("--sims", type=int, default=10_000)
    p.add_argument("--fit-draws", type=int, default=500)
    p.add_argument("--no-field", action="store_true", help="old car/driver inputs (sim_v2)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = run(
        args.season,
        args.rounds,
        args.sims,
        args.fit_draws,
        use_field=not args.no_field,
        model_version="sim_v2" if args.no_field else MODEL_VERSION,
    )
    print(json.dumps({k: v for k, v in out.items() if k != "summary"}, indent=1))
    s = pd.DataFrame(out["summary"]).pivot_table(
        index=["stage", "metric"], columns="method", values="value"
    )
    print(s.round(3).to_string())


if __name__ == "__main__":
    main()
