"""Everything the simulator knows about one event, as of a cutoff date (no look-ahead).

``load_event_inputs(season, round, as_of)`` reads the gold layer through the SQL warehouse
and returns an :class:`EventInputs`. Only data from races before ``as_of`` is used; driver
traits come from the latest fit with ``data_cutoff <= as_of``.

Pace convention everywhere: **% of lap time, lower = faster**.

Car vs driver: a team's measured gap includes its drivers, so

    car_gap      = team gap - mean skill of the drivers who produced it
    driver pace  = car_gap + driver skill

which lets the simulator handle line-up changes and teammates of different strength.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from f1_forecast.sim.circuit_params import event_rates, overtaking, pit_loss, tyre_params

CATALOG = "f1_forecast"
TRAITS = [
    "quali_pace",
    "race_pace",
    "consistency",
    "tyre_wear",
    "starts",
    "error_rate",
    "wet_skill",
]

# Population defaults for drivers with no fitted value yet (rookies): prior mean and spread.
TRAIT_PRIORS = {
    "quali_pace": (0.0, 0.30),
    "race_pace": (0.0, 0.30),
    "consistency": (0.0, 15.0),
    "tyre_wear": (0.0, 0.05),
    "starts": (0.0, 0.5),
    "error_rate": (0.07, 0.03),
    "wet_skill": (0.0, 0.25),
}
# Uncertainty added per season a trait estimate is out of date (career random-walk scale).
TRAIT_DRIFT = {"quali_pace": 0.13, "race_pace": 0.13, "wet_skill": 0.1, "starts": 0.2}
TEAM_FORM_SD = 0.45  # % lap time: event-to-event team pace variation (measured 0.3-0.6)
EWMA_HALF_LIFE = 5.0  # races


@dataclass
class EventInputs:
    season: int
    round: int
    as_of: date
    circuit: dict[str, Any]
    drivers: pd.DataFrame  # one row per driver: team, traits (value + se), pace components
    teams: pd.DataFrame  # one row per team: car gaps, reliability, pit stops, form sd
    tyres: pd.DataFrame  # one row per compound
    meta: dict[str, Any] = field(default_factory=dict)


def _q(w, wh, sql: str) -> pd.DataFrame:
    from f1_forecast.features.run_traits import query_df

    return query_df(w, wh, sql)


def ewma(values: np.ndarray, half_life: float = EWMA_HALF_LIFE) -> tuple[float, float]:
    """Recency-weighted mean of a series (oldest first) and its effective sample size."""
    v = np.asarray(values, float)
    v = v[~np.isnan(v)]
    if len(v) == 0:
        return float("nan"), 0.0
    w = 0.5 ** (np.arange(len(v))[::-1] / half_life)
    return float((w * v).sum() / w.sum()), float(w.sum() ** 2 / (w**2).sum())


def latest_trait_values(traits: pd.DataFrame, drivers: list[str], season: int) -> pd.DataFrame:
    """Per driver and trait: value/se at ``season``, else the most recent earlier season with
    the se widened for each season of drift, else the population prior."""
    rows = []
    for d in drivers:
        for t in TRAITS:
            sub = traits[
                (traits["driver_id"] == d)
                & (traits["trait"] == t)
                & (traits["as_of_season"] <= season)
            ]
            if len(sub):
                last = sub.loc[sub["as_of_season"].idxmax()]
                gap = season - int(last["as_of_season"])
                se = float(np.hypot(last["std_err"], TRAIT_DRIFT.get(t, 0) * np.sqrt(gap)))
                rows.append(
                    (d, t, float(last["value"]), se, "fitted" if gap == 0 else f"carried_{gap}y")
                )
            else:
                mean, sd = TRAIT_PRIORS[t]
                rows.append((d, t, mean, sd, "prior"))
    long = pd.DataFrame(rows, columns=["driver_id", "trait", "value", "se", "source"])
    wide = long.pivot(index="driver_id", columns="trait", values=["value", "se"])
    wide.columns = [f"{t}{'' if k == 'value' else '_se'}" for k, t in wide.columns]
    src = long[long["trait"] == "race_pace"].set_index("driver_id")["source"].rename("trait_source")
    return wide.join(src).reset_index()


def car_gaps(team_events: pd.DataFrame, lineup_skill: pd.DataFrame) -> pd.DataFrame:
    """Per team: recency-weighted quali/race gap to the best team, minus the mean skill of the
    drivers who set those gaps, i.e. the car's own pace (% lap, lower = faster)."""
    out = []
    for team, g in team_events.sort_values(["season", "round"]).groupby("constructor_id"):
        q, q_n = ewma(g["quali_gap_pct"].to_numpy(float))
        r, r_n = ewma(g["race_gap_pct"].to_numpy(float))
        out.append((team, q, q_n, r, r_n, len(g)))
    cars = pd.DataFrame(
        out, columns=["constructor_id", "quali_gap", "quali_n", "race_gap", "race_n", "events"]
    )
    cars = cars.merge(lineup_skill, on="constructor_id", how="left").fillna(
        {"mean_quali_skill": 0.0, "mean_race_skill": 0.0}
    )
    cars["car_quali"] = cars["quali_gap"] - cars["mean_quali_skill"]
    cars["car_race"] = cars["race_gap"] - cars["mean_race_skill"]
    # uncertainty of the car estimate: event-to-event spread over the effective sample size
    cars["car_quali_se"] = TEAM_FORM_SD / np.sqrt(cars["quali_n"].clip(lower=1))
    cars["car_race_se"] = TEAM_FORM_SD / np.sqrt(cars["race_n"].clip(lower=1))
    return cars


