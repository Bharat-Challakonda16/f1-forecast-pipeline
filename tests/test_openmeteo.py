from __future__ import annotations

import csv
import json
from datetime import date

import pytest

from f1_forecast.config import CONFIG_DIR
from f1_forecast.ingest.http import LocalStore
from f1_forecast.ingest.jolpica import LATEST_DUMP_POINTER
from f1_forecast.ingest.openmeteo import (
    Circuit,
    columnar_to_records,
    load_circuits,
    request_cost,
    settled_months,
)


def test_request_cost_matches_observed_headers():
    assert request_cost(14, 11) == pytest.approx(1.1)
    assert request_cost(366, 11) == pytest.approx(28.757, rel=1e-3)
    assert request_cost(3, 2) == 1.0  # minimum one unit
    assert request_cost(3, 102) == pytest.approx(10.2)  # ensemble: short range, many members


def test_settled_months_stops_before_unsettled_month():
    months = settled_months(date(2026, 9, 1), date(2026, 11, 24), settle_days=7)
    assert months == [(2026, 9), (2026, 10)]  # Oct ended 31st + 7 = Nov 7 <= Nov 24
    months = settled_months(date(2026, 9, 1), date(2026, 11, 5), settle_days=7)
    assert months == [(2026, 9)]


def test_settled_months_crosses_year_boundary():
    months = settled_months(date(2025, 11, 1), date(2026, 2, 20), settle_days=7)
    assert months == [(2025, 11), (2025, 12), (2026, 1)]


def _write(store, key, rows):
    header = list(rows[0])
    lines = [",".join(header)] + [",".join(str(r[h]) for h in header) for r in rows]
    store.write(key, ("\n".join(lines) + "\n").encode())


def test_load_circuits_prioritises_calendar_and_recent(tmp_path):
    store, f = LocalStore(tmp_path), "jolpica/dump/2026-11-09"
    store.write(LATEST_DUMP_POINTER, json.dumps({"folder": f}).encode())
    _write(
        store,
        f"{f}/formula_one_season.csv",
        [{"id": "s1", "year": 2000}, {"id": "s2", "year": 2010}, {"id": "s3", "year": 2026}],
    )
    with open(CONFIG_DIR / "calendar_2027.csv", encoding="utf-8") as fh:
        calendar_refs = sorted({r["circuit_ref"] for r in csv.DictReader(fh)})
    circuits = [
        {"id": f"c{i}", "reference": ref, "latitude": 1.0 + i, "longitude": 2.0}
        for i, ref in enumerate([*calendar_refs, "valencia", "a1_ring"])
    ]
    _write(store, f"{f}/formula_one_circuit.csv", circuits)
    ids = {c["reference"]: c["id"] for c in circuits}
    _write(
        store,
        f"{f}/formula_one_round.csv",
        [
            {"id": "r1", "season_id": "s2", "circuit_id": ids["valencia"]},  # raced 2010
            {"id": "r2", "season_id": "s1", "circuit_id": ids["a1_ring"]},  # only before 2001
            {"id": "r3", "season_id": "s3", "circuit_id": ids["monza"]},
        ],
    )
    out = load_circuits(store, date(2026, 11, 24), first_season=2001)
    refs = [c.ref for c in out]
    assert "a1_ring" not in refs  # last raced before 2001
    assert refs[-1] == "valencia" and not out[-1].priority  # historic circuits go last
    assert all(c.priority for c in out[:-1])
    assert set(refs[:-1]) == set(calendar_refs)


def test_columnar_to_records():
    payload = {
        "latitude": 45.6,
        "longitude": 9.3,
        "elevation": 189.0,
        "hourly": {"time": ["2024-09-01T00:00", "2024-09-01T01:00"], "rain": [0.0, 1.2]},
    }
    rows = columnar_to_records(payload, Circuit("monza", 45.6, 9.3, True))
    assert rows[1] == {
        "circuit_ref": "monza",
        "latitude": 45.6,
        "longitude": 9.3,
        "elevation": 189.0,
        "time": "2024-09-01T01:00",
        "rain": 1.2,
    }
