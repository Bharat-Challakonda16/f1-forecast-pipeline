"""FastF1 ingestion for 2018-2022 (OpenF1 covers 2023+).

Since ~2026-10-15 F1's livetiming archive needs an F1 TV Access (or higher) login. FastF1
signs in with ``F1_USERNAME`` / ``F1_PASSWORD`` (GitHub secrets in CI) or a token stored by
``python -m fastf1 auth --authenticate f1tv`` locally. The run stops immediately if sign-in
fails, so a bad login can never be recorded as "no data".

Like OpenF1, every table is landed as Parquet with values as text (timedeltas as seconds),
plus ``_year``, ``_round``, ``_session`` columns because FastF1 frames carry no session key.

Landing layout::

    fastf1/<table>/<year>/<year>_<round>_<session>.parquet
    fastf1/_done/<year>_<round>_<session>.json      written last: row counts / status

FastF1's own download cache (~300 MB per weekend) lives in a temporary folder that is
deleted after every event, so a CI runner's disk never fills up.

Run ``python -m f1_forecast.ingest.fastf1_ingest --help``.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from f1_forecast.config import load_sources
from f1_forecast.ingest.http import Store
from f1_forecast.ingest.stores import make_store

log = logging.getLogger(__name__)

SOURCE = "fastf1"
SESSION_CODES = {
    "Practice 1": "FP1",
    "Practice 2": "FP2",
    "Practice 3": "FP3",
    "Qualifying": "Q",
    "Sprint Qualifying": "SQ",
    "Sprint Shootout": "SS",
    "Sprint": "S",
    "Race": "R",
}
# FastF1 enforces 500 livetiming calls/hour per process; wait this long when it trips.
RATE_LIMIT_PAUSE_S = 15 * 60


# --------------------------------------------------------------------------- conversion


def frame_to_parquet(df: Any, extra: dict[str, str]) -> bytes:
    """pandas DataFrame -> Parquet, every column as nullable text, plus constant ``extra``."""
    import pandas as pd

    columns: dict[str, pa.Array] = {}
    for name in df.columns:
        s = df[name]
        if pd.api.types.is_timedelta64_dtype(s):
            s = s.dt.total_seconds()
        try:
            arr = pa.array(s, from_pandas=True)
            arr = pa.nulls(len(s), pa.string()) if pa.types.is_null(arr.type) else arr
            arr = arr.cast(pa.string())
        except (pa.ArrowInvalid, pa.ArrowNotImplementedError, pa.ArrowTypeError):
            arr = pa.array([None if _isna(v) else str(v) for v in s], type=pa.string())
        columns[str(name)] = arr
    for name, value in extra.items():
        columns[name] = pa.array([value] * len(df), type=pa.string())
    buf = io.BytesIO()
    pq.write_table(pa.table(columns), buf, compression="zstd")
    return buf.getvalue()


def _isna(value: Any) -> bool:
    try:
        import pandas as pd

        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False  # lists/arrays


def session_id(year: int, rnd: int, code: str) -> str:
    return f"{year}_{rnd:02d}_{code}"


# --------------------------------------------------------------------------- FastF1


def check_login() -> dict[str, Any]:
    """Sign in (or reuse a valid token) and confirm an active F1 TV subscription."""
    from fastf1.internals.f1account import get_token

    claims = get_token().claims
    if str(claims.get("SubscriptionStatus", "")).lower() != "active":
        raise SystemExit(f"F1 TV subscription not active: {claims.get('SubscriptionStatus')}")
    return {"product": claims.get("SubscribedProduct"), "status": claims.get("SubscriptionStatus")}


def session_tables(session: Any) -> dict[str, Callable[[], Any]]:
    """Table name -> function returning a DataFrame (each may fail independently)."""
    import pandas as pd

    def per_driver(data: dict) -> pd.DataFrame:
        frames = [df.assign(DriverNumber=num) for num, df in data.items()]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def corners() -> pd.DataFrame:
        info = session.get_circuit_info()
        return info.corners.assign(Rotation=info.rotation)

    return {
        "laps": lambda: pd.DataFrame(session.laps),
        "results": lambda: pd.DataFrame(session.results),
        "car_data": lambda: per_driver(session.car_data),
        "position": lambda: per_driver(session.pos_data),
        "weather": lambda: session.weather_data,
        "race_control": lambda: session.race_control_messages,
        "track_status": lambda: session.track_status,
        "session_status": lambda: session.session_status,
        "circuit_corners": corners,
    }


@contextmanager
def tolerant_tyre_fix(notes: list[str]) -> Iterator[None]:
    """Work around a FastF1 3.9 crash in ``Session.__fix_tyre_info`` (e.g. 2018 Monza race).

    FastF1 tidies each driver's tyre-message timestamps; for a few drivers that step raises
    ``IndexError`` and FastF1 then discards the *whole* lap table. Here the tidy-up is
    skipped for just that driver (tyre data kept exactly as F1 recorded it) and noted.
    """
    from fastf1.core import Session

    attr = "_Session__fix_tyre_info"
    original = getattr(Session, attr)

    def patched(self, df, stint_split_times):
        try:
            return original(self, df, stint_split_times)
        except IndexError:
            driver = df["Driver"].iloc[0] if "Driver" in df and len(df) else "?"
            notes.append(f"tyre-info tidy-up skipped for driver {driver} (FastF1 IndexError)")
            # Later steps merge on time, so the raw messages must at least be time-ordered.
            return df.sort_values("Time", kind="stable").reset_index(drop=True)

    setattr(Session, attr, patched)
    try:
        yield
    finally:
        setattr(Session, attr, original)


def ingest_session(
    store: Store,
    year: int,
    rnd: int,
    name: str,
    event_name: str,
    tables: set[str] | None = None,
) -> dict:
    """Load one session through FastF1 and land each table (or only ``tables``)."""
    import fastf1
    from fastf1.exceptions import RateLimitExceededError

    code = SESSION_CODES.get(name, name.replace(" ", ""))
    sid = session_id(year, rnd, code)
    extra = {"_year": str(year), "_round": str(rnd), "_session": code, "_event": event_name}

    while True:
        try:
            session = fastf1.get_session(year, rnd, name)
            session.load(laps=True, telemetry=True, weather=True, messages=True)
            break
        except RateLimitExceededError:
            log.warning("FastF1 hourly limit reached; pausing %d min", RATE_LIMIT_PAUSE_S // 60)
            time.sleep(RATE_LIMIT_PAUSE_S)

    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    for table, get in session_tables(session).items():
        if tables is not None and table not in tables:
            continue
        try:
            df = get()
        except Exception as exc:  # data missing for this session/table
            errors[table] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        counts[table] = len(df)
        path = f"{SOURCE}/{table}/{year}/{sid}.parquet"
        # Landed files are immutable: never rewrite one (bronze has already loaded it).
        if len(df) and not store.exists(path):
            store.write(path, frame_to_parquet(df, extra))
    status = "ok" if counts.get("laps") else "no_timing_data"
    return {"session_id": sid, "status": status, "row_counts": counts, "errors": errors}


def events_for(year: int) -> list[tuple[int, str, list[tuple[str, datetime]]]]:
    """(round, event name, [(session name, start UTC)]) for every championship event."""
    import fastf1
    import pandas as pd

    schedule = fastf1.get_event_schedule(year, include_testing=False)
    out = []
    for _, ev in schedule.iterrows():
        sessions = []
        for i in range(1, 6):
            name, start = ev.get(f"Session{i}"), ev.get(f"Session{i}DateUtc")
            if name and not pd.isna(start):
                sessions.append((name, pd.Timestamp(start).tz_localize("UTC").to_pydatetime()))
        out.append((int(ev["RoundNumber"]), str(ev["EventName"]), sessions))
    return out


# --------------------------------------------------------------------------- orchestration

# Sessions with no timing data are retried on later runs (could be a network blip) and
# only accepted as genuinely empty (e.g. cancelled by weather) after this many attempts.
MAX_EMPTY_ATTEMPTS = 2


def record_result(store: Store, result: dict[str, Any]) -> bool:
    """Write the ``_done`` marker when the session is final. False = retry next run."""
    sid = result["session_id"]
    if result["status"] != "ok":
        key = f"{SOURCE}/_empty_attempts/{sid}.json"
        attempts = json.loads(store.read(key))["attempts"] + 1 if store.exists(key) else 1
        store.write(key, json.dumps({"attempts": attempts, "last": result}).encode())
        if attempts < MAX_EMPTY_ATTEMPTS:
            log.warning("%s: no timing data (attempt %d), will retry next run", sid, attempts)
            return False
    store.write(f"{SOURCE}/_done/{sid}.json", json.dumps(result).encode())
    return True


def run(
    store: Store,
    *,
    years: list[int],
    max_minutes: float = 330,
    max_sessions: int | None = None,
) -> dict[str, Any]:
    import fastf1

    os.environ.setdefault("FASTF1_AUTH_METHOD", "password")
    fastf1.set_log_level("WARNING")
    signed_in = False  # only sign in once there is actually something to download

    deadline = time.monotonic() + max_minutes * 60
    done = {n.removesuffix(".json") for n in store.list_names(f"{SOURCE}/_done")}
    landed, partial, processed = [], [], 0
    out_of_time = False

    for year in years:
        cache_root = Path(tempfile.mkdtemp(prefix="fastf1_"))
        try:
            fastf1.Cache.enable_cache(str(cache_root))
            events = events_for(year)
        finally:
            shutil.rmtree(cache_root, ignore_errors=True)

        for rnd, event_name, sessions in events:
            todo = [
                (n, start)
                for n, start in sessions
                if session_id(year, rnd, SESSION_CODES.get(n, n.replace(" ", ""))) not in done
            ]
            if not todo:
                continue
            if not signed_in:
                log.info("F1 TV sign-in OK (%s)", check_login()["product"])
                signed_in = True
            cache_dir = Path(tempfile.mkdtemp(prefix="fastf1_"))
            fastf1.Cache.enable_cache(str(cache_dir))
            try:
                for name, _start in todo:
                    if time.monotonic() > deadline or (
                        max_sessions is not None and processed >= max_sessions
                    ):
                        out_of_time = True
                        break
                    t0 = time.monotonic()
                    result = ingest_session(store, year, rnd, name, event_name)
                    processed += 1
                    result["landed_at"] = datetime.now(UTC).isoformat()
                    if record_result(store, result):
                        (landed if result["status"] == "ok" else partial).append(
                            result["session_id"]
                        )
                    log.info(
                        "%s %s %s: %s %s (%.0fs)",
                        result["session_id"],
                        event_name,
                        name,
                        result["status"],
                        result["row_counts"],
                        time.monotonic() - t0,
                    )
            finally:
                shutil.rmtree(cache_dir, ignore_errors=True)  # keep the runner's disk clean
            if out_of_time:
                break
        if out_of_time:
            break

    return {
        "sessions_already_landed": len(done),
        "sessions_landed_now": len(landed),
        "sessions_without_timing_data": partial,
        "stopped_early": out_of_time,
    }


FillSpec = tuple[int, int, str, set[str] | None]


def parse_fill(spec: str) -> FillSpec:
    """``"2025:17:Qualifying"`` or ``"2025:17:Qualifying:laps,results"`` (only those tables)."""
    year, rnd, name, *rest = spec.split(":")
    tables = {t.strip() for t in rest[0].split(",") if t.strip()} if rest else None
    return int(year), int(rnd), name, tables


def fill(store: Store, specs: list[FillSpec]) -> list[dict[str, Any]]:
    """One-off gap filling for specific sessions, in any year.

    Only tables that are not landed yet are written, so already-loaded data is never
    duplicated. Results go to ``fastf1/_fills/<session>.json``; ``_done`` is left alone.
    """
    import fastf1

    os.environ.setdefault("FASTF1_AUTH_METHOD", "password")
    log.info("F1 TV sign-in OK (%s)", check_login()["product"])
    results = []
    for year, rnd, name, tables in specs:
        cache_dir = Path(tempfile.mkdtemp(prefix="fastf1_"))
        fastf1.Cache.enable_cache(str(cache_dir))
        notes: list[str] = []
        try:
            event_name = str(fastf1.get_event(year, rnd)["EventName"])
            with tolerant_tyre_fix(notes):
                result = ingest_session(store, year, rnd, name, event_name, tables)
        finally:
            shutil.rmtree(cache_dir, ignore_errors=True)
        result["notes"] = notes
        result["filled_at"] = datetime.now(UTC).isoformat()
        store.write(f"{SOURCE}/_fills/{result['session_id']}.json", json.dumps(result).encode())
        log.info(
            "fill %s: %s errors=%s notes=%s",
            result["session_id"],
            result["row_counts"],
            result["errors"],
            notes,
        )
        results.append(result)
    return results


def main(argv: list[str] | None = None) -> None:
    cfg = load_sources()[SOURCE]
    first, last = cfg["use_for_seasons"]
    p = argparse.ArgumentParser(description="Land FastF1 session data (2018-2022 by default).")
    p.add_argument("--store", choices=["local", "volume"], default="local")
    p.add_argument("--location", default="data/landing")
    p.add_argument("--years", type=int, nargs="+", default=list(range(first, last + 1)))
    p.add_argument("--max-minutes", type=float, default=330)
    p.add_argument("--max-sessions", type=int, help="Only this many sessions (samples/tests)")
    p.add_argument(
        "--fill",
        nargs="+",
        metavar="YEAR:ROUND:SESSION",
        help='Fill gaps, e.g. "2018:14:Race" or "2025:17:Qualifying:laps" (only laps)',
    )
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.fill:
        results = fill(make_store(args.store, args.location), [parse_fill(s) for s in args.fill])
        print(json.dumps(results, indent=2))
        return

    summary = run(
        make_store(args.store, args.location),
        years=args.years,
        max_minutes=args.max_minutes,
        max_sessions=args.max_sessions,
    )
    print(json.dumps(summary, indent=2))
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as f:
            new = summary["sessions_landed_now"] + len(summary["sessions_without_timing_data"])
            f.write(f"landed={new}\n")


if __name__ == "__main__":
    main()
