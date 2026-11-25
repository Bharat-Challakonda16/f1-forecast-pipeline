from __future__ import annotations

import numpy as np
import pandas as pd

from f1_forecast.sim.circuit_params import event_rates, overtaking, pit_loss, tyre_params


def test_event_rates_shrink_toward_field_and_weight_recent_seasons():
    rows = [("x", 2020, 1.0)] * 1 + [("y", s, float(s % 2)) for s in range(2010, 2026)] * 3
    n = pd.DataFrame(rows, columns=["circuit_id", "season", "had_sc"])
    for col in ("had_vsc", "had_red", "had_lap1_incident"):
        n[col] = 0.0
    r = event_rates(n, "x", 2026)
    assert 0.4 < r["sc_prob"] < 0.8  # one SC race at x: pulled toward the ~50% field rate
    assert r["races_at_circuit"] == 1
    assert event_rates(n, "unknown", 2026)["sc_prob"] < 0.6  # no races: field rate


def test_unknown_vsc_before_2018_is_ignored():
    n = pd.DataFrame(
        {
            "circuit_id": "x",
            "season": [2010, 2024],
            "had_sc": [1.0, 0.0],
            "had_vsc": [np.nan, 1.0],
            "had_red": 0.0,
            "had_lap1_incident": 0.0,
        }
    )
    assert event_rates(n, "x", 2026)["vsc_prob"] == 1.0  # only the race where it was known


def test_overtaking_quintiles_and_pit_loss():
    ov = pd.DataFrame({"circuit_id": list("abcdefghij"), "passes": range(1, 11), "green_laps": 10})
    rate, diff = overtaking(ov, "a")
    assert rate == 0.1 and diff == 5  # fewest passes = hardest
    assert overtaking(ov, "j")[1] == 1
    assert overtaking(ov, "zz") == (None, None)
    stops = pd.DataFrame({"circuit_id": "a", "loss_s": [20.0, 21.0, 22.0, 900.0]})
    assert pit_loss(stops, "a") == 21.0  # the 900 s red-flag stop is excluded
    assert pit_loss(stops, "b") == 22.0


def test_tyre_params_order_and_only_given_rows_matter():
    rng = np.random.default_rng(0)
    rows = []
    for season in (2024, 2025):
        for circ, level in (("abrasive", 2.0), ("smooth", 0.5)):
            for comp, base in (("SOFT", 0.06), ("MEDIUM", 0.045), ("HARD", 0.035)):
                for _ in range(10):
                    rows.append(
                        (
                            season,
                            circ,
                            "high",
                            comp,
                            base * level + rng.normal(0, 0.002),
                            90_000.0,
                            20.0,
                            35.0,
                        )
                    )
    st = pd.DataFrame(
        rows,
        columns=[
            "season",
            "circuit_id",
            "speed_profile",
            "compound",
            "wear_pct_per_lap",
            "median_lap_ms",
            "stint_laps",
            "track_temp_c",
        ],
    )
    off = pd.DataFrame(
        {"circuit_id": "abrasive", "compound": ["SOFT", "HARD"], "fresh_vs_medium_pct": [-0.3, 0.2]}
    )
    a = tyre_params(st, off, "abrasive", "high", 2026).set_index("compound")
    sm = tyre_params(st, off, "smooth", "high", 2026).set_index("compound")
    assert (
        a.loc["SOFT", "wear_pct_per_lap_35c"]
        > a.loc["MEDIUM", "wear_pct_per_lap_35c"]
        > a.loc["HARD", "wear_pct_per_lap_35c"]
    )
    assert (
        a.loc["MEDIUM", "circuit_wear_level"] > 1.2 > 0.8 > sm.loc["MEDIUM", "circuit_wear_level"]
    )
    assert a.loc["SOFT", "fresh_pace_vs_medium_pct"] < 0 < a.loc["HARD", "fresh_pace_vs_medium_pct"]
