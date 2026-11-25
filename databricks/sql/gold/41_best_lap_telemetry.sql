-- Speed traces of every driver's fastest qualifying lap (2018+): input for the corner-speed
-- analysis (features/corners.py). ~4,000 laps x ~350 samples, extracted once per build
-- from the full telemetry so downstream jobs never scan the 440M-row tables.

CREATE OR REPLACE TABLE ${catalog}.gold.quali_best_lap_telemetry
COMMENT 'Speed samples (~4 Hz) of each driver''s fastest qualifying lap per session, 2018+; t_s = seconds from lap start'
CLUSTER BY (session_id)
AS
WITH best AS (
  SELECT
    l.session_id, l.season, l.round, l.driver_id, l.car_number, l.lap,
    l.lap_time_ms, l.lap_start_utc,
    l.lap_start_utc + make_interval(0, 0, 0, 0, 0, 0, l.lap_time_ms / 1000.0) AS lap_end_utc
  FROM ${catalog}.silver.laps l
  WHERE l.session_code = 'Q' AND l.season >= 2018
    AND l.lap_time_ms IS NOT NULL AND l.lap_start_utc IS NOT NULL
    AND NOT coalesce(l.is_deleted, false) AND l.lap_time_flag IS NULL
  QUALIFY row_number() OVER (PARTITION BY l.session_id, l.driver_id ORDER BY l.lap_time_ms) = 1
)
SELECT
  b.session_id, b.season, b.round, b.driver_id, e.constructor_id, b.lap_time_ms,
  (unix_millis(t.sample_utc) - unix_millis(b.lap_start_utc)) / 1000.0 AS t_s,
  t.speed_kph
FROM best b
JOIN ${catalog}.silver.car_telemetry t
  ON t.session_id = b.session_id AND t.car_number = b.car_number
 AND t.sample_utc BETWEEN b.lap_start_utc AND b.lap_end_utc
LEFT JOIN ${catalog}.silver.entries e
  ON e.season = b.season AND e.round = b.round AND e.driver_id = b.driver_id
WHERE t.speed_kph IS NOT NULL;
