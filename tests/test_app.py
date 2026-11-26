"""Smoke test: every page of the Streamlit app renders without errors on sample data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")

APP = Path(__file__).resolve().parent.parent / "app"
PAGES = [
    "streamlit_app.py",
    "views/next_race.py",
    "views/championship.py",
    "views/accuracy.py",
    "views/about.py",
]


def driver(d, team, color, p):
    return {
        "driver_id": d,
        "code": d[:3].upper(),
        "full_name": d.title(),
        "constructor_id": team,
        "team_name": team.title(),
        "team_color": color,
        "p_pole": p,
        "p_top3_grid": p,
        "expected_grid": 3.0,
        "p_win": p,
        "p_podium": min(1, 2 * p),
        "p_points": 0.9,
        "p_dnf": 0.1,
        "expected_position": 4.0,
        "expected_points": 10.0,
    }


@pytest.fixture
def site_data(tmp_path, monkeypatch):
    out = tmp_path / "site_data"
    out.mkdir()
    drivers = [driver("alpha", "red", "FF0000", 0.6), driver("beta", "blue", None, 0.4)]
    files = {
        "meta": {
            "generated_at": "2026-11-27T10:00:00Z",
            "model_version": "sim_v7",
            "sims": 100,
            "latest_stage": "post_practice",
        },
        "next_race": {
            "event": {
                "season": 2026,
                "round": 22,
                "event_name": "Test Grand Prix",
                "circuit_id": "x",
                "circuit_name": "Test Circuit",
                "country": "Nowhere",
                "race_date": "2026-11-29",
                "is_sprint": False,
            },
            "stages": [
                {"stage": "pre_weekend", "label": "Before the weekend"},
                {"stage": "post_practice", "label": "After practice"},
            ],
            "latest_stage": "post_practice",
            "forecasts": {"pre_weekend": drivers, "post_practice": drivers},
            "strategies": {"alpha": [{"strategy": "M-H", "share": 0.6}]},
        },
        "season": {
            "season": 2026,
            "drivers": [
                {
                    "driver_id": "alpha",
                    "full_name": "Alpha",
                    "current_points": 100,
                    "expected_points": 120,
                    "points_p05": 105,
                    "points_p95": 140,
                    "p_title": 0.7,
                    "p_top3": 1.0,
                }
            ],
            "constructors": [
                {
                    "constructor_id": "red",
                    "team_name": "Red",
                    "current_points": 200,
                    "expected_points": 230,
                    "points_p05": 210,
                    "points_p95": 250,
                    "p_title": 0.8,
                    "p_top3": 1.0,
                }
            ],
            "remaining_events": [{"round": 22, "event_name": "Test Grand Prix"}],
        },
        "accuracy": [
            {"season": 2026, "stage": s, "method": m, "metric": k, "value": 0.5, "n_events": 21}
            for s in ("pre_weekend", "post_practice", "qualifying")
            for m in ("model", "championship")
            for k in ("log_loss_win", "winner_hit", "log_loss_pole")
        ],
    }
    for name, obj in files.items():
        (out / f"{name}.json").write_text(json.dumps(obj))
    import sys

    monkeypatch.syspath_prepend(str(APP))
    sys.modules.pop("lib", None)
    import lib

    monkeypatch.setattr(lib, "DATA_DIR", out)
    lib.load.clear()
    return out


@pytest.mark.parametrize("page", PAGES)
def test_page_renders(site_data, page):
    at = st_testing.AppTest.from_file(str(APP / page), default_timeout=30)
    at.run()
    assert not at.exception, at.exception
