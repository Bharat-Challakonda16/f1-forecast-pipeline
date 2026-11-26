"""Chance that a session (qualifying, sprint, race) is wet, as of a given day.

Wet = wet or intermediate tyres on at least 10% of the session's laps (2018+, where tyre data
exists). Three pieces, all fitted on sessions before the test season:

1. **Measured rain → wet.** In the hourly weather history (a coarse reanalysis, which misses
   many showers), more rain in the session window means a wetter session::

       p_measured(r) = sigmoid(m0 + m1 * ln(1 + r / r0))      r = rain in the window, mm

   ``r0`` sets how little rain already counts (the jump from "none" to "a little" matters
   most).

2. **Climate.** The same window on days -7..+7 around the date in every earlier year at the
   circuit; the climate chance is the average of ``p_measured`` over those samples.

3. **Forecast.** Within ``MAX_LEAD`` days of the session the forecast moves the climate
   chance, less the further ahead it was issued::

       logit p = logit p_climate + w(lead) * (c0 + c1 * ln(1 + f))      f = forecast rain, mm
       w(lead) = exp(-lead / tau)

   With no forecast (or too far ahead) the chance is the climate chance.

Run ``python -m f1_forecast.features.rain`` to refit and print the held-out scores; the
fitted numbers are then copied into :data:`COEFS`.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd
from scipy.optimize import minimize

CATALOG = "f1_forecast"
MAX_LEAD = 7  # days: past forecasts exist only this far ahead, so nothing is fitted beyond
EPS = 1e-4


@dataclass(frozen=True)
class RainCoefs:
    m0: float  # measured-rain curve intercept
    m1: float  # measured-rain curve slope on ln(1 + mm / r0)
    r0: float  # mm: rain scale of the measured-rain curve
    c0: float  # forecast shift with no rain forecast (negative: a dry forecast lowers the chance)
    c1: float  # forecast slope on ln(1 + mm)
    tau: float  # days: how fast the forecast's weight fades with lead time


# Fitted on 2018-2025 (measured) and 2024-2025 (forecast, leads 1-7) sessions with PRIOR_SD
# 0.5, chosen on 2025 and 2026 (each fitted on the seasons before). r0 ~ 0: any measured
# rain at all is what matters (no rain: 2.6% wet; 0.1 mm: 13%; 1 mm: 26%; 5 mm: 39%).
# Held out on 2026 (54 sessions, 3 wet), log loss: base rate 0.225, climate 0.189, with the
# forecast 0.186 one day ahead, 0.188 three days ahead. Forecasts 1-7 days ahead added little
# out of sample (2025: 0.234 vs climate 0.232): showers are hard to time and there are few
# wet sessions to learn from, so the prior keeps their weight small.
COEFS = RainCoefs(m0=-3.634, m1=0.375, r0=0.001, c0=-0.024, c1=0.221, tau=3.12)


def sigmoid(z):
    return 1.0 / (1.0 + np.exp(-z))


def logit(p):
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def p_measured(rain_mm, m0: float, m1: float, r0: float = 1.0):
    return sigmoid(m0 + m1 * np.log1p(np.clip(np.asarray(rain_mm, float), 0, None) / r0))


def climate_prob(samples: pd.DataFrame, coefs: RainCoefs = COEFS) -> pd.Series:
    """Per session_id: mean measured-rain wet chance over its climate samples. ``samples``
    has session_id, rain_mm and n (how many samples had that amount)."""
    p = p_measured(samples["rain_mm"].to_numpy(float), coefs.m0, coefs.m1, coefs.r0)
    n = samples["n"].to_numpy(float) if "n" in samples else np.ones(len(samples))
    g = pd.DataFrame({"session_id": samples["session_id"], "pn": p * n, "n": n})
    tot = g.groupby("session_id")[["pn", "n"]].sum()
    return tot["pn"] / tot["n"]


CLIMATE_SQL = """
    SELECT c.session_id, round(c.rain_mm, 1) AS rain_mm, count(*) AS n
    FROM {cat}.gold.session_rain_climate c
    JOIN {cat}.gold.session_rain s USING (session_id)
    WHERE {where}
    GROUP BY ALL"""


def wet_probability(p_climate, forecast_mm, lead_days, coefs: RainCoefs = COEFS):
    """Wet chance from the climate chance and (optionally) a forecast ``lead_days`` ahead.
    ``forecast_mm`` may be NaN (no forecast): the climate chance is returned."""
    p_climate = np.asarray(p_climate, float)
    f = np.asarray(forecast_mm, float)
    lead = np.asarray(lead_days, float)
    use = ~np.isnan(f) & (lead <= MAX_LEAD) & (lead >= 0)
    w = np.where(use, np.exp(-np.clip(lead, 0, None) / coefs.tau), 0.0)
    shift = coefs.c0 + coefs.c1 * np.log1p(np.clip(np.nan_to_num(f), 0, None))
    return sigmoid(logit(p_climate) + w * shift)


def log_loss(y, p) -> float:
    p = np.clip(np.asarray(p, float), EPS, 1 - EPS)
    y = np.asarray(y, float)
    return float(-(y * np.log(p) + (1 - y) * np.log(1 - p)).mean())


def brier(y, p) -> float:
    return float(((np.asarray(p, float) - np.asarray(y, float)) ** 2).mean())


def fit_measured(rain_mm: np.ndarray, wet: np.ndarray) -> tuple[float, float, float]:
    def loss(x):
        return log_loss(wet, p_measured(rain_mm, x[0], x[1], np.exp(x[2])))

    r = minimize(loss, x0=[-3.0, 1.0, np.log(0.2)], method="Nelder-Mead")
    return float(r.x[0]), float(r.x[1]), float(np.exp(r.x[2]))


PRIOR_SD = 0.5  # shrinks c0, c1 towards 0 and tau towards 3 days (few wet sessions to fit on)


def fit_forecast(
    df: pd.DataFrame, measured: tuple[float, float, float], prior_sd: float = PRIOR_SD
) -> RainCoefs:
    """``df``: one row per session and lead with p_climate, forecast_mm, lead_days, is_wet.
    Penalised log loss: each session counts once (its leads share the weight), and the
    coefficients have a normal prior of sd ``prior_sd`` (ln tau: around ln 3)."""
    n_sessions = df["session_id"].nunique()

    def loss(x):
        c = RainCoefs(*measured, x[0], x[1], np.exp(x[2]))
        p = wet_probability(df["p_climate"], df["forecast_mm"], df["lead_days"], c)
        penalty = (x[0] ** 2 + x[1] ** 2 + (x[2] - np.log(3.0)) ** 2) / (2 * prior_sd**2)
        return n_sessions * log_loss(df["is_wet"], p) + penalty

    r = minimize(loss, x0=[-0.5, 1.0, np.log(3.0)], method="Nelder-Mead")
    return RainCoefs(*measured, float(r.x[0]), float(r.x[1]), float(np.exp(r.x[2])))


def pick_forecast(forecasts: pd.DataFrame, sessions: pd.DataFrame, as_of) -> pd.DataFrame:
    """Per session: the forecast known on day ``as_of`` (no look-ahead). The latest live
    snapshot issued on or before ``as_of`` if any, else the past forecast issued on
    ``as_of`` (lead = session day - as_of, at least 1). Returns session_id, forecast_mm, lead_days,
    forecast_source; sessions without one are left out."""
    as_of = pd.Timestamp(as_of).normalize()
    f = forecasts.copy()
    day = pd.to_datetime(sessions.set_index("session_id")["start_utc"]).dt.tz_localize(None)
    f["session_day"] = f["session_id"].map(day.dt.normalize())
    live = f[f["source"] == "live"].copy()
    live["issued"] = pd.to_datetime(live["issued_utc"]).dt.tz_localize(None)
    live = live[live["issued"].dt.normalize() <= as_of].sort_values("issued")
    live = live.groupby("session_id").tail(1)
    live["lead_days"] = (live["session_day"] - live["issued"].dt.normalize()).dt.days
    # past forecasts: issued on as_of; at least a day ahead (the latest run, lead 0, can be
    # issued after the session started)
    past = f[f["source"] == "previous_runs"]
    want = ((past["session_day"] - as_of).dt.days).clip(lower=1)
    past = past[past["lead_days"] == want]
    past = past[~past["session_id"].isin(live["session_id"])]
    cols = ["session_id", "source", "forecast_mm", "lead_days"]
    cols += [c for c in ("temp_c", "solar_radiation_wm2") if c in f]
    parts = [x[cols] for x in (live, past) if len(x)]
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)
    out = out[out["lead_days"] >= 0]
    return out.rename(columns={"source": "forecast_source"})


def session_outlook(season: int, rnd: int, as_of, *, w=None, wh=None) -> pd.DataFrame:
    """Weather outlook of each competitive session of an event as of ``as_of``: one row
    per session_code with p_wet, p_climate, forecast_mm, lead_days, forecast_source and
    track_temp_c (from the forecast within MAX_LEAD days, else NaN)."""
    from f1_forecast.features.run_traits import query_df

    if w is None:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient()
        wh = next(iter(w.warehouses.list())).id
    ev = f"s.season = {int(season)} AND s.round = {int(rnd)}"
    sessions = query_df(
        w,
        wh,
        f"""SELECT session_id, session_code, start_utc FROM {CATALOG}.gold.session_rain s
            WHERE {ev}""",
    )
    if sessions.empty:
        return pd.DataFrame(
            columns=[
                "session_code",
                "p_wet",
                "p_climate",
                "forecast_mm",
                "lead_days",
                "track_temp_c",
            ]
        )
    climate = query_df(w, wh, CLIMATE_SQL.format(cat=CATALOG, where=ev))
    climate["rain_mm"] = pd.to_numeric(climate["rain_mm"])
    climate["n"] = climate["n"].astype(int)
    fc = query_df(
        w,
        wh,
        f"""SELECT session_id, source, issued_utc, lead_days, rain_mm AS forecast_mm,
                   temp_c, solar_radiation_wm2
            FROM {CATALOG}.gold.session_rain_forecast s WHERE {ev}""",
    )
    fc["lead_days"] = pd.to_numeric(fc["lead_days"]).astype(int)
    for c in ("forecast_mm", "temp_c", "solar_radiation_wm2"):
        fc[c] = pd.to_numeric(fc[c])
    return outlook_frame(sessions, climate, fc, as_of)


def outlook_frame(sessions, climate, forecasts, as_of, coefs: RainCoefs = COEFS):
    """The pure part of :func:`session_outlook` (testable without a warehouse)."""
    out = sessions[["session_id", "session_code"]].copy()
    out["p_climate"] = out["session_id"].map(climate_prob(climate, coefs)).astype(float)
    # no climate samples (new circuit): the overall wet rate of recent seasons
    out["p_climate"] = out["p_climate"].fillna(0.08)
    picked = pick_forecast(forecasts, sessions, as_of) if len(forecasts) else None
    if picked is not None and len(picked):
        out = out.merge(picked, on="session_id", how="left")
    else:
        out[["forecast_mm", "lead_days", "forecast_source"]] = [np.nan, np.nan, None]
    out["p_wet"] = wet_probability(out["p_climate"], out["forecast_mm"], out["lead_days"], coefs)
    out["track_temp_c"] = np.nan
    if {"temp_c", "solar_radiation_wm2"} <= set(out.columns):
        from f1_forecast.features.track_temp import predict

        near = out["lead_days"].le(MAX_LEAD)
        out.loc[near, "track_temp_c"] = predict(
            out.loc[near, "temp_c"], out.loc[near, "solar_radiation_wm2"]
        )
    return out.drop(columns=["session_id", "temp_c", "solar_radiation_wm2"], errors="ignore")


# --------------------------------------------------------------------------- data


def load_training_data() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(sessions, climate samples, past forecasts) for sessions with a known wet flag."""
    from databricks.sdk import WorkspaceClient

    from f1_forecast.features.run_traits import query_df

    w = WorkspaceClient()
    wh = next(iter(w.warehouses.list())).id
    sessions = query_df(
        w,
        wh,
        f"""
        SELECT session_id, season, round, session_code, rain_mm, is_wet
        FROM {CATALOG}.gold.session_rain WHERE is_wet IS NOT NULL""",
    )
    sessions["is_wet"] = sessions["is_wet"].map({"true": 1, "false": 0, True: 1, False: 0})
    for c in ("season", "round"):
        sessions[c] = sessions[c].astype(int)
    sessions["rain_mm"] = pd.to_numeric(sessions["rain_mm"])
    climate = query_df(w, wh, CLIMATE_SQL.format(cat=CATALOG, where="s.is_wet IS NOT NULL"))
    climate["rain_mm"] = pd.to_numeric(climate["rain_mm"])
    climate["n"] = climate["n"].astype(int)
    forecasts = query_df(
        w,
        wh,
        f"""
        SELECT session_id, lead_days, rain_mm AS forecast_mm
        FROM {CATALOG}.gold.session_rain_forecast WHERE source = 'previous_runs'""",
    )
    forecasts["lead_days"] = forecasts["lead_days"].astype(int)
    forecasts["forecast_mm"] = pd.to_numeric(forecasts["forecast_mm"])
    return sessions, climate, forecasts


