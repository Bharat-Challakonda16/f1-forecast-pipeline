"""Open-Meteo weather forecasts for race weekends: live snapshots and past forecasts.

Two kinds of data, both for the hours of each race weekend (first session day → race day):

**Live snapshots** (daily, races within ``forecast_horizon_days``): the hourly forecast as it
stands at download time, plus the ECMWF ensemble (51 members) for rain and temperature. Each
download is a new file, so the forecast's evolution is kept::

    openmeteo/forecast/<season>_<round>/<issued YYYY-MM-DDTHH>.parquet   one row per hour
    openmeteo/ensemble/<season>_<round>/<issued YYYY-MM-DDTHH>.parquet   one row per hour x member

**Past forecasts** (Previous Runs API, from early 2024): for every hour, what the forecast
issued 1..7 days earlier said (``lead_days``; 0 = the latest run, close to what happened).
This is what makes an honest backtest of the weather model possible. A race is fetched once,
after it has happened, so the files never change::

    openmeteo/previous_runs/<season>_<round>.parquet    one row per hour x lead_days

Rows are reshaped to long format (one value column per variable) but values are as received.

Run ``python -m f1_forecast.ingest.weather_forecast --help``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from f1_forecast.config import load_source_defaults, load_sources
from f1_forecast.ingest.http import CachedClient, Store, client_for
from f1_forecast.ingest.openf1 import records_to_parquet
from f1_forecast.ingest.openmeteo import request_cost
from f1_forecast.ingest.stores import make_store

log = logging.getLogger(__name__)

SOURCE = "openmeteo"
CONFIG_KEY = "open_meteo"
CATALOG = "f1_forecast"

FORECAST_VARS = [
    "precipitation",
    "rain",
    "precipitation_probability",
    "temperature_2m",
    "relative_humidity_2m",
    "cloud_cover",
    "shortwave_radiation",
    "wind_speed_10m",
    "weather_code",
]
ENSEMBLE_VARS = ["precipitation", "temperature_2m"]
ENSEMBLE_MODEL = "ecmwf_ifs025"
# Previous Runs has no precipitation probability; these are the variables kept per lead.
PAST_VARS = [
    "precipitation",
    "temperature_2m",
    "relative_humidity_2m",
    "cloud_cover",
    "shortwave_radiation",
    "wind_speed_10m",
]
PAST_LEADS = range(0, 8)  # 0 = latest run; Previous Runs keeps 1..7 days before
PAST_START = date(2024, 3, 1)  # Previous Runs coverage starts in early 2024


@dataclass(frozen=True)
class Weekend:
    season: int
    round: int
    circuit_id: str
    latitude: float
    longitude: float
    first_day: date  # day of the first session (UTC)
    race_date: date

    @property
    def key(self) -> str:
        return f"{self.season}_{self.round:02d}"

    def meta(self) -> dict[str, Any]:
        return {"season": self.season, "round": self.round, "circuit_ref": self.circuit_id}


def load_weekends(first: date, last: date) -> list[Weekend]:
    """Race weekends with race day in [first, last], from silver (sessions give the days)."""
    from databricks.sdk import WorkspaceClient

    from f1_forecast.features.run_traits import query_df

    w = WorkspaceClient()
    wh = next(iter(w.warehouses.list())).id
    df = query_df(
        w,
        wh,
        f"""
        SELECT e.season, e.round, e.circuit_id, c.latitude, c.longitude, e.race_date,
               to_date(min(s.start_utc)) AS first_day
        FROM {CATALOG}.silver.events e
        JOIN {CATALOG}.silver.circuits c USING (circuit_id)
        JOIN {CATALOG}.silver.sessions s ON s.season = e.season AND s.round = e.round
        WHERE e.status <> 'cancelled'
          AND e.race_date BETWEEN DATE'{first}' AND DATE'{last}'
        GROUP BY ALL
        ORDER BY e.race_date""",
    )
    return [
        Weekend(
            int(r.season),
            int(r.round),
            r.circuit_id,
            float(r.latitude),
            float(r.longitude),
            date.fromisoformat(str(r.first_day)[:10]),
            date.fromisoformat(str(r.race_date)[:10]),
        )
        for r in df.itertuples()
    ]


def _base_params(wk: Weekend, start: date | None = None) -> dict[str, Any]:
    return {
        "latitude": wk.latitude,
        "longitude": wk.longitude,
        "start_date": str(start or wk.first_day),
        "end_date": str(wk.race_date),
        "timezone": "UTC",
    }


def forecast_records(payload: dict[str, Any], meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Columnar hourly forecast → one record per hour."""
    h = payload["hourly"]
    names = [k for k in h if k != "time"]
    return [{**meta, "time": t, **{n: h[n][i] for n in names}} for i, t in enumerate(h["time"])]


