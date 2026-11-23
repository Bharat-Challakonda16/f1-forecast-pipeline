"""Jolpica-F1 ingestion: bulk history from the CSV dump, recent rounds from the API.

Landing layout (relative to the store root)::

    jolpica/dump/<dump-date>/formula_one_<table>.csv   full database snapshot
    jolpica/dump/<dump-date>/_SUCCESS                  written last; holds the SHA-256
    jolpica/api/schedule/<season>/<YYYY-MM-DD>.json    daily calendar snapshot (it can change)
    jolpica/api/<season>/<round>/<endpoint>/offset=<n>.json

Run ``python -m f1_forecast.ingest.jolpica --help``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import logging
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from f1_forecast.config import load_source_defaults, load_sources
from f1_forecast.ingest.http import CachedClient, Store, client_for
from f1_forecast.ingest.stores import make_store

log = logging.getLogger(__name__)

SOURCE = "jolpica"
PAGE = 100
# Endpoints fetched for every completed round. "required" ones must be non-empty before we
# keep them; otherwise Jolpica has not published them yet and we try again next run.
ROUND_ENDPOINTS: dict[str, bool] = {
    "results": True,
    "qualifying": True,
    "laps": True,
    "sprint": False,
    "pitstops": False,
    "driverstandings": True,
    "constructorstandings": True,
}
# Results usually appear within a few hours of the chequered flag.
PUBLISH_GRACE = timedelta(hours=3)


@dataclass(frozen=True, order=True)
class RoundId:
    season: int
    round: int

    def __str__(self) -> str:
        return f"{self.season}/{self.round:02d}"


def mrdata_total(payload: dict[str, Any]) -> int:
    return int(payload["MRData"]["total"])


# --------------------------------------------------------------------------- dump


def ingest_dump(client: CachedClient, store: Store, index_url: str) -> str:
    """Land the latest free (delayed) CSV dump if we don't have it. Returns its folder key."""
    index = json.loads(client.fetch(index_url))
    meta = index["delayed_dumps"]["csv"]
    folder = f"{SOURCE}/dump/{meta['uploaded_at'][:10]}"
    marker = f"{folder}/_SUCCESS"
    if store.exists(marker):
        log.info("dump %s already landed", folder)
        return folder

    log.info("downloading dump %s (%.1f MB)", folder, meta["file_size"] / 1e6)
    blob = client.fetch(meta["download_url"])
    digest = hashlib.sha256(blob).hexdigest()
    if digest != meta["file_hash"]:
        raise ValueError(f"dump checksum mismatch: expected {meta['file_hash']}, got {digest}")
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        for name in zf.namelist():
            if name.endswith(".csv") and "/" not in name:
                store.write(f"{folder}/{name}", zf.read(name))
    store.write(marker, json.dumps({"sha256": digest, **meta}).encode())
    return folder


def _read_csv(store: Store, key: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(store.read(key).decode("utf-8"))))


def rounds_with_race_results(store: Store, folder: str) -> set[RoundId]:
    """Rounds whose race session already has classified entries in the dump."""
    years = {s["id"]: int(s["year"]) for s in _read_csv(store, f"{folder}/formula_one_season.csv")}
    rounds = {
        r["id"]: RoundId(years[r["season_id"]], int(r["number"]))
        for r in _read_csv(store, f"{folder}/formula_one_round.csv")
        if r["number"]  # non-championship / cancelled events have no round number
    }
    race_sessions = {
        s["id"]: s["round_id"]
        for s in _read_csv(store, f"{folder}/formula_one_session.csv")
        if s["type"] == "R"
    }
    have = set()
    for e in _read_csv(store, f"{folder}/formula_one_sessionentry.csv"):
        round_id = race_sessions.get(e["session_id"])
        if round_id and e["position"]:
            have.add(rounds[round_id])
    return have


# --------------------------------------------------------------------------- API


def fetch_schedule(client: CachedClient, base_url: str, season: int, today: date) -> list[dict]:
    """Snapshot of one season's calendar, stored once per day (calendars change)."""
    key = f"{SOURCE}/api/schedule/{season}/{today.isoformat()}.json"
    payload = client.get_json(f"{base_url}{season}.json", {"limit": PAGE}, key=key)
    return payload["MRData"]["RaceTable"]["Races"]


def race_start(race: dict[str, Any]) -> datetime:
    time_part = race.get("time", "12:00:00Z").replace("Z", "+00:00")
    return datetime.fromisoformat(f"{race['date']}T{time_part}")


def completed_rounds(races: Iterable[dict[str, Any]], now: datetime) -> list[RoundId]:
    return [
        RoundId(int(r["season"]), int(r["round"]))
        for r in races
        if race_start(r) + PUBLISH_GRACE <= now
    ]


def fetch_paged(
    client: CachedClient, base_url: str, rid: RoundId, endpoint: str, required: bool
) -> Iterator[dict[str, Any]]:
    """Yield every page of ``<season>/<round>/<endpoint>``; stops early if not published."""
    url = f"{base_url}{rid.season}/{rid.round}/{endpoint}.json"
    accept = (lambda p: mrdata_total(p) > 0) if required else None
    offset, total = 0, None
    while total is None or offset < total:
        key = f"{SOURCE}/api/{rid.season}/{rid.round:02d}/{endpoint}/offset={offset:05d}.json"
        page = client.get_json(url, {"limit": PAGE, "offset": offset}, key=key, accept=accept)
        if page is None:
            log.info("%s %s: not published yet", rid, endpoint)
            return
        total = mrdata_total(page)
        yield page
        offset += PAGE


def ingest_round(client: CachedClient, base_url: str, rid: RoundId) -> bool:
    """Land every endpoint for one round. False if race results are not out yet."""
    for endpoint, required in ROUND_ENDPOINTS.items():
        pages = sum(1 for _ in fetch_paged(client, base_url, rid, endpoint, required))
        if endpoint == "results" and pages == 0:
            return False
    return True


# --------------------------------------------------------------------------- orchestration


def run(store: Store, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    sources, defaults = load_sources(), load_source_defaults()
    cfg = sources[SOURCE]
    base_url = cfg["base_url"]
    client = client_for(SOURCE, store, sources, defaults)

    folder = ingest_dump(client, store, cfg["dumps"]["index_url"])
    in_dump = rounds_with_race_results(store, folder)
    latest_dump_season = max(r.season for r in in_dump)

    # The current season (rolling predictions) plus next season's calendar once it exists.
    seasons = sorted({latest_dump_season, now.year})
    missing: list[RoundId] = []
    for season in [*seasons, now.year + 1]:
        races = fetch_schedule(client, base_url, season, now.date())
        if season in seasons:
            missing += [r for r in completed_rounds(races, now) if r not in in_dump]

    landed, pending = [], []
    for rid in sorted(missing):
        (landed if ingest_round(client, base_url, rid) else pending).append(str(rid))

    summary = {
        "dump": folder,
        "rounds_in_dump": len(in_dump),
        "latest_in_dump": str(max(in_dump)),
        "api_rounds_landed": landed,
        "api_rounds_pending": pending,
        **client.stats,
    }
    log.info("jolpica summary: %s", summary)
    return summary


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Land Jolpica-F1 data (dump + recent API rounds).")
    p.add_argument("--store", choices=["local", "volume"], default="local")
    p.add_argument(
        "--location",
        default="data/landing",
        help="Local folder, or a Volume path like /Volumes/f1_forecast/bronze/landing",
    )
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(json.dumps(run(make_store(args.store, args.location)), indent=2))


if __name__ == "__main__":
    main()
