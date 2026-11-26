"""Open-Meteo historical weather per circuit (hourly, 2001 → last settled month).

Circuits come from the landed Jolpica dump (every circuit raced since 2001) plus the manual
2027 calendar. Each circuit is downloaded in monthly chunks; a chunk is only requested once
its month ended ``archive_settle_days`` ago, so landed files never change.

Open-Meteo weights requests by size: cost = (days / 14) × (variables / 10). The free tier
allows ~10,000 units/day, so each run spends at most ``--max-cost`` units and the daily
schedule continues the next day. Circuits on the current/next calendar go first.

Landing layout::

    openmeteo/archive/<circuit_ref>/<YYYY-MM>.parquet    hourly rows, values as text

Run ``python -m f1_forecast.ingest.openmeteo --help``.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from f1_forecast.config import CONFIG_DIR, load_source_defaults, load_sources
from f1_forecast.ingest.http import CachedClient, Store, client_for
from f1_forecast.ingest.jolpica import latest_dump_folder, read_dump_csv
from f1_forecast.ingest.openf1 import records_to_parquet
from f1_forecast.ingest.stores import make_store

log = logging.getLogger(__name__)

SOURCE = "openmeteo"  # landing folder name
CONFIG_KEY = "open_meteo"  # key in configs/sources.yaml


@dataclass(frozen=True)
class Circuit:
    ref: str
    latitude: float
    longitude: float
    priority: bool  # on the current or next calendar


def request_cost(days: int, n_vars: int) -> float:
    """Open-Meteo units: (days / 14, at least 1) x (variables / 10), at least 1 overall.
    Matches the X-RateLimit-Cost header (archive 366 d x 11 vars = 28.8; ensemble 3 d x
    2 vars x 51 members = 10.2; forecast 3 d x 9 vars = 1)."""
    return max(1.0, max(1.0, days / 14) * n_vars / 10)


def settled_months(start: date, today: date, settle_days: int) -> list[tuple[int, int]]:
    """(year, month) from ``start`` to the last month that ended ``settle_days`` ago."""
    months = []
    y, m = start.year, start.month
    while True:
        last_day = date(y, m, monthrange(y, m)[1])
        if last_day + timedelta(days=settle_days) > today:
            return months
        months.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def load_circuits(store: Store, today: date, first_season: int) -> list[Circuit]:
    """Circuits raced since ``first_season`` plus the 2027 calendar; calendar ones first."""
    folder = latest_dump_folder(store)
    years = {s["id"]: int(s["year"]) for s in read_dump_csv(store, folder, "season")}
    circuits = {c["id"]: c for c in read_dump_csv(store, folder, "circuit")}
    raced: dict[str, int] = {}  # circuit ref -> last season raced
    for r in read_dump_csv(store, folder, "round"):
        year = years.get(r["season_id"], 0)
        if year >= first_season and r["circuit_id"] in circuits:
            ref = circuits[r["circuit_id"]]["reference"]
            raced[ref] = max(raced.get(ref, 0), year)

    with open(CONFIG_DIR / "calendar_2027.csv", encoding="utf-8") as f:
        upcoming = {row["circuit_ref"] for row in csv.DictReader(f)}
    current = {ref for ref, last in raced.items() if last >= today.year - 1}
    by_ref = {c["reference"]: c for c in circuits.values()}
    missing = upcoming - by_ref.keys()
    if missing:
        raise ValueError(f"calendar_2027.csv circuits not in Jolpica: {sorted(missing)}")

    out = [
        Circuit(
            ref,
            float(by_ref[ref]["latitude"]),
            float(by_ref[ref]["longitude"]),
            priority=ref in upcoming or ref in current,
        )
        for ref in sorted(raced.keys() | upcoming)
    ]
    return sorted(out, key=lambda c: (not c.priority, c.ref))


def columnar_to_records(payload: dict[str, Any], circuit: Circuit) -> list[dict[str, Any]]:
    hourly = payload["hourly"]
    names = [k for k in hourly if k != "time"]
    meta = {
        "circuit_ref": circuit.ref,
        "latitude": payload.get("latitude"),
        "longitude": payload.get("longitude"),
        "elevation": payload.get("elevation"),
    }
    return [
        {**meta, "time": t, **{n: hourly[n][i] for n in names}}
        for i, t in enumerate(hourly["time"])
    ]


def run(
    store: Store,
    *,
    today: date | None = None,
    max_cost: float = 8000,
    circuits_filter: set[str] | None = None,
) -> dict[str, Any]:
    today = today or datetime.now(UTC).date()
    sources, defaults = load_sources(), load_source_defaults()
    cfg = sources[CONFIG_KEY]
    client: CachedClient = client_for(CONFIG_KEY, store, sources, defaults)
    variables = cfg["archive_hourly"]
    start = date.fromisoformat(cfg["archive_start"])
    months = settled_months(start, today, int(cfg["archive_settle_days"]))

    circuits = load_circuits(store, today, start.year)
    if circuits_filter:
        circuits = [c for c in circuits if c.ref in circuits_filter]

    spent, landed, out_of_budget = 0.0, 0, False
    complete = 0
    for c in circuits:
        have = set(store.list_names(f"{SOURCE}/archive/{c.ref}"))
        todo = [(y, m) for y, m in months if f"{y}-{m:02d}.parquet" not in have]
        if not todo:
            complete += 1
            continue
        log.info("%s: %d months to fetch (priority=%s)", c.ref, len(todo), c.priority)
        for y, m in todo:
            days = monthrange(y, m)[1]
            cost = request_cost(days, len(variables))
            if spent + cost > max_cost:
                out_of_budget = True
                break
            params = {
                "latitude": c.latitude,
                "longitude": c.longitude,
                "start_date": f"{y}-{m:02d}-01",
                "end_date": f"{y}-{m:02d}-{days:02d}",
                "hourly": ",".join(variables),
                "timezone": "UTC",
            }
            payload = json.loads(client.fetch(cfg["endpoints"]["archive"], params, cost=cost))
            records = columnar_to_records(payload, c)
            store.write(
                f"{SOURCE}/archive/{c.ref}/{y}-{m:02d}.parquet", records_to_parquet(records)
            )
            spent += cost
            landed += 1
        else:
            complete += 1
        if out_of_budget:
            log.info("cost budget of %.0f units used; continuing next run", max_cost)
            break

    return {
        "circuits": len(circuits),
        "circuits_complete": complete,
        "months_per_circuit": len(months),
        "months_landed_now": landed,
        "cost_units_spent": round(spent, 1),
        "stopped_on_budget": out_of_budget,
        "requests": client.stats.get("requests", 0),
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Land Open-Meteo hourly history per circuit.")
    p.add_argument("--store", choices=["local", "volume"], default="local")
    p.add_argument("--location", default="data/landing")
    p.add_argument("--max-cost", type=float, default=8000, help="Open-Meteo units per run")
    p.add_argument("--circuits", nargs="+", help="Only these circuit refs (samples/tests)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    summary = run(
        make_store(args.store, args.location),
        max_cost=args.max_cost,
        circuits_filter=set(args.circuits) if args.circuits else None,
    )
    print(json.dumps(summary, indent=2))
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"landed={summary['months_landed_now']}\n")


if __name__ == "__main__":
    main()
