"""Track temperature for a session from the air temperature and the sunshine.

The asphalt is warmer than the air, more so in strong sun::

    track_c = a + b * air_c + c * solar_wm2           (means over the session window)

Fitted on dry races 2018-2024 (trackside sensors vs the hourly weather history). Used with
the forecast within ``rain.MAX_LEAD`` days of the race; further out the median track
temperature of earlier races at the circuit stays (the climate's air/sun did no better).
Run ``python -m f1_forecast.features.track_temp`` to refit and compare.
"""

from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

CATALOG = "f1_forecast"
# Fitted on dry races 2018-2024 (n=135). Held out on 2025-2026 dry races (n=37), mean error:
# from the forecast 3 days ahead 3.8 C; median of earlier races at the circuit 6.8 C; from
# the climate's air/sun 7.0 C.
COEFS = (4.41, 0.92, 0.0214)


def predict(air_c, solar_wm2, coefs: tuple[float, float, float] = COEFS):
    a, b, c = coefs
    return a + b * np.asarray(air_c, float) + c * np.asarray(solar_wm2, float)


def load_data() -> pd.DataFrame:
    """Dry races with the trackside median track/air temperature and, over the same window,
    the hourly-history air/sun, the climate air/sun and the 3-day-ahead forecast air/sun."""
    from databricks.sdk import WorkspaceClient

    from f1_forecast.features.run_traits import query_df

    w = WorkspaceClient()
    wh = next(iter(w.warehouses.list())).id
    d = query_df(
        w,
        wh,
        f"""
        WITH tw AS (
          SELECT r.session_id, any_value(r.season) AS season,
                 any_value(r.circuit_id) AS circuit_id, any_value(r.start_utc) AS start_utc,
                 any_value(r.window_start_utc) AS w0, any_value(r.window_end_utc) AS w1,
                 percentile(w.track_temp_c, 0.5) AS track
          FROM {CATALOG}.gold.session_rain r
          JOIN {CATALOG}.silver.session_weather w USING (session_id)
          WHERE r.session_code = 'R' AND NOT coalesce(r.is_wet, false)
            AND w.sample_utc BETWEEN r.start_utc AND r.window_end_utc
          GROUP BY 1),
        h AS (
          SELECT t.session_id, avg(h.temp_c) AS air, avg(h.solar_radiation_wm2) AS solar
          FROM tw t JOIN {CATALOG}.silver.weather_hourly h ON h.circuit_id = t.circuit_id
           AND h.time_utc >= date_trunc('HOUR', t.w0) AND h.time_utc < t.w1
          GROUP BY 1),
        c AS (
          SELECT session_id, avg(temp_c) AS clim_air, avg(solar_radiation_wm2) AS clim_solar
          FROM {CATALOG}.gold.session_rain_climate GROUP BY 1),
        f AS (
          SELECT session_id, temp_c AS fc_air, solar_radiation_wm2 AS fc_solar
          FROM {CATALOG}.gold.session_rain_forecast
          WHERE source = 'previous_runs' AND lead_days = 3)
        SELECT * FROM tw LEFT JOIN h USING (session_id) LEFT JOIN c USING (session_id)
        LEFT JOIN f USING (session_id)""",
    )
    for col in d.columns:
        if col not in ("session_id", "circuit_id", "start_utc", "w0", "w1"):
            d[col] = pd.to_numeric(d[col])
    return d.dropna(subset=["track", "air", "solar"])


def fit(d: pd.DataFrame) -> tuple[float, float, float]:
    x = np.c_[np.ones(len(d)), d["air"], d["solar"]]
    beta, *_ = np.linalg.lstsq(x, d["track"].to_numpy(float), rcond=None)
    return tuple(float(b) for b in beta)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Fit and score the track-temperature model.")
    p.add_argument("--train-until", type=int, default=2024)
    args = p.parse_args(argv)
    d = load_data()
    train, test = d[d["season"] <= args.train_until], d[d["season"] > args.train_until].copy()
    coefs = fit(train)
    print(f"fitted on {len(train)} races: track = %.2f + %.2f * air + %.4f * solar" % coefs)

    def earlier_median(r):
        prev = d[(d["circuit_id"] == r.circuit_id) & (d["start_utc"] < r.start_utc)]
        return prev["track"].median() if len(prev) else np.nan

    test["circuit_median"] = test.apply(earlier_median, axis=1)
    test["forecast_3d"] = predict(test["fc_air"], test["fc_solar"], coefs)
    test["climate"] = predict(test["clim_air"], test["clim_solar"], coefs)
    ok = test.dropna(subset=["circuit_median", "forecast_3d", "climate"])
    for k in ("circuit_median", "climate", "forecast_3d"):
        print(f"{k:<15} mean error {(ok[k] - ok['track']).abs().mean():.1f} C  (n={len(ok)})")


if __name__ == "__main__":
    main()
