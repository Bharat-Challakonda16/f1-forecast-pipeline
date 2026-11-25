from __future__ import annotations

import numpy as np
import pandas as pd

from f1_forecast.sim.field_inputs import NEW_SEASON_CAR_SD, apply_field_effects


def effects() -> pd.DataFrame:
    rows = []
    for kind in ("quali", "race"):
        rows += [
            # red bull: slow early, upgraded by round 3
            (kind, "car", "red_bull", None, None, None, 2026, 1, 0.0, 0.1, None),
            (kind, "car", "red_bull", None, None, None, 2026, 3, -0.8, 0.1, None),
            (kind, "car", "mercedes", None, None, None, 2026, 3, -1.0, 0.1, None),
            # williams last seen in 2025 only
            (kind, "car", "williams", None, None, None, 2025, 24, 0.5, 0.1, None),
            (kind, "car_type", "red_bull", None, None, "high", 2026, None, -0.2, 0.05, None),
            (kind, "skill", None, "max_verstappen", None, None, 2026, None, -0.7, 0.1, None),
            (kind, "skill", None, "hadjar", None, None, 2025, None, -0.2, 0.1, None),
            (
                kind,
                "driver_type",
                None,
                "max_verstappen",
                None,
                "high",
                None,
                None,
                -0.05,
                0.03,
                None,
            ),
            (
                kind,
                "driver_track",
                None,
                "max_verstappen",
                "suzuka",
                None,
                None,
                None,
                -0.15,
                0.05,
                6,
            ),
            (kind, "hyper:sigma_upgrade", None, None, None, None, None, None, 0.15, 0.01, None),
            (kind, "hyper:sigma_walk", None, None, None, None, None, None, 0.1, 0.01, None),
        ]
    cols = [
        "kind",
        "effect",
        "constructor_id",
        "driver_id",
        "circuit_id",
        "speed_profile",
        "season",
        "round",
        "value",
        "std_err",
        "n_samples",
    ]
    return pd.DataFrame(rows, columns=cols)


def frames():
    drivers = pd.DataFrame(
        {
            "driver_id": ["max_verstappen", "hadjar", "rookie"],
            "constructor_id": ["red_bull", "red_bull", "williams"],
        }
    )
    teams = pd.DataFrame({"constructor_id": ["red_bull", "williams", "newteam"]})
    return drivers, teams


def test_latest_round_and_track_bonus():
    d, t = apply_field_effects(
        effects(), *frames(), season=2026, circuit_id="suzuka", speed_profile="high"
    )
    t = t.set_index("constructor_id")
    d = d.set_index("driver_id")
    assert t.loc["red_bull", "car_race"] == -0.8  # the upgraded car, not the early one
    assert t.loc["red_bull", "track_adj_race"] == -0.2
    assert np.isclose(t.loc["red_bull", "car_race_se"], np.hypot(0.1, 0.15))
    # Verstappen at Suzuka: skill + high-speed bonus + Suzuka bonus
    assert np.isclose(d.loc["max_verstappen", "race_pace"], -0.7 - 0.05 - 0.15)
    assert d.loc["max_verstappen", "race_track_visits"] == 6


def test_new_season_rookie_and_new_team():
    d, t = apply_field_effects(
        effects(), *frames(), season=2026, circuit_id="monza", speed_profile="high"
    )
    t = t.set_index("constructor_id")
    d = d.set_index("driver_id")
    # williams: last season's car, winter uncertainty, no track adjustment carried over
    assert t.loc["williams", "car_quali"] == 0.5
    assert np.isclose(t.loc["williams", "car_quali_se"], np.hypot(0.1, NEW_SEASON_CAR_SD))
    assert t.loc["williams", "track_adj_quali"] == 0.0
    assert t.loc["newteam", "car_quali_se"] == 1.0
    # hadjar carried one season with drift; rookie at the prior
    assert np.isclose(d.loc["hadjar", "quali_pace_se"], np.hypot(0.1, 0.1))
    assert d.loc["rookie", "quali_pace"] == 0.0 and d.loc["rookie", "quali_pace_se"] == 0.3


def test_scale_retirements_matches_field_rate_and_keeps_order():
    from f1_forecast.sim.inputs import LAP1_RETIRE_SHARE, scale_retirements

    drivers = pd.DataFrame(
        {
            "driver_id": ["a", "b", "c", "d"],
            "constructor_id": ["x", "x", "y", "y"],
            "error_rate": [0.06, 0.08, 0.06, 0.04],
        }
    )
    teams = pd.DataFrame({"constructor_id": ["x", "y"], "reliability_dnf_rate": [0.10, 0.20]})
    d, t, factor = scale_retirements(drivers, teams, field_rate=0.12, lap1_prob=0.4)
    rel = d["constructor_id"].map(t.set_index("constructor_id")["reliability_dnf_rate"])
    lap1 = 0.4 * LAP1_RETIRE_SHARE / 4
    assert np.isclose((rel + d["error_rate"]).mean() + lap1, 0.12)
    assert factor < 1
    assert t["reliability_dnf_rate"].iloc[1] > t["reliability_dnf_rate"].iloc[0]  # order kept
