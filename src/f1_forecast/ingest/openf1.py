"""OpenF1 ingestion (2023+): every session's telemetry, positions, laps, tyres, weather, etc.

Each API response is landed as one Parquet file. Values are kept **exactly as received**:
every field is stored as its JSON text (numbers, booleans, lists all become strings, nulls
stay null). This keeps bronze faithful to the source and guarantees that files from different
sessions always share one schema, even when OpenF1 mixes types (e.g. ``"+1 LAP"`` in a
numeric gap column). Silver does the typing.

Landing layout (relative to the store root)::

    openf1/<endpoint>/<year>/<session_key>.parquet                 session-wide endpoints
    openf1/<endpoint>/<year>/<session_key>__driver=<NN>.parquet    car_data, location
    openf1/_index/sessions/<year>/<YYYY-MM-DD>.json                daily session-list snapshot
    openf1/_done/<session_key>.json                                written last: row counts

A session with a ``_done`` marker is never requested again. A run stops cleanly when its
time budget is used up; the next run continues where it stopped.

Run ``python -m f1_forecast.ingest.openf1 --help``.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from f1_forecast.config import load_source_defaults, load_sources
from f1_forecast.ingest.http import CachedClient, DownloadError, Store, client_for
from f1_forecast.ingest.stores import make_store

log = logging.getLogger(__name__)

SOURCE = "openf1"
SESSION_ENDPOINTS = [
    "drivers",
    "laps",
    "stints",
    "pit",
    "weather",
    "race_control",
    "intervals",
    "position",
    "session_result",
    "starting_grid",
    "overtakes",
]
DRIVER_ENDPOINTS = ["car_data", "location"]  # high-frequency: one request per driver
# A session counts as published only if these are non-empty.
REQUIRED = ("drivers", "laps")
# OpenF1 processes sessions shortly after they end; give it time before trusting them.
PUBLISH_GRACE = timedelta(hours=3)
# If a required endpoint is still empty this long after the session, OpenF1 simply doesn't
# have it (e.g. 2025 Baku qualifying laps). Land what exists instead of retrying forever.
GIVE_UP_AFTER = timedelta(days=7)


# --------------------------------------------------------------------------- conversion


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"))


def records_to_parquet(records: list[dict[str, Any]]) -> bytes:
    """Encode API records as Parquet with every column as nullable text (see module doc)."""
    columns: dict[str, None] = {}
    for rec in records:
        columns.update(dict.fromkeys(rec))
    table = pa.table(
        {c: pa.array([_as_text(r.get(c)) for r in records], type=pa.string()) for c in columns}
    )
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="zstd")
    return buf.getvalue()


def parquet_to_records(data: bytes) -> list[dict[str, Any]]:
    return pq.read_table(io.BytesIO(data)).to_pylist()


# --------------------------------------------------------------------------- sessions


def is_testing(session: dict[str, Any]) -> bool:
    return session["session_name"].startswith("Day")


def finished_sessions(
    sessions: list[dict[str, Any]], now: datetime, include_testing: bool = False
) -> list[dict[str, Any]]:
    """Sessions that ended long enough ago to be published, oldest first."""
    done = [
        s
        for s in sessions
        if not s.get("is_cancelled")
        and (include_testing or not is_testing(s))
        and datetime.fromisoformat(s["date_end"]) + PUBLISH_GRACE <= now
    ]
    return sorted(done, key=lambda s: s["date_start"])


@dataclass
class OpenF1:
    client: CachedClient
    store: Store
    base_url: str

    def fetch(self, endpoint: str, **params: Any) -> list[dict[str, Any]]:
        """GET one endpoint. OpenF1 answers 404 "No results found" for empty results."""
        try:
            data = json.loads(self.client.fetch(f"{self.base_url}{endpoint}", params))
        except DownloadError as exc:
            if exc.status == 404:
                return []
            raise
        if not isinstance(data, list):
            raise DownloadError(f"openf1 {endpoint}: unexpected response {str(data)[:200]}")
        return data

    def land(self, endpoint: str, session: dict[str, Any], suffix: str = "", **params: Any):
        """Land one response. Returns its records (from the store if landed earlier)."""
        path = f"{SOURCE}/{endpoint}/{session['year']}/{session['session_key']}{suffix}.parquet"
        if self.store.exists(path):
            return parquet_to_records(self.store.read(path))
        records = self.fetch(endpoint, session_key=session["session_key"], **params)
        if records:
            self.store.write(path, records_to_parquet(records))
        return records

    def ingest_session(
        self, session: dict[str, Any], require_complete: bool = True
    ) -> dict[str, int] | None:
        """Land every endpoint for one session. Row counts, or None if not published yet.

        With ``require_complete=False`` (old sessions), a missing required endpoint no longer
        blocks the session: whatever OpenF1 has is landed and the gap shows as a 0 count.
        """
        counts: dict[str, int] = {}
        drivers: list[dict[str, Any]] = []
        for endpoint in SESSION_ENDPOINTS:
            records = self.land(endpoint, session)
            counts[endpoint] = len(records)
            if endpoint in REQUIRED and not records and require_complete:
                log.info("%s: %s empty, not published yet", session["session_key"], endpoint)
                return None
            if endpoint == "drivers":
                drivers = records

        numbers = sorted(
            {int(d["driver_number"]) for d in drivers if d.get("driver_number") is not None}
        )
        for endpoint in DRIVER_ENDPOINTS:
            counts[endpoint] = sum(
                len(self.land(endpoint, session, f"__driver={n:02d}", driver_number=n))
                for n in numbers
            )
        return counts


# --------------------------------------------------------------------------- orchestration


def run(
    store: Store,
    *,
    now: datetime | None = None,
    first_year: int | None = None,
    max_minutes: float = 330,
    max_sessions: int | None = None,
    include_testing: bool = False,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    sources, defaults = load_sources(), load_source_defaults()
    cfg = sources[SOURCE]
    api = OpenF1(client_for(SOURCE, store, sources, defaults), store, cfg["base_url"])
    deadline = time.monotonic() + max_minutes * 60

    done = {n.removesuffix(".json") for n in store.list_names(f"{SOURCE}/_done")}
    todo: list[dict[str, Any]] = []
    for year in range(first_year or cfg["use_from_season"], now.year + 1):
        sessions = api.fetch("sessions", year=year)
        store.write(
            f"{SOURCE}/_index/sessions/{year}/{now.date().isoformat()}.json",
            json.dumps(sessions).encode(),
        )
        todo += [
            s
            for s in finished_sessions(sessions, now, include_testing)
            if str(s["session_key"]) not in done
        ]
    if max_sessions is not None:
        todo = todo[:max_sessions]

    log.info("%d sessions already landed, %d to do", len(done), len(todo))
    landed, pending = [], []
    for i, session in enumerate(todo, 1):
        if time.monotonic() > deadline:
            log.info("time budget used; continuing next run")
            break
        label = f"{session['year']} {session['location']} {session['session_name']}"
        t0 = time.monotonic()
        ended = datetime.fromisoformat(session["date_end"])
        counts = api.ingest_session(session, require_complete=now - ended < GIVE_UP_AFTER)
        if counts is None:
            pending.append(label)
            continue
        missing = [e for e in REQUIRED if counts.get(e) == 0]
        if missing:
            log.warning("%s: accepted without %s (source has no data)", label, missing)
        marker = {
            "session": session,
            "row_counts": counts,
            "status": f"partial: no {', '.join(missing)}" if missing else "ok",
            "landed_at": now.isoformat(),
        }
        store.write(f"{SOURCE}/_done/{session['session_key']}.json", json.dumps(marker).encode())
        landed.append(label)
        log.info("[%d/%d] %s %s (%.0fs)", i, len(todo), label, counts, time.monotonic() - t0)

    return {
        "sessions_already_landed": len(done),
        "sessions_landed_now": len(landed),
        "sessions_not_published_yet": pending,
        "sessions_remaining": len(todo) - len(landed) - len(pending),
        **api.client.stats,
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Land OpenF1 session data (2023+).")
    p.add_argument("--store", choices=["local", "volume"], default="local")
    p.add_argument("--location", default="data/landing")
    p.add_argument("--first-year", type=int, help="Default: openf1.use_from_season in config")
    p.add_argument("--max-minutes", type=float, default=330, help="Stop cleanly after this long")
    p.add_argument("--max-sessions", type=int, help="Only this many sessions (local samples)")
    p.add_argument("--include-testing", action="store_true", help="Also land pre-season testing")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    summary = run(
        make_store(args.store, args.location),
        first_year=args.first_year,
        max_minutes=args.max_minutes,
        max_sessions=args.max_sessions,
        include_testing=args.include_testing,
    )
    print(json.dumps(summary, indent=2))

    # Lets the GitHub workflow skip the Databricks load when nothing new landed.
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"landed={summary['sessions_landed_now']}\n")


if __name__ == "__main__":
    main()
