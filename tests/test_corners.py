from __future__ import annotations

import numpy as np
import pandas as pd

from f1_forecast.features.corners import apexes, corner_class, session_corner_speeds, track_corners

# A synthetic lap: 300 kph straights with three corners of known apex speed at known places.
CORNERS = [(0.2, 90.0), (0.5, 170.0), (0.8, 240.0)]  # (lap fraction, apex kph)


def lap_trace(offsets=(0.0, 0.0, 0.0), n=360, lap_s=90.0, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    t = np.linspace(0, lap_s, n)
    frac = t / lap_s  # close enough to distance fraction for the test track
    v = np.full(n, 300.0)
    for (pos, apex), off in zip(CORNERS, offsets, strict=True):
        dip = (300.0 - (apex + off)) * np.exp(-(((frac - pos) / 0.02) ** 2))
        v = np.minimum(v, 300.0 - dip)
    return t, v + rng.normal(0, noise, n)


def test_apexes_finds_each_corner_with_its_speed():
    t, v = lap_trace()
    a = apexes(t, v)
    assert len(a) == 3
    for (pos, apex), (_, row) in zip(CORNERS, a.iterrows(), strict=True):
        assert abs(row.frac - pos) < 0.03
        assert abs(row.speed_kph - apex) < 3


def test_small_lifts_are_not_corners():
    t, v = lap_trace()
    v = v.copy()
    v[100] -= 8  # an 8 kph lift on a straight
    assert len(apexes(t, v)) == 3


def test_track_corners_needs_half_the_field():
    common = pd.DataFrame({"frac": [0.2, 0.5], "speed_kph": [90, 170]})
    odd = pd.DataFrame({"frac": [0.2, 0.5, 0.9], "speed_kph": [90, 170, 200]})
    c = track_corners({"a": common, "b": common, "c": odd, "d": common})
    assert np.allclose(c, [0.2, 0.5])


def test_corner_class_thresholds():
    assert [corner_class(v) for v in (90, 129.9, 130, 209, 210, 260)] == [
        "low",
        "low",
        "medium",
        "medium",
        "high",
        "high",
    ]


def test_session_corner_speeds_ranks_teams_by_class():
    rows = []
    # "fast_low" is 6 kph quicker in slow corners; "fast_high" 6 kph quicker in fast ones.
    teams = {"fast_low": (6, 0, 0), "fast_high": (0, 0, 6), "base": (0, 0, 0)}
    for team, offs in teams.items():
        for k in range(2):
            t, v = lap_trace(offs, noise=0.5, seed=10 * len(team) + k)
            rows += [(f"{team}_{k}", team, ti, vi) for ti, vi in zip(t, v, strict=True)]
    laps = pd.DataFrame(rows, columns=["driver_id", "constructor_id", "t_s", "speed_kph"])
    out = session_corner_speeds(laps).set_index(["constructor_id", "corner_class"])["kph_vs_field"]
    assert out[("fast_low", "low")] > out[("base", "low")] + 3
    assert out[("fast_high", "high")] > out[("base", "high")] + 3
    assert abs(out[("fast_low", "high")] - out[("base", "high")]) < 2


def test_broken_trace_without_movement_is_skipped():
    t = np.linspace(0, 90, 100)
    assert apexes(t, np.zeros(100)).empty
