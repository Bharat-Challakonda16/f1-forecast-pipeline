from __future__ import annotations

import io
import json

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from f1_forecast.ingest.fastf1_ingest import (
    MAX_EMPTY_ATTEMPTS,
    frame_to_parquet,
    record_result,
    session_id,
)
from f1_forecast.ingest.http import LocalStore


def read(data: bytes) -> pa.Table:
    return pq.read_table(io.BytesIO(data))


def test_frame_to_parquet_all_text_with_session_columns():
    df = pd.DataFrame(
        {
            "LapTime": pd.to_timedelta(["0 days 00:01:23.456", None]),
            "Speed": [312.0, None],
            "nGear": [8, 7],
            "IsPersonalBest": [True, False],
            "Compound": ["SOFT", None],
            "Date": pd.to_datetime(["2022-05-22 13:05:01.123", "2022-05-22 13:05:02.000"]),
        }
    )
    table = read(frame_to_parquet(df, {"_year": "2022", "_session": "R"}))
    assert {f.type for f in table.schema} == {pa.string()}
    rows = table.to_pylist()
    assert float(rows[0]["LapTime"]) == 83.456
    assert rows[1]["LapTime"] is None and rows[1]["Speed"] is None
    assert rows[0]["nGear"] == "8"
    assert rows[0]["IsPersonalBest"] == "true"
    assert rows[1]["Compound"] is None
    assert rows[0]["Date"].startswith("2022-05-22 13:05:01.123")
    assert rows[1]["_year"] == "2022" and rows[1]["_session"] == "R"


def test_frame_to_parquet_handles_unconvertible_objects():
    df = pd.DataFrame({"Mixed": [[1, 2], "x", None]})
    rows = read(frame_to_parquet(df, {})).to_pylist()
    assert [r["Mixed"] for r in rows] == ["[1, 2]", "x", None]


def test_frame_to_parquet_all_null_column():
    rows = read(frame_to_parquet(pd.DataFrame({"Empty": [None, None]}), {})).to_pylist()
    assert rows == [{"Empty": None}, {"Empty": None}]


def test_session_id():
    assert session_id(2019, 3, "FP1") == "2019_03_FP1"


def test_ok_session_is_marked_done(tmp_path):
    store = LocalStore(tmp_path)
    assert record_result(store, {"session_id": "2022_06_R", "status": "ok"}) is True
    assert store.exists("fastf1/_done/2022_06_R.json")


def test_empty_session_retried_before_accepted(tmp_path):
    store = LocalStore(tmp_path)
    result = {"session_id": "2019_17_FP3", "status": "no_timing_data"}
    for _ in range(1, MAX_EMPTY_ATTEMPTS):
        assert record_result(store, result) is False
        assert not store.exists("fastf1/_done/2019_17_FP3.json")
    assert record_result(store, result) is True
    marker = json.loads(store.read("fastf1/_done/2019_17_FP3.json"))
    assert marker["status"] == "no_timing_data"


def test_parse_fill():
    from f1_forecast.ingest.fastf1_ingest import parse_fill

    assert parse_fill("2018:14:Race") == (2018, 14, "Race", None)
    assert parse_fill("2021:4:Sprint Qualifying") == (2021, 4, "Sprint Qualifying", None)
    assert parse_fill("2025:17:Qualifying:laps, results") == (
        2025,
        17,
        "Qualifying",
        {"laps", "results"},
    )


def test_tolerant_tyre_fix_skips_only_failing_driver():
    import pandas as pd
    import pytest

    session_cls = pytest.importorskip("fastf1.core").Session  # optional dependency
    from f1_forecast.ingest.fastf1_ingest import tolerant_tyre_fix

    attr = "_Session__fix_tyre_info"
    original = getattr(session_cls, attr)

    def boom(self, df, splits):
        if df["Driver"].iloc[0] == "7":
            raise IndexError("list index out of range")
        return df.assign(fixed=True)

    setattr(session_cls, attr, boom)
    try:
        notes: list[str] = []
        with tolerant_tyre_fix(notes):
            fix = getattr(session_cls, attr)
            ok = fix(None, pd.DataFrame({"Driver": ["44"], "Time": [1]}), [])
            bad = fix(None, pd.DataFrame({"Driver": ["7", "7"], "Time": [2, 1]}), [])
        assert "fixed" in ok and "fixed" not in bad
        assert bad["Time"].tolist() == [1, 2]  # fallback keeps messages time-ordered
        assert notes == ["tyre-info tidy-up skipped for driver 7 (FastF1 IndexError)"]
        assert getattr(session_cls, attr) is boom  # restored after the block
    finally:
        setattr(session_cls, attr, original)