def ensemble_records(payload: dict[str, Any], meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Ensemble columns ``<var>`` (control) and ``<var>_memberNN`` → one record per hour x
    member (member 0 = control run)."""
    h = payload["hourly"]
    members: dict[int, dict[str, str]] = {}
    for col in h:
        if col == "time":
            continue
        var, _, m = col.partition("_member")
        members.setdefault(int(m) if m else 0, {})[var] = col
    return [
        {**meta, "time": t, "member": m, **{v: h[c][i] for v, c in cols.items()}}
        for i, t in enumerate(h["time"])
        for m, cols in sorted(members.items())
    ]


def past_columns() -> list[str]:
    return [v if lead == 0 else f"{v}_previous_day{lead}" for lead in PAST_LEADS for v in PAST_VARS]


def past_records(payload: dict[str, Any], meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Previous Runs columns ``<var>_previous_dayN`` → one record per hour x lead_days.
    Leads with no values at all (outside the archive) are dropped."""
    h = payload["hourly"]
    out = []
    for lead in PAST_LEADS:
        cols = {v: v if lead == 0 else f"{v}_previous_day{lead}" for v in PAST_VARS}
        if all(x is None for c in cols.values() for x in h.get(c, [])):
            continue
        values = {v: h.get(c) or [None] * len(h["time"]) for v, c in cols.items()}
        out += [
            {**meta, "lead_days": lead, "time": t, **{v: values[v][i] for v in PAST_VARS}}
            for i, t in enumerate(h["time"])
        ]
    return out


def _cost(wk: Weekend, n_vars: int, start: date | None = None) -> float:
    days = (wk.race_date - (start or wk.first_day)).days + 1
    return request_cost(days, n_vars)


def land_live(
    store: Store, client: CachedClient, cfg: dict, weekends: list[Weekend], now: datetime
) -> int:
    """One forecast + ensemble snapshot per weekend for this hour (skipped if already landed)."""
    issued = now.strftime("%Y-%m-%dT%H")
    landed = 0
    for wk in weekends:
        start = max(wk.first_day, now.date())
        if start > wk.race_date:
            continue
        meta = {**wk.meta(), "issued_utc": now.strftime("%Y-%m-%dT%H:%M")}
        key = f"{SOURCE}/forecast/{wk.key}/{issued}.parquet"
        if not store.exists(key):
            params = {**_base_params(wk, start), "hourly": ",".join(FORECAST_VARS)}
            payload = json.loads(
                client.fetch(
                    cfg["endpoints"]["forecast"],
                    params,
                    cost=_cost(wk, len(FORECAST_VARS), start),
                )
            )
            store.write(key, records_to_parquet(forecast_records(payload, meta)))
            landed += 1
        key = f"{SOURCE}/ensemble/{wk.key}/{issued}.parquet"
        if not store.exists(key):
            params = {
                **_base_params(wk, start),
                "hourly": ",".join(ENSEMBLE_VARS),
                "models": ENSEMBLE_MODEL,
            }
            # the ensemble API counts every member as a variable
            cost = _cost(wk, len(ENSEMBLE_VARS) * 51, start)
            payload = json.loads(client.fetch(cfg["endpoints"]["ensemble"], params, cost=cost))
            store.write(key, records_to_parquet(ensemble_records(payload, meta)))
            landed += 1
    return landed


def land_past(
    store: Store, client: CachedClient, cfg: dict, weekends: list[Weekend], today: date
) -> int:
    """Past forecasts for weekends that are over (fetched once each)."""
    cols = past_columns()
    landed = 0
    for wk in weekends:
        if wk.race_date < PAST_START or wk.race_date >= today:
            continue
        key = f"{SOURCE}/previous_runs/{wk.key}.parquet"
        if store.exists(key):
            continue
        params = {**_base_params(wk), "hourly": ",".join(cols)}
        payload = json.loads(
            client.fetch(cfg["endpoints"]["previous_runs"], params, cost=_cost(wk, len(cols)))
        )
        records = past_records(payload, wk.meta())
        if not records:
            log.warning("%s: no past forecasts returned", wk.key)
            continue
        store.write(key, records_to_parquet(records))
        landed += 1
    return landed


def run(store: Store, *, now: datetime | None = None, past: bool = True) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    today = now.date()
    sources, defaults = load_sources(), load_source_defaults()
    cfg = sources[CONFIG_KEY]
    client = client_for(CONFIG_KEY, store, sources, defaults)
    horizon = int(cfg["forecast_horizon_days"])
    upcoming = load_weekends(today, today + timedelta(days=horizon))
    live = land_live(store, client, cfg, upcoming, now)
    done = land_past(store, client, cfg, load_weekends(PAST_START, today), today) if past else 0
    return {
        "upcoming_weekends": [w.key for w in upcoming],
        "live_files_landed": live,
        "past_files_landed": done,
        "requests": client.stats.get("requests", 0),
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Land Open-Meteo race-weekend forecasts.")
    p.add_argument("--store", choices=["local", "volume"], default="local")
    p.add_argument("--location", default="data/landing")
    p.add_argument("--no-past", action="store_true", help="Skip the past-forecast backfill")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    summary = run(make_store(args.store, args.location), past=not args.no_past)
    print(json.dumps(summary, indent=2))
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"landed={summary['live_files_landed'] + summary['past_files_landed']}\n")


if __name__ == "__main__":
    main()