def sane_compound_pace(tyres: pd.DataFrame, global_offsets: pd.DataFrame) -> pd.DataFrame:
    """Fresh-tyre pace must order Soft < Medium < Hard (faster = lower). Circuits with too
    little data can violate that (e.g. Lusail); then the global medians are used instead."""
    t = tyres.set_index("compound")
    g = global_offsets.set_index("compound")["global_offset"].astype(float)
    off = t["fresh_pace_vs_medium_pct"]
    soft, hard = off.get("SOFT", np.nan), off.get("HARD", np.nan)
    if not (soft < 0 < hard):
        t["fresh_pace_vs_medium_pct"] = g.reindex(t.index).fillna(0.0)
        t.loc[t.index == "MEDIUM", "fresh_pace_vs_medium_pct"] = 0.0
        t["pace_source"] = "global"
    else:
        t["pace_source"] = "circuit"
    return t.reset_index()


def load_event_inputs(season: int, rnd: int, as_of: date | None = None) -> EventInputs:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    wh = next(iter(w.warehouses.list())).id
    cat = CATALOG

    ev = _q(
        w,
        wh,
        f"""
        SELECT e.season, e.round, e.event_name, e.circuit_id, e.race_date, e.is_sprint,
               c.race_laps, c.length_km, c.speed_profile
        FROM {cat}.silver.events e LEFT JOIN {cat}.silver.circuits c USING (circuit_id)
        WHERE e.season = {season} AND e.round = {rnd}""",
    )
    if ev.empty:
        raise ValueError(f"No event {season}/{rnd}")
    ev = ev.iloc[0]
    race_date = date.fromisoformat(str(ev["race_date"])[:10])
    as_of = as_of or race_date - timedelta(days=3)  # before the weekend starts
    cid = ev["circuit_id"]

    # Line-up: the event's own entry list if known (backtests), else the latest race's.
    entries = _q(
        w,
        wh,
        f"""
        SELECT driver_id, constructor_id, car_number FROM {cat}.silver.entries
        WHERE season = {season} AND round = {rnd} AND is_race_entry""",
    )
    if entries.empty:
        entries = _q(
            w,
            wh,
            f"""
            SELECT driver_id, constructor_id, car_number FROM {cat}.silver.entries
            WHERE is_race_entry AND season * 100 + round = (
              SELECT max(season * 100 + round) FROM {cat}.silver.events
              WHERE status = 'completed' AND race_date < DATE'{as_of}')""",
        )
    entries = entries.astype({"car_number": int})

    traits = _q(
        w,
        wh,
        f"""
        SELECT trait, driver_id, as_of_season, value, std_err
        FROM {cat}.gold.driver_traits t
        WHERE data_cutoff = (SELECT max(data_cutoff) FROM {cat}.gold.driver_traits x
                             WHERE x.trait = t.trait AND x.data_cutoff <= DATE'{as_of}')""",
    ).astype({"as_of_season": int, "value": float, "std_err": float})
    if traits.empty:
        raise ValueError(
            f"No driver-trait fit with data_cutoff <= {as_of}; run fit_features --as-of"
        )
    drivers = entries.merge(
        latest_trait_values(traits, entries["driver_id"].tolist(), season), on="driver_id"
    )

    # Team pace from this season's events before the cutoff (previous season if none yet).
    tep = _q(
        w,
        wh,
        f"""
        SELECT p.constructor_id, p.season, p.round, p.quali_gap_pct, p.race_gap_pct
        FROM {cat}.gold.team_event_pace p JOIN {cat}.silver.events e USING (season, round)
        WHERE e.race_date < DATE'{as_of}' AND p.season = (
          SELECT max(p2.season) FROM {cat}.gold.team_event_pace p2
          JOIN {cat}.silver.events e2 USING (season, round) WHERE e2.race_date < DATE'{as_of}')""",
    )
    tep = tep.astype({"season": int, "round": int, "quali_gap_pct": float, "race_gap_pct": float})
    pace_season = int(tep["season"].max())
    lineup = (
        drivers.groupby("constructor_id")
        .agg(mean_quali_skill=("quali_pace", "mean"), mean_race_skill=("race_pace", "mean"))
        .reset_index()
    )
    teams = car_gaps(tep, lineup)
    if pace_season < season:  # new season: last year's order, much less certain
        teams["car_quali_se"] = np.hypot(teams["car_quali_se"], 0.6)
        teams["car_race_se"] = np.hypot(teams["car_race_se"], 0.6)

    prof = _q(
        w,
        wh,
        f"""
        SELECT p.constructor_id, p.reliability_dnf_rate, p.pit_stationary_s, p.high_speed_track_idx,
               p.medium_speed_track_idx, p.low_speed_track_idx
        FROM {cat}.gold.car_profiles p
        JOIN {cat}.silver.events e ON e.season = p.season AND e.round = p.as_of_round
        WHERE p.season = {pace_season} AND e.race_date < DATE'{as_of}'
        QUALIFY row_number() OVER (
          PARTITION BY p.constructor_id ORDER BY p.as_of_round DESC) = 1""",
    )
    for col in prof.columns[1:]:
        prof[col] = pd.to_numeric(prof[col])
    teams = teams.merge(prof, on="constructor_id", how="left")
    # Teams new to the grid (or renamed): field-average car with wide uncertainty.
    missing = sorted(set(entries["constructor_id"]) - set(teams["constructor_id"]))
    if missing:
        filler = pd.DataFrame({"constructor_id": missing})
        filler["car_quali"] = teams["car_quali"].mean()
        filler["car_race"] = teams["car_race"].mean()
        filler["car_quali_se"] = filler["car_race_se"] = 1.0
        teams = pd.concat([teams, filler], ignore_index=True)
    teams = teams[teams["constructor_id"].isin(entries["constructor_id"])].reset_index(drop=True)
    teams["reliability_dnf_rate"] = teams["reliability_dnf_rate"].fillna(0.08)
    teams["form_sd"] = TEAM_FORM_SD

    # Circuit parameters from per-race tables, only races before the cutoff (no leakage).
    before = f"e.race_date < DATE'{as_of}'"
    ref_season = int(season)
    neutral = _q(
        w,
        wh,
        f"""
        SELECT n.season, n.circuit_id, n.had_sc, n.had_vsc, n.had_red, n.had_lap1_incident
        FROM {cat}.gold.race_neutralisations n
        JOIN {cat}.silver.events e ON e.season = n.season AND e.round = n.round
        WHERE {before}""",
    )
    for col in ("had_sc", "had_vsc", "had_red", "had_lap1_incident"):
        neutral[col] = neutral[col].map({"true": 1.0, "false": 0.0, True: 1.0, False: 0.0})
    rates = event_rates(neutral, cid, ref_season)
    overtakes = _q(
        w,
        wh,
        f"""
        SELECT circuit_id, passes, green_laps FROM {cat}.gold.race_overtakes e WHERE {before}""",
    )
    overtakes[["passes", "green_laps"]] = overtakes[["passes", "green_laps"]].astype(float)
    passes, difficulty = overtaking(overtakes, cid)
    stops = _q(
        w,
        wh,
        f"""
        SELECT circuit_id, loss_s FROM {cat}.gold.green_pit_stops e
        WHERE circuit_id = '{cid}' AND {before}""",
    )
    stints = _q(
        w,
        wh,
        f"""
        SELECT t.season, t.circuit_id, t.speed_profile, t.compound, t.wear_pct_per_lap,
               t.median_lap_ms, t.stint_laps, t.track_temp_c
        FROM {cat}.gold.tyre_stints t
        JOIN {cat}.silver.events e ON e.season = t.season AND e.round = t.round
        WHERE {before}""",
    )
    for col in ("season", "wear_pct_per_lap", "median_lap_ms", "stint_laps", "track_temp_c"):
        stints[col] = pd.to_numeric(stints[col])
    offsets = _q(
        w,
        wh,
        f"""
        SELECT circuit_id, compound, fresh_vs_medium_pct
        FROM {cat}.gold.tyre_compound_offsets e WHERE {before}""",
    ).astype({"fresh_vs_medium_pct": float})
    tyres = tyre_params(stints, offsets, cid, ev["speed_profile"], ref_season)
    global_offsets = (
        offsets.groupby("compound")["fresh_vs_medium_pct"]
        .median()
        .rename("global_offset")
        .reset_index()
    )
    tyres = sane_compound_pace(tyres, global_offsets)
    c = {
        **rates,
        "pit_loss_s": pit_loss(stops, cid),
        "passes_per_green_lap": passes,
        "overtaking_difficulty": difficulty,
    }
    temp = _q(
        w,
        wh,
        f"""
        SELECT percentile(w.track_temp_c, 0.5) AS track_temp_c
        FROM {cat}.silver.session_weather w JOIN {cat}.silver.sessions s USING (session_id)
        JOIN {cat}.silver.events e ON e.season = s.season AND e.round = s.round
        WHERE e.circuit_id = '{cid}' AND s.session_code = 'R' AND e.race_date < DATE'{as_of}'""",
    )

    circuit = {
        "circuit_id": cid,
        "event_name": ev["event_name"],
        "speed_profile": ev["speed_profile"],
        "laps": int(ev["race_laps"]) if pd.notna(ev["race_laps"]) else 60,
        "base_lap_ms": float(tyres["typical_lap_ms"].median()),
        "track_temp_c": float(temp.iloc[0, 0]) if len(temp) and pd.notna(temp.iloc[0, 0]) else 35.0,
        **{k: float(v) if v is not None and pd.notna(v) else None for k, v in c.items()},
    }
    circuit["pit_loss_s"] = circuit.get("pit_loss_s") or 22.0
    return EventInputs(
        season=season,
        round=rnd,
        as_of=as_of,
        circuit=circuit,
        drivers=drivers,
        teams=teams,
        tyres=tyres,
        meta={"pace_season": pace_season, "race_date": str(race_date)},
    )
