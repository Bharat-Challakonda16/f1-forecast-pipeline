"""Forecast the next race from today's data, plus the championship, and publish the results.

Picks the first event that hasn't happened yet (race day today or later) and runs every
stage the data allows (same model as the backtest, ``sim/weekend.py``):

- always ``pre_weekend``
- ``post_practice`` once practice lap times are in ``gold.driver_practice_pace``
- ``post_quali`` once the qualifying result is in ``silver.qualifying`` (grid = qualifying
  order; grid penalties are not known yet)

Then the championship odds from today's standings and every remaining event.

Outputs:
- ``gold.predictions`` rows (``snapshot_id`` starting with ``live:``)
- small JSON files for the app in ``--out`` (default ``site_data/``): ``next_race.json``,
  ``season.json``, ``accuracy.json``, ``meta.json``

    python -m f1_forecast.predict.live [--today 2026-11-27] [--sims 10000] [--out site_data]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from f1_forecast.backtest.race_backtest import (
    CATALOG,
    MODEL_VERSION,
    Ctx,
    frequency_tables,
    history,
    quali_priors,
    store,
)
from f1_forecast.sim.blend import QUALI_BLEND, QUALI_COLS, blend_many
from f1_forecast.sim.inputs import load_event_inputs
from f1_forecast.sim.season import simulate_season
from f1_forecast.sim.weekend import simulate_weekend

log = logging.getLogger(__name__)

STAGE_LABELS = {
    "pre_weekend": "Before the weekend",
    "post_practice": "After practice",
    "post_quali": "After qualifying",
}
TOP_STRATEGIES = 3


# --------------------------------------------------------------------------- data


def next_event(ctx: Ctx, today: date) -> dict[str, Any]:
    ev = ctx.q(f"""
        SELECT e.season, e.round, e.event_name, e.circuit_id, e.race_date, e.race_start_utc,
               e.is_sprint, c.name AS circuit_name, c.country
        FROM {CATALOG}.silver.events e LEFT JOIN {CATALOG}.silver.circuits c USING (circuit_id)
        WHERE e.status <> 'cancelled' AND e.race_date >= DATE'{today}'
        ORDER BY e.race_date LIMIT 1""")
    if ev.empty:
        raise ValueError(f"No upcoming event after {today}")
    r = ev.iloc[0].to_dict()
    r["season"], r["round"] = int(r["season"]), int(r["round"])
    r["race_date"] = str(r["race_date"])[:10]
    r["is_sprint"] = str(r["is_sprint"]).lower() == "true"
    return r


def practice_rows(ctx: Ctx, season: int, rnd: int) -> pd.DataFrame | None:
    fp = ctx.q(f"""
        SELECT round, session_code, driver_id, best_pct, long_pct, long_laps
        FROM {CATALOG}.gold.driver_practice_pace
        WHERE season = {season} AND round = {rnd} AND NOT is_outlier""")
    if fp.empty:
        return None
    for c in ("round", "best_pct", "long_pct", "long_laps"):
        fp[c] = pd.to_numeric(fp[c])
    return fp


def quali_result(ctx: Ctx, season: int, rnd: int) -> pd.DataFrame | None:
    q = ctx.q(f"""
        SELECT driver_id, position, least(q1_ms, q2_ms, q3_ms) AS best_ms
        FROM {CATALOG}.silver.qualifying
        WHERE season = {season} AND round = {rnd} AND session_code = 'Q'""")
    q["position"] = pd.to_numeric(q["position"])
    q["best_ms"] = pd.to_numeric(q["best_ms"])
    return q if q["best_ms"].notna().sum() >= 10 else None


def names(ctx: Ctx) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = ctx.q(f"SELECT driver_id, code, full_name FROM {CATALOG}.silver.drivers")
    t = ctx.q(f"SELECT constructor_id, name, color_hex FROM {CATALOG}.silver.constructors")
    return d, t


# --------------------------------------------------------------------------- forecast


def forecast_next(ctx: Ctx, ev_info: dict, today: date, sims: int):
    season, rnd = ev_info["season"], ev_info["round"]
    ev = load_event_inputs(season, rnd, today)
    hist = history(ctx, season)
    freq = frequency_tables(hist, season)

    practice = practice_rows(ctx, season, rnd)
    q = quali_result(ctx, season, rnd)
    grid = quali_pct = None
    if q is not None:
        qi = q.set_index("driver_id")
        grid = (
            qi["position"]
            .reindex(ev.drivers["driver_id"])
            .fillna(99)
            .rank(method="first")
            .to_numpy()
            .astype(int)
        )
        best = qi["best_ms"].astype(float)
        quali_pct = 100 * np.log(best / best.median())

    wf = simulate_weekend(
        ev, sims, practice=practice, grid=grid, quali_pct=quali_pct, grid_table=freq["grid"]
    )
    pred = wf.pred
    for stage, weights in QUALI_BLEND.items():
        m = pred["stage"] == stage
        if not m.any():
            continue
        priors = quali_priors(hist, freq, season, rnd, pred.loc[m, "driver_id"].tolist())
        pred.loc[m, list(QUALI_COLS)] = blend_many(
            pred.loc[m].reset_index(drop=True), priors, weights, QUALI_COLS
        )[list(QUALI_COLS)].to_numpy()
    return ev, wf, pred


def strategies(race) -> dict[str, list[dict]]:
    s = race.strategy_summary().sort_values("share", ascending=False)
    out: dict[str, list[dict]] = {}
    for d, g in s.groupby("driver_id", sort=False):
        out[d] = [
            {"strategy": r.strategy, "share": round(float(r.share), 3)}
            for r in g.head(TOP_STRATEGIES).itertuples()
        ]
    return out


# --------------------------------------------------------------------------- output


def _clean(v):
    if isinstance(v, float | np.floating):
        return None if not math.isfinite(float(v)) else round(float(v), 4)
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.ndarray):
        return [_clean(x) for x in v.tolist()]
    if isinstance(v, list):
        return [_clean(x) for x in v]
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    return v


def records(df: pd.DataFrame, cols: list[str]) -> list[dict]:
    keep = [c for c in cols if c in df.columns]
    return [_clean(r) for r in df[keep].to_dict("records")]


def with_names(df: pd.DataFrame, dn: pd.DataFrame, tn: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "driver_id" in out:
        out = out.merge(dn, on="driver_id", how="left")
    if "constructor_id" in out:
        out = out.merge(
            tn.rename(columns={"name": "team_name", "color_hex": "team_color"}),
            on="constructor_id",
            how="left",
        )
    return out


def accuracy(ctx: Ctx) -> list[dict]:
    a = ctx.q(f"""
        SELECT season, stage, method, metric, value, n_events FROM {CATALOG}.gold.backtest_results
        WHERE model_version = '{MODEL_VERSION}'""")
    a["value"] = pd.to_numeric(a["value"])
    a["season"] = pd.to_numeric(a["season"])
    a["n_events"] = pd.to_numeric(a["n_events"])
    return records(a, ["season", "stage", "method", "metric", "value", "n_events"])


DRIVER_COLS = [
    "driver_id",
    "code",
    "full_name",
    "constructor_id",
    "team_name",
    "team_color",
    "p_pole",
    "p_top3_grid",
    "expected_grid",
    "p_win",
    "p_podium",
    "p_points",
    "p_dnf",
    "expected_position",
    "expected_points",
]


def run(today: date, sims: int, out: Path, *, season_sims: int | None = None) -> dict:
    from databricks.sdk import WorkspaceClient

    t0 = time.monotonic()
    w = WorkspaceClient()
    ctx = Ctx(w, next(iter(w.warehouses.list())).id)
    created = datetime.now(UTC).replace(tzinfo=None)
    info = next_event(ctx, today)
    log.info("next event: %s %d/%d", info["event_name"], info["season"], info["round"])
    dn, tn = names(ctx)

    ev, wf, pred = forecast_next(ctx, info, today, sims)
    stages = [s for s in STAGE_LABELS if s in set(pred["stage"])]
    latest = stages[-1]
    named = with_names(pred, dn, tn)

    next_race = {
        "event": {k: _clean(v) for k, v in info.items()},
        "speed_profile": ev.circuit.get("speed_profile"),
        "laps": _clean(ev.circuit.get("laps")),
        "stages": [{"stage": s, "label": STAGE_LABELS[s]} for s in stages],
        "latest_stage": latest,
        "forecasts": {
            s: records(
                named[named["stage"] == s].sort_values("p_win", ascending=False), DRIVER_COLS
            )
            for s in stages
        },
        "strategies": strategies(wf.races[latest]),
    }

    log.info("season simulation")
    sr = simulate_season(info["season"], today, sims=season_sims or sims)
    season = {
        "season": info["season"],
        "drivers": records(
            with_names(sr.drivers, dn, tn.iloc[0:0]),
            [
                "driver_id",
                "code",
                "full_name",
                "current_points",
                "expected_points",
                "points_p05",
                "points_p95",
                "p_title",
                "p_top3",
                "expected_rank",
            ],
        ),
        "constructors": records(
            with_names(sr.constructors, dn.iloc[0:0], tn),
            [
                "constructor_id",
                "team_name",
                "team_color",
                "current_points",
                "expected_points",
                "points_p05",
                "points_p95",
                "p_title",
                "p_top3",
                "expected_rank",
            ],
        ),
        "remaining_events": [
            {"round": e["round"], "event_name": e["event_name"]} for e in sr.events
        ],
    }
    team_of = ev.drivers.set_index("driver_id")["constructor_id"]
    for d in season["drivers"]:
        d["constructor_id"] = team_of.get(d["driver_id"])

    meta = {
        "generated_at": created.isoformat(timespec="seconds") + "Z",
        "today": str(today),
        "model_version": MODEL_VERSION,
        "sims": sims,
        "data_cutoff": str(today),
        "event": f"{info['season']} round {info['round']}: {info['event_name']}",
        "latest_stage": latest,
    }
    out.mkdir(parents=True, exist_ok=True)
    for name, obj in (
        ("next_race", next_race),
        ("season", season),
        ("accuracy", accuracy(ctx)),
        ("meta", meta),
    ):
        (out / f"{name}.json").write_text(json.dumps(obj, indent=1), encoding="utf-8")

    # versioned record of what we predicted, when
    p = pred.copy()
    p["season"], p["round"], p["data_cutoff"] = info["season"], info["round"], today
    p["circuit_id"] = info["circuit_id"]
    p["pass_k"], p["slipstream"] = wf.pass_k, wf.slipstream
    p["snapshot_id"] = (
        "live:" + p["stage"] + f":{info['season']}_{info['round']:02d}:{today}:{MODEL_VERSION}"
    )
    p["model_version"], p["created_at"] = MODEL_VERSION, created
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
        *[c for c in DRIVER_COLS if c.startswith(("p_", "expected_"))],
        "p_front_row",
        "p_top10",
        "pass_k",
        "slipstream",
    ]
    p = p[[c for c in dict.fromkeys(keep) if c in p.columns]]
    store(ctx, p, "predictions", ["snapshot_id", "driver_id"], f"live_{today}")
    meta["seconds"] = round(time.monotonic() - t0)
    return meta


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--today", type=date.fromisoformat, default=datetime.now(UTC).date())
    p.add_argument("--sims", type=int, default=10_000)
    p.add_argument("--season-sims", type=int, default=None)
    p.add_argument("--out", type=Path, default=Path("site_data"))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    meta = run(args.today, args.sims, args.out, season_sims=args.season_sims)
    print(json.dumps(meta, indent=1))


if __name__ == "__main__":
    main()
