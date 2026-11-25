-- Season-to-date corner-speed profile per constructor (from gold.corner_speeds, written by
-- features/run_corners.py). The table is created empty here if the Python step hasn't run.

CREATE TABLE IF NOT EXISTS ${catalog}.gold.corner_speeds (
  session_id STRING, season INT, round INT, constructor_id STRING, corner_class STRING,
  kph_vs_field DOUBLE, corners INT, computed_at TIMESTAMP
) COMMENT 'Per quali session and team: apex speed vs field (kph) by corner class';

CREATE OR REPLACE TABLE ${catalog}.gold.car_corner_profiles
COMMENT 'Per constructor, season and round (season-to-date): mean apex speed vs field median in low (<130 kph), medium (<210) and high-speed braking corners, weighted by corners measured. Positive = faster through that corner type.'
AS
WITH per_round AS (
  SELECT
    constructor_id, season, round,
    sum(CASE WHEN corner_class = 'low' THEN kph_vs_field * corners END) AS low_w,
    sum(CASE WHEN corner_class = 'low' THEN corners END) AS low_n,
    sum(CASE WHEN corner_class = 'medium' THEN kph_vs_field * corners END) AS med_w,
    sum(CASE WHEN corner_class = 'medium' THEN corners END) AS med_n,
    sum(CASE WHEN corner_class = 'high' THEN kph_vs_field * corners END) AS high_w,
    sum(CASE WHEN corner_class = 'high' THEN corners END) AS high_n
  FROM ${catalog}.gold.corner_speeds
  GROUP BY ALL
)
SELECT
  constructor_id, season, round AS as_of_round,
  sum(low_w) OVER s2d / sum(low_n) OVER s2d AS low_speed_corner_kph,
  sum(med_w) OVER s2d / sum(med_n) OVER s2d AS medium_speed_corner_kph,
  sum(high_w) OVER s2d / sum(high_n) OVER s2d AS high_speed_corner_kph,
  sum(low_n) OVER s2d AS low_corners,
  sum(med_n) OVER s2d AS medium_corners,
  sum(high_n) OVER s2d AS high_corners
FROM per_round
WINDOW s2d AS (PARTITION BY constructor_id, season ORDER BY round ROWS UNBOUNDED PRECEDING);