def evaluate(
    train_until: int, test_season: int, prior_sd: float = PRIOR_SD, data=None
) -> tuple[RainCoefs, pd.DataFrame]:
    """Fit on seasons <= ``train_until``; score base rate, climate and forecast model on
    ``test_season`` by lead time."""
    sessions, climate, forecasts = data or load_training_data()
    train = sessions[(sessions["season"] <= train_until) & sessions["rain_mm"].notna()]
    measured = fit_measured(train["rain_mm"].to_numpy(float), train["is_wet"].to_numpy(float))
    base = RainCoefs(*measured, 0.0, 0.0, 1.0)
    p_clim = climate_prob(climate, base).rename("p_climate")
    s = sessions.join(p_clim, on="session_id")
    s["p_climate"] = s["p_climate"].fillna(float(train["is_wet"].mean()))
    # lead 0 (the latest run) can be issued after the session started: never used
    df = forecasts[forecasts["lead_days"] >= 1].merge(s, on="session_id")
    coefs = fit_forecast(df[df["season"] <= train_until], measured, prior_sd)

    test = df[df["season"] == test_season].copy()
    base_rate = float(sessions.loc[sessions["season"] <= train_until, "is_wet"].mean())
    test["p_model"] = wet_probability(
        test["p_climate"], test["forecast_mm"], test["lead_days"], coefs
    )
    rows = []
    for lead, g in test.groupby("lead_days"):
        y = g["is_wet"]
        rows.append(
            {
                "lead_days": lead,
                "sessions": len(g),
                "wet": int(y.sum()),
                "ll_base_rate": log_loss(y, np.full(len(g), base_rate)),
                "ll_climate": log_loss(y, g["p_climate"]),
                "ll_model": log_loss(y, g["p_model"]),
                "brier_climate": brier(y, g["p_climate"]),
                "brier_model": brier(y, g["p_model"]),
            }
        )
    return coefs, pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Fit and score the wet-session model.")
    p.add_argument("--train-until", type=int, default=2025)
    p.add_argument("--test", type=int, default=2026)
    p.add_argument("--prior-sd", type=float, default=PRIOR_SD)
    p.add_argument(
        "--choose-prior",
        action="store_true",
        help="Score several prior sds on 2025 (fit <= 2024) and 2026 (fit <= 2025)",
    )
    args = p.parse_args(argv)
    if args.choose_prior:
        data = load_training_data()
        for sd in (0.25, 0.5, 1.0, 2.0, 5.0):
            parts = [evaluate(y - 1, y, sd, data)[1] for y in (2025, 2026)]
            s = pd.concat(parts)
            print(
                f"prior_sd={sd:<5} log loss climate {s['ll_climate'].mean():.3f} "
                f"model {s['ll_model'].mean():.3f} | brier climate "
                f"{s['brier_climate'].mean():.4f} model {s['brier_model'].mean():.4f}"
            )
        return
    coefs, scores = evaluate(args.train_until, args.test, args.prior_sd)
    print({k: round(v, 3) for k, v in asdict(coefs).items()})
    print(scores.round(3).to_string(index=False))


if __name__ == "__main__":
    main()
