from __future__ import annotations

from datetime import UTC, date, datetime

from f1_forecast.ingest.http import LocalStore
from f1_forecast.ingest.weather_forecast import (
    PAST_VARS,
    Weekend,
    ensemble_records,
    forecast_records,
    land_live,
    land_past,
    past_columns,
    past_records,
)

WK = Weekend(2026, 22, "losail", 25.49, 51.45, date(2026, 11, 27), date(2026, 11, 29))
TIMES = ["2026-11-29T15:00", "2026-11-29T16:00"]


def test_forecast_records_one_per_hour():
    payload = {"hourly": {"time": TIMES, "precipitation": [0.0, 1.5]}}
    rows = forecast_records(payload, {"season": 2026})
    assert rows == [
        {"season": 2026, "time": TIMES[0], "precipitation": 0.0},
        {"season": 2026, "time": TIMES[1], "precipitation": 1.5},
    ]


def test_ensemble_records_split_members():
    payload = {
        "hourly": {
            "time": TIMES,
            "precipitation": [0.0, 0.1],
            "temperature_2m": [25.0, 24.0],
            "precipitation_member01": [0.5, 0.6],
            "temperature_2m_member01": [26.0, 25.0],
        }
    }
    rows = ensemble_records(payload, {})
    assert len(rows) == 4
    assert rows[1] == {
        "time": TIMES[0],
        "member": 1,
        "precipitation": 0.5,
        "temperature_2m": 26.0,
    }
    assert rows[0]["member"] == 0 and rows[0]["temperature_2m"] == 25.0


def test_past_records_long_by_lead_and_drop_empty_leads():
    cols = past_columns()
    assert cols[: len(PAST_VARS)] == PAST_VARS  # lead 0 = plain names
    assert "precipitation_previous_day7" in cols
    hourly = {"time": TIMES} | {c: [None, None] for c in cols}
    hourly["precipitation"] = [0.0, 0.2]
    hourly["precipitation_previous_day3"] = [1.0, 2.0]
    rows = past_records({"hourly": hourly}, {"round": 22})
    assert sorted({r["lead_days"] for r in rows}) == [0, 3]
    r3 = [r for r in rows if r["lead_days"] == 3]
    assert [r["precipitation"] for r in r3] == [1.0, 2.0]
    assert r3[0]["round"] == 22 and r3[0]["temperature_2m"] is None


class FakeClient:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    def fetch(self, url, params, *, cost=1.0):
        import json

        self.calls.append((url, params, cost))
        return json.dumps(self.payload).encode()


CFG = {"endpoints": {"forecast": "F", "ensemble": "E", "previous_runs": "P"}}


def test_land_live_once_per_hour_and_from_today(tmp_path):
    store = LocalStore(tmp_path)
    client = FakeClient({"hourly": {"time": TIMES, "precipitation": [0, 0]}})
    now = datetime(2026, 11, 28, 8, 15, tzinfo=UTC)
    assert land_live(store, client, CFG, [WK], now) == 2
    assert client.calls[0][1]["start_date"] == "2026-11-28"  # past days not requested
    assert client.calls[1][2] > 1  # ensemble members are charged
    assert land_live(store, client, CFG, [WK], now) == 0  # same hour: nothing new
    assert store.exists("openmeteo/forecast/2026_22/2026-11-28T08.parquet")


def test_land_past_only_finished_weekends(tmp_path):
    store = LocalStore(tmp_path)
    hourly = {"time": TIMES} | {c: [0.0, 0.0] for c in past_columns()}
    client = FakeClient({"hourly": hourly})
    assert land_past(store, client, CFG, [WK], date(2026, 11, 29)) == 0  # race day: not yet
    assert land_past(store, client, CFG, [WK], date(2026, 11, 30)) == 1
    assert land_past(store, client, CFG, [WK], date(2026, 12, 5)) == 0  # never twice
    old = Weekend(2023, 5, "x", 0, 0, date(2023, 5, 1), date(2023, 5, 3))
    assert land_past(store, client, CFG, [old], date(2026, 12, 5)) == 0  # before coverage
