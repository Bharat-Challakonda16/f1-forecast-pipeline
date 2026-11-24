from __future__ import annotations

import io
from datetime import UTC, datetime

import pyarrow as pa
import pyarrow.parquet as pq

from f1_forecast.ingest.http import CachedClient, LocalStore, RateLimiter
from f1_forecast.ingest.openf1 import (
    DRIVER_ENDPOINTS,
    SESSION_ENDPOINTS,
    OpenF1,
    finished_sessions,
    parquet_to_records,
    records_to_parquet,
)

SESSION = {"year": 2026, "session_key": 11412, "session_name": "Race", "location": "Interlagos"}


def test_parquet_keeps_values_as_received_text():
    records = [
        {"gap": 1.234, "lap": 3, "dnf": False, "segments": [2048, 2049], "flag": None},
        {"gap": "+1 LAP", "lap": 4, "dnf": True, "segments": None, "extra": "new"},
    ]
    data = records_to_parquet(records)
    schema = pq.read_schema(io.BytesIO(data))
    assert {f.type for f in schema} == {pa.string()}
    rows = parquet_to_records(data)
    assert rows[0] == {
        "gap": "1.234",
        "lap": "3",
        "dnf": "false",
        "segments": "[2048,2049]",
        "flag": None,
        "extra": None,
    }
    assert rows[1]["gap"] == "+1 LAP"
    assert rows[1]["extra"] == "new"


def sess(key, name, end, cancelled=False):
    return {
        "session_key": key,
        "session_name": name,
        "date_start": end,
        "date_end": end,
        "is_cancelled": cancelled,
    }


def test_finished_sessions_filters_and_orders():
    now = datetime(2026, 11, 23, 12, tzinfo=UTC)
    sessions = [
        sess(3, "Race", "2026-11-22T06:00:00+00:00"),
        sess(1, "Day 1", "2026-02-26T16:00:00+00:00"),  # testing
        sess(2, "Practice 1", "2026-11-20T03:00:00+00:00"),
        sess(4, "Race", "2026-11-23T10:00:00+00:00"),  # ended 2h ago: too fresh
        sess(5, "Sprint", "2026-10-01T10:00:00+00:00", cancelled=True),
    ]
    assert [s["session_key"] for s in finished_sessions(sessions, now)] == [2, 3]
    with_testing = finished_sessions(sessions, now, include_testing=True)
    assert [s["session_key"] for s in with_testing] == [1, 2, 3]


class FakeOpenF1(OpenF1):
    def __init__(self, store, data):
        client = CachedClient(source="openf1", store=store, limiter=RateLimiter())
        super().__init__(client, store, "https://api.test/v1/")
        self.data = data
        self.calls: list[tuple[str, dict]] = []

    def fetch(self, endpoint, **params):
        self.calls.append((endpoint, params))
        rows = self.data.get(endpoint, [])
        return rows(params) if callable(rows) else rows


def full_data():
    return {
        "drivers": [{"driver_number": 1}, {"driver_number": 44}],
        "laps": [{"lap_number": 1}, {"lap_number": 2}],
        "weather": [{"air_temperature": 25.1}],
        "car_data": lambda p: [{"speed": 300, "driver_number": p["driver_number"]}] * 3,
        "location": lambda p: [{"x": 1, "driver_number": p["driver_number"]}] * 2,
    }


def test_ingest_session_lands_everything_and_skips_empty(tmp_path):
    api = FakeOpenF1(LocalStore(tmp_path), full_data())
    counts = api.ingest_session(SESSION)
    assert counts["car_data"] == 6 and counts["location"] == 4 and counts["pit"] == 0
    assert set(counts) == set(SESSION_ENDPOINTS + DRIVER_ENDPOINTS)
    assert (tmp_path / "openf1/car_data/2026/11412__driver=44.parquet").is_file()
    assert (tmp_path / "openf1/laps/2026/11412.parquet").is_file()
    assert not (tmp_path / "openf1/pit/2026/11412.parquet").exists()  # empty: no file


def test_unpublished_session_returns_none(tmp_path):
    data = full_data() | {"laps": []}
    assert FakeOpenF1(LocalStore(tmp_path), data).ingest_session(SESSION) is None


def test_old_session_missing_laps_is_landed_as_partial(tmp_path):
    data = full_data() | {"laps": []}
    counts = FakeOpenF1(LocalStore(tmp_path), data).ingest_session(SESSION, require_complete=False)
    assert counts["laps"] == 0
    assert counts["car_data"] == 6  # everything else that exists is still landed


def test_interrupted_session_resumes_without_refetching_landed_files(tmp_path):
    store = LocalStore(tmp_path)
    FakeOpenF1(store, full_data()).ingest_session(SESSION)
    again = FakeOpenF1(store, full_data())
    counts = again.ingest_session(SESSION)
    assert counts["car_data"] == 6
    refetched = {e for e, _ in again.calls}
    # only endpoints that were empty (no file) are asked again
    assert "car_data" not in refetched and "laps" not in refetched and "drivers" not in refetched
    assert "pit" in refetched
