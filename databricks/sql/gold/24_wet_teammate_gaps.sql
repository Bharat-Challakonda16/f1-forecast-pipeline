-- Wet-weather teammate gaps (input to the wet-skill trait; the dry gap is subtracted at
-- fit time using the race_pace / quali_pace traits).
--   2018+ races   : laps where BOTH teammates were on intermediates/wets, judged against
--                   each driver's own wet-tyre pace that race (in mixed races every wet lap
--                   would fail a 107% rule based on dry laps)
--   2001-2017     : races flagged is_wet_confident, normal clean laps
--   2018+ quali   : sessions where most laps were on wet tyres

CREATE OR REPLACE TABLE ${catalog}.gold.wet_teammate_gaps
COMMENT 'Teammate gaps in wet conditions: gap_pct = 100*median(ln(t_a/t_b)) (positive = driver_a slower); dry_trait says which dry trait to subtract (race_pace or quali_pace)'
AS
WITH wet_laps_2018 AS (
  SELECT l.*,
    percentile(l.lap_time_ms, 0.5) OVER (PARTITION BY l.session_id, l.driver_id) AS wet_median_ms
  FROM ${catalog}.gold.race_laps l
  JOIN ${catalog}.silver.laps s
    ON s.session_id = l.session_id AND s.driver_id = l.driver_id AND s.lap = l.lap
  WHERE l.season >= 2018 AND s.is_wet_tyre
    AND l.lap > 1 AND NOT l.is_neutralised AND NOT l.is_in_lap AND NOT l.is_out_lap
),
usable_2018 AS (
  SELECT * FROM wet_laps_2018
  WHERE lap_time_ms <= 1.07 * wet_median_ms
    AND (gap_ahead_ms IS NULL OR gap_ahead_ms >= 1500)
),
usable_pre AS (
  SELECT l.*
  FROM ${catalog}.gold.race_laps l
  JOIN ${catalog}.gold.race_conditions c ON c.session_id = l.session_id
  WHERE l.season < 2018 AND c.is_wet_confident AND l.is_clean
),
race_laps_wet AS (
  SELECT session_id, season, round, constructor_id, driver_id, lap, lap_time_ms FROM usable_2018
  UNION ALL
  SELECT session_id, season, round, constructor_id, driver_id, lap, lap_time_ms FROM usable_pre
),
race_gaps AS (
  SELECT
    a.session_id, a.season, a.round, a.constructor_id,
    a.driver_id AS driver_a, b.driver_id AS driver_b,
    count(*) AS shared_laps,
    100 * percentile(ln(CAST(a.lap_time_ms AS DOUBLE) / b.lap_time_ms), 0.5) AS gap_pct,
    'race_pace' AS dry_trait
  FROM race_laps_wet a
  JOIN race_laps_wet b
    ON a.session_id = b.session_id AND a.lap = b.lap
   AND a.constructor_id = b.constructor_id AND a.driver_id < b.driver_id
  GROUP BY ALL
  HAVING count(*) >= 5
),
wet_quali AS (
  SELECT session_id
  FROM ${catalog}.silver.laps
  WHERE session_code = 'Q' AND season >= 2018 AND compound IS NOT NULL
  GROUP BY session_id
  HAVING avg(CAST(is_wet_tyre AS INT)) >= 0.5
),
quali_gaps AS (
  SELECT g.session_id, g.season, g.round, g.constructor_id, g.driver_a, g.driver_b,
         1 AS shared_laps, g.gap_pct, 'quali_pace' AS dry_trait
  FROM ${catalog}.gold.quali_teammate_gaps g
  JOIN wet_quali w USING (session_id)
  WHERE NOT g.is_outlier
)
SELECT * FROM race_gaps
UNION ALL
SELECT * FROM quali_gaps;
