from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from f1_forecast.features.field_model import prepare

CIRCUITS = [
    ("monaco", "low"),
    ("monza", "high"),
    ("hungaroring", "low"),
    ("suzuka", "high"),
    ("bahrain", "medium"),
]


def synthetic(seed: int = 1) -> tuple[pd.DataFrame, dict]:
    """4 teams x 2 drivers, 4 seasons x 10 rounds. Truth: team 'red' gains 1.0% mid-season
    in every season (upgrade); driver 'ace' is 0.4% faster at monaco than elsewhere."""
    rng = np.random.default_rng(seed)
    teams = {"red": 0.6, "blue": 0.0, "green": 0.3, "pink": 0.9}
    drivers = {
        "red": ["ace", "r2"],
        "blue": ["b1", "b2"],
        "green": ["g1", "g2"],
        "pink": ["p1", "p2"],
    }
    skill = {"ace": -0.2, "r2": 0.2, "b1": 0.0, "b2": 0.1, "g1": -0.1, "g2": 0.0, "p1": 0.1}
    skill["p2"] = -0.1
    rows = []
    for season in range(2020, 2024):
        # swap two drivers each season so the car scales are tied together
        line = {k: list(v) for k, v in drivers.items()}
        if season % 2:
            line["blue"][1], line["green"][1] = line["green"][1], line["blue"][1]
        for rnd in range(1, 11):
            cid, prof = CIRCUITS[(rnd - 1) % 5]
            for team, ds in line.items():
                car = teams[team] - (1.0 if team == "red" and rnd >= 6 else 0.0)
                for drv in ds:
                    bonus = -0.4 if (drv == "ace" and cid == "monaco") else 0.0
                    pace = car + skill[drv] + bonus + rng.normal(0, 0.1)
                    rows.append((season, rnd, cid, prof, drv, team, pace))
    df = pd.DataFrame(
        rows,
        columns=[
            "season",
            "round",
            "circuit_id",
            "speed_profile",
            "driver_id",
            "constructor_id",
            "pace_pct",
        ],
    )
    return df, {"upgrade": 1.0, "ace_monaco": -0.4}


def test_prepare_indexes_every_effect():
    df, _ = synthetic()
    data = prepare(df)
    o = data.obs
    assert data.meta["n_observations"] == len(df)
    assert data.meta["n_car_rounds"] == 4 * 4 * 10  # teams x seasons x rounds
    assert (o["ct_i"] >= 0).all() and (o["dt_i"] >= 0).all()
    # each team-season's car walk starts once per season
    assert data.car_keys["is_first"].sum() == 16
    assert data.meta["n_driver_tracks"] == 8 * 5


def test_unknown_track_type_has_no_type_effect():
    df, _ = synthetic()
    df.loc[df["circuit_id"] == "bahrain", "speed_profile"] = None
    data = prepare(df)
    o = data.obs
    assert (o.loc[o["circuit_id"] == "bahrain", ["ct_i", "dt_i"]] == -1).all().all()


@pytest.mark.slow
def test_fit_recovers_upgrade_and_track_bonus():
    from f1_forecast.features.field_model import fit

    df, truth = synthetic()
    eff, diag = fit(df, draws=300, tune=400, chains=2)
    car = eff[(eff["effect"] == "car") & (eff["constructor_id"] == "red")]
    # a random walk spreads the step over a few rounds: compare the season's ends
    by = car.pivot(index="round", columns="season", values="value")
    gain = by.loc[1] - by.loc[10]
    assert np.allclose(gain, truth["upgrade"], atol=0.3)
    tr = eff[eff["effect"] == "driver_track"].set_index(["driver_id", "circuit_id"])["value"]
    ty = eff[eff["effect"] == "driver_type"].set_index(["driver_id", "speed_profile"])["value"]
    # found the Monaco specialist: a Monaco bonus, not "good on all slow tracks"
    assert tr[("ace", "monaco")] < -0.2
    assert tr[("ace", "monaco")] < ty[("ace", "low")]
    # within one car only the teammate difference is pinned down exactly
    assert abs(tr[("ace", "monaco")] - tr[("r2", "monaco")] - truth["ace_monaco"]) < 0.15
    assert tr.drop(("ace", "monaco")).abs().max() < 0.2
