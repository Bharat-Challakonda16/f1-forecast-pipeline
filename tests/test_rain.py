from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from f1_forecast.features.rain import (
    COEFS,
    MAX_LEAD,
    RainCoefs,
    climate_prob,
    fit_measured,
    outlook_frame,
    p_measured,
    pick_forecast,
    wet_probability,
)

C = RainCoefs(m0=-3.0, m1=1.0, r0=0.1, c0=-0.5, c1=2.0, tau=3.0)


def test_measured_curve_rises_with_rain():
    p = p_measured([0.0, 0.1, 1.0, 5.0], C.m0, C.m1, C.r0)
    assert np.all(np.diff(p) > 0)
    assert p[0] == pytest.approx(1 / (1 + np.exp(3.0)))


def test_climate_prob_weights_by_count():
    samples = pd.DataFrame(
        {"session_id": ["a", "a", "b"], "rain_mm": [0.0, 2.0, 0.0], "n": [3, 1, 1]}
    )
    p0, p2 = p_measured([0.0, 2.0], C.m0, C.m1, C.r0)
    got = climate_prob(samples, C)
    assert got["a"] == pytest.approx((3 * p0 + p2) / 4)
    assert got["b"] == pytest.approx(p0)


def test_wet_probability_forecast_fades_with_lead_and_needs_a_forecast():
    clim = np.full(4, 0.1)
    rainy = wet_probability(clim, [3.0, 3.0, 3.0, np.nan], [1, 4, MAX_LEAD + 1, 1], C)
    assert rainy[0] > rainy[1] > 0.1  # rain forecast raises the chance, less when far ahead
    assert rainy[2] == pytest.approx(0.1)  # beyond the fitted range: climate
    assert rainy[3] == pytest.approx(0.1)  # no forecast: climate
    dry = wet_probability([0.1], [0.0], [1], C)
    assert dry[0] < 0.1


def test_fit_measured_recovers_a_curve():
    rng = np.random.default_rng(1)
    rain = np.where(rng.random(4000) < 0.7, 0.0, rng.exponential(2.0, 4000))
    wet = rng.random(4000) < p_measured(rain, -3.0, 1.0, 0.5)
    m0, m1, r0 = fit_measured(rain, wet.astype(float))
    fitted = p_measured([0.0, 1.0, 5.0], m0, m1, r0)
    assert fitted == pytest.approx(p_measured([0.0, 1.0, 5.0], -3.0, 1.0, 0.5), abs=0.03)


SESSIONS = pd.DataFrame(
    {
        "session_id": ["q", "r"],
        "session_code": ["Q", "R"],
        "start_utc": ["2026-11-28T18:00:00Z", "2026-11-29T16:00:00Z"],
    }
)


def _fc(rows):
    return pd.DataFrame(
        rows, columns=["session_id", "source", "issued_utc", "lead_days", "forecast_mm"]
    )


def test_pick_forecast_never_looks_ahead():
    fc = _fc(
        [
            ("r", "previous_runs", None, 0, 9.0),  # latest run: never used
            ("r", "previous_runs", None, 1, 2.0),
            ("r", "previous_runs", None, 3, 1.0),
            ("r", "live", "2026-11-27T08:15", 2, 4.0),
            ("r", "live", "2026-11-29T08:15", 0, 7.0),  # issued after as_of
        ]
    )
    got = pick_forecast(fc, SESSIONS, "2026-11-27").set_index("session_id")
    assert got.loc["r", "forecast_mm"] == 4.0 and got.loc["r", "forecast_source"] == "live"
    past_only = fc[fc["source"] == "previous_runs"]
    got = pick_forecast(past_only, SESSIONS, "2026-11-26").set_index("session_id")
    assert got.loc["r", "forecast_mm"] == 1.0 and got.loc["r", "lead_days"] == 3
    got = pick_forecast(past_only, SESSIONS, "2026-11-29").set_index("session_id")
    assert got.loc["r", "lead_days"] == 1  # race day: at least a day ahead


def test_outlook_frame_without_forecasts_is_climate():
    climate = pd.DataFrame({"session_id": ["q"], "rain_mm": [0.0], "n": [10]})
    out = outlook_frame(SESSIONS, climate, _fc([]), "2026-11-20", COEFS).set_index("session_code")
    assert out.loc["Q", "p_wet"] == pytest.approx(out.loc["Q", "p_climate"])
    assert out.loc["R", "p_climate"] == pytest.approx(0.08)  # no samples: recent average
