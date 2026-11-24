-- Weather: trackside session weather (FastF1 2018-22, OpenF1 2023+) and hourly history
-- per circuit (Open-Meteo, 2001+).

CREATE OR REPLACE TABLE ${catalog}.silver.session_weather
COMMENT 'Trackside weather samples (about one per minute) during each session'
AS
WITH ff_t0 AS (
  -- FastF1 weather times are relative to the session clock; laps give the clock offset.
  SELECT
    _year, _round, _session,
    min(unix_millis(try_cast(left(LapStartDate, 23) AS TIMESTAMP)) - bigint(double(LapStartTime) * 1000)) AS t0_ms
  FROM ${catalog}.bronze.fastf1_laps
  WHERE LapStartDate IS NOT NULL AND LapStartTime IS NOT NULL
  GROUP BY ALL
),
ff AS (
  SELECT
    ${catalog}.silver.session_id(int(w._year), int(w._round), ${catalog}.silver.session_code(w._session)) AS session_id,
    timestamp_millis(t.t0_ms + bigint(double(w.Time) * 1000)) AS sample_utc,
    double(w.AirTemp) AS air_temp_c, double(w.TrackTemp) AS track_temp_c,
    double(w.Humidity) AS humidity_pct, double(w.Pressure) AS pressure_mbar,
    try_cast(w.Rainfall AS BOOLEAN) AS rainfall,
    double(w.WindSpeed) AS wind_speed_ms, double(w.WindDirection) AS wind_direction_deg,
    'fastf1' AS source
  FROM ${catalog}.bronze.fastf1_weather w
  JOIN ff_t0 t USING (_year, _round, _session)
),
of AS (
  SELECT
    s.session_id,
    try_cast(w.date AS TIMESTAMP),
    double(w.air_temperature), double(w.track_temperature),
    double(w.humidity), double(w.pressure),
    double(w.rainfall) > 0,
    double(w.wind_speed), double(w.wind_direction),
    'openf1'
  FROM ${catalog}.bronze.openf1_weather w
  JOIN ${catalog}.silver.sessions s ON s.openf1_session_key = int(w.session_key)
)
SELECT * FROM ff
UNION ALL
SELECT * FROM of o WHERE o.session_id NOT IN (SELECT DISTINCT session_id FROM ff);

CREATE OR REPLACE TABLE ${catalog}.silver.weather_hourly
COMMENT 'Hourly weather history at each circuit (Open-Meteo archive, UTC)'
CLUSTER BY (circuit_id, time_utc)
AS
SELECT
  circuit_ref AS circuit_id,
  to_timestamp(time, "yyyy-MM-dd'T'HH:mm") AS time_utc,
  double(temperature_2m) AS temp_c,
  double(relative_humidity_2m) AS humidity_pct,
  double(precipitation) AS precipitation_mm,
  double(rain) AS rain_mm,
  double(cloud_cover) AS cloud_cover_pct,
  double(shortwave_radiation) AS solar_radiation_wm2,
  double(wind_speed_10m) AS wind_speed_kmh,
  double(wind_direction_10m) AS wind_direction_deg,
  double(wind_gusts_10m) AS wind_gusts_kmh,
  double(surface_pressure) AS pressure_hpa,
  int(weather_code) AS weather_code
FROM ${catalog}.bronze.openmeteo_archive;
