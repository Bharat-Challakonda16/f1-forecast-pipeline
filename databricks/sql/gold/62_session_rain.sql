-- Rain for the competitive sessions (qualifying, sprint qualifying, sprint, race), as the
-- inputs of the wet-session model (features/rain.py):
--   session_rain           one row per session (2001+, including upcoming ones): its time
--                          window, whether it was wet (2018+: wet tyres on >= 10% of laps)
--                          and the rain measured in the hourly weather history.
--   session_rain_climate   the same UTC window on nearby days (+-7) of every earlier year at
--                          that circuit: the climate the wet chance starts from.
--   session_rain_forecast  the forecast rain in the window, per forecast issue and lead time
--                          (past forecasts 2024+, and daily live snapshots).
-- Window: one hour before the start to the typical end (race 2 h, others 1 h), because
-- rain just before a session leaves the track wet.

CREATE OR REPLACE TABLE ${catalog}.gold.session_rain
COMMENT 'Per competitive session (Q, SQ, S, R; 2001+ incl. upcoming): window, wet flag (2018+, wet tyres on >= 10% of laps) and rain in the hourly weather history (mm)'
AS
WITH tyres AS (
  SELECT session_id, avg(CAST(is_wet_tyre AS INT)) AS wet_tyre_share, count(*) AS n_laps
  FROM ${catalog}.silver.laps
  WHERE season >= 2018 AND compound IS NOT NULL
  GROUP BY session_id
),
s AS (
  SELECT
    s.session_id, s.season, s.round, s.session_code, e.circuit_id, e.race_date,
    s.start_utc,
    s.start_utc - INTERVAL 1 HOUR AS window_start_utc,
    s.start_utc + CASE WHEN s.session_code = 'R' THEN INTERVAL 2 HOURS ELSE INTERVAL 1 HOUR END
      AS window_end_utc,
    t.wet_tyre_share,
    CASE WHEN t.n_laps >= 100 THEN t.wet_tyre_share >= 0.1 END AS is_wet
  FROM ${catalog}.silver.sessions s
  JOIN ${catalog}.silver.events e ON e.season = s.season AND e.round = s.round
  LEFT JOIN tyres t ON t.session_id = s.session_id
  WHERE s.session_code IN ('Q', 'SQ', 'S', 'R') AND NOT coalesce(s.is_cancelled, false)
    AND e.status <> 'cancelled' AND s.start_utc IS NOT NULL
),
-- whole UTC hours of the window (equality joins are much faster than range joins)
hours AS (
  SELECT session_id, circuit_id,
         explode(sequence(date_trunc('HOUR', window_start_utc),
                          date_trunc('HOUR', window_end_utc - INTERVAL 1 SECOND),
                          INTERVAL 1 HOUR)) AS time_utc
  FROM s
),
measured AS (
  SELECT x.session_id, sum(h.precipitation_mm) AS rain_mm, count(*) AS hours
  FROM hours x JOIN ${catalog}.silver.weather_hourly h USING (circuit_id, time_utc)
  GROUP BY x.session_id
)
SELECT s.*, CASE WHEN m.hours >= 2 THEN m.rain_mm END AS rain_mm
FROM s LEFT JOIN measured m USING (session_id);

CREATE OR REPLACE TABLE ${catalog}.gold.session_rain_climate
COMMENT 'Per session: rain (mm) in the same UTC window on days -7..+7 around the session date in every earlier year (2001+) at the circuit'
CLUSTER BY (session_id)
AS
WITH s AS (
  SELECT session_id, season, circuit_id,
         date_trunc('HOUR', window_start_utc) AS w0,
         (unix_seconds(window_end_utc) - 1 - unix_seconds(date_trunc('HOUR', window_start_utc)))
           DIV 3600 AS last_hour
  FROM ${catalog}.gold.session_rain
  -- 29 February has no twin in most years
  WHERE NOT (month(window_start_utc) = 2 AND day(window_start_utc) = 29)
),
samples AS (
  SELECT s.session_id, s.circuit_id, s.last_hour, y.sample_year, d.day_offset,
         make_timestamp(y.sample_year, month(s.w0), day(s.w0), hour(s.w0), 0, 0, 'UTC')
           + make_interval(0, 0, 0, d.day_offset) AS t0
  FROM s
  CROSS JOIN (SELECT explode(sequence(2001, 2030)) AS sample_year) y
  CROSS JOIN (SELECT explode(sequence(-7, 7)) AS day_offset) d
  WHERE y.sample_year < s.season
),
hours AS (
  SELECT session_id, circuit_id, sample_year, day_offset, last_hour,
         t0 + make_interval(0, 0, 0, 0, i) AS time_utc
  FROM samples LATERAL VIEW explode(sequence(0, last_hour)) AS i
)
SELECT x.session_id, x.sample_year, x.day_offset, sum(h.precipitation_mm) AS rain_mm
FROM hours x
JOIN ${catalog}.silver.weather_hourly h USING (circuit_id, time_utc)
GROUP BY x.session_id, x.sample_year, x.day_offset
HAVING count(*) = max(x.last_hour) + 1;

CREATE OR REPLACE TABLE ${catalog}.gold.session_rain_forecast
COMMENT 'Per session and forecast issue: forecast rain (mm) in the session window. source previous_runs (2024+): issued_date = session day - lead_days. source live: daily snapshots (issued_utc). max_prob_pct = highest hourly precipitation probability (live only)'
AS
SELECT
  s.session_id, s.season, s.round, s.session_code, f.source, f.issued_utc, f.lead_days,
  date_sub(to_date(s.start_utc), f.lead_days) AS issued_date,
  sum(f.precipitation_mm) AS rain_mm,
  max(f.precipitation_prob_pct) AS max_prob_pct,
  avg(f.temp_c) AS temp_c,
  avg(f.solar_radiation_wm2) AS solar_radiation_wm2,
  avg(f.cloud_cover_pct) AS cloud_cover_pct,
  count(*) AS hours
FROM (
  SELECT *, explode(sequence(date_trunc('HOUR', window_start_utc),
                             date_trunc('HOUR', window_end_utc - INTERVAL 1 SECOND),
                             INTERVAL 1 HOUR)) AS time_utc
  FROM ${catalog}.gold.session_rain
  WHERE season >= 2024
) s
JOIN ${catalog}.silver.weather_forecasts f USING (season, round, time_utc)
GROUP BY ALL
HAVING count(*) >= 2;
