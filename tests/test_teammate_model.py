from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from f1_forecast.features.teammate_model import prepare


def gaps(rows):
    return pd.DataFrame(rows, columns=["season", "driver_a", "driver_b", "gap_pct"])


def test_prepare_indexes_careers_in_order_with_gaps():
    df = gaps(
        [
            (2018, "alonso", "vandoorne", -0.4),
            (2021, "alonso", "ocon", -0.1),  # 2019-2020 away from F1
            (2021, "alonso", "ocon", 0.2),
            (2018, "ocon", "perez", -0.05),
        ]
    )
    d = prepare(df)
    keys = d.keys
    assert list(zip(keys.driver_id, keys.season, strict=True))[:2] == [
        ("alonso", 2018),
        ("alonso", 2021),
    ]
    alonso_2021 = keys[(keys.driver_id == "alonso") & (keys.season == 2021)].iloc[0]
    assert alonso_2021.year_gap == 3 and not alonso_2021.is_first
    assert alonso_2021.n_obs == 2
    # every row points at its own career's first row
    for _, row in keys.iterrows():
        first = keys[keys.driver_id == row.driver_id].index.min()
        assert row.start_idx == first
    # observation indices point at the right driver-season
    assert keys.loc[d.idx_a[1], ["driver_id", "season"]].tolist() == ["alonso", 2021]
    assert keys.loc[d.idx_b[3], ["driver_id", "season"]].tolist() == ["perez", 2018]
    assert d.meta == {"n_observations": 4, "n_driver_seasons": 6}


def test_prepare_drops_incomplete_rows():
    df = gaps([(2020, "a", "b", 0.1), (2020, "a", None, 0.2), (2020, "a", "b", None)])
    assert len(prepare(df).gap) == 1


def simulate(seed=0, n_drivers=12, seasons=range(2015, 2021), races=18):
    """Teams of two drivers, reshuffled each season, with known true skills."""
    rng = np.random.default_rng(seed)
    true = {f"d{i}": rng.normal(0, 0.4) for i in range(n_drivers)}
    rows = []
    for s in seasons:
        order = rng.permutation(n_drivers)
        for t in range(n_drivers // 2):
            a, b = f"d{order[2 * t]}", f"d{order[2 * t + 1]}"
            for _ in range(races):
                gap = true[a] - true[b] + rng.standard_t(5) * 0.25
                rows.append((s, a, b, gap))
    return gaps(rows), true


@pytest.mark.slow
def test_fit_recovers_known_driver_order():
    pytest.importorskip("pymc")
    pytest.importorskip("nutpie")
    from f1_forecast.features.teammate_model import fit

    df, true = simulate()
    # Same sampler budget as the real fits: shorter runs leave r-hat borderline on this data.
    est, diag = fit(df, draws=500, tune=500, chains=4)
    last = est.sort_values("season").groupby("driver_id").tail(1).set_index("driver_id")
    corr = np.corrcoef([true[d] for d in last.index], last["value"])[0, 1]
    assert corr > 0.9
    assert diag["max_rhat_skill"] < 1.05
    # uncertainty shrinks with data: every estimate has a positive, finite std error
    assert (est["std_err"] > 0).all() and np.isfinite(est["std_err"]).all()


def test_to_gold_rows_shapes_output():
    from datetime import date, datetime

    from f1_forecast.features.run_traits import TRAITS, to_gold_rows

    est = pd.DataFrame(
        {
            "driver_id": ["alonso"],
            "season": [2026],
            "n_obs": [16],
            "value": [-0.35],
            "std_err": [0.22],
            "q05": [-0.7],
            "q95": [0.0],
        }
    )
    rows = to_gold_rows(est, TRAITS["quali_pace"], date(2026, 11, 24), datetime(2026, 11, 25))
    assert rows.iloc[0].to_dict() == {
        "trait": "quali_pace",
        "driver_id": "alonso",
        "as_of_season": 2026,
        "value": -0.35,
        "std_err": 0.22,
        "q05": -0.7,
        "q95": 0.0,
        "n_samples": 16,
        "model_version": "teammate_rw_studentt_v1",
        "data_cutoff": date(2026, 11, 24),
        "fitted_at": datetime(2026, 11, 25),
    }


@pytest.mark.slow
def test_rate_model_separates_crash_prone_driver():
    pytest.importorskip("pymc")
    pytest.importorskip("nutpie")
    from f1_forecast.features.rate_model import fit

    rng = np.random.default_rng(3)
    rows = []
    # 50 races per season keeps the simulated counts informative (with ~20, random noise
    # alone can make an "average" driver look as careful as the careful one).
    for s in range(2015, 2023):
        for d, rate in [("careful", 0.03), ("average", 0.07), ("wild", 0.18)]:
            rows.append((d, s, rng.poisson(rate * 50), 50))
    rows.append(("rookie", 2022, 0, 3))  # three races, no errors: stays near average
    df = pd.DataFrame(rows, columns=["driver_id", "season", "events", "exposure"])
    est, diag = fit(df, draws=500, tune=500, chains=4)
    last = est[est.season == 2022].set_index("driver_id")["value"]
    assert last["careful"] < last["average"] < last["wild"]
    assert 0.02 < last["rookie"] < 0.15  # shrunk towards the field, not zero
    assert diag["divergences"] == 0
