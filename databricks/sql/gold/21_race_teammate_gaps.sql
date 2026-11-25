-- Teammate comparisons on race day (dry races only), inputs to the Bayesian trait models.
--   race_pace   : median gap on laps where BOTH teammates were clean (same fuel, same track state)
--   consistency : spread of each driver's clean laps around their local pace
--   tyre_wear   : lap-time loss per 10 laps of tyre age on the same compound (2018+)

CREATE OR REPLACE TABLE ${catalog}.gold.race_teammate_gaps
COMMENT 'Teammate race pace: gap_pct = 100*median(ln(t_a/t_b)) over laps both drivers ran clean (positive = driver_a slower); dry races, >= 10 shared clean laps'
AS
SELECT
  a.session_id, a.season, a.round, a.constructor_id,
  a.driver_id AS driver_a, b.driver_id AS driver_b,
  count(*) AS shared_clean_laps,
  100 * percentile(ln(CAST(a.lap_time_ms AS DOUBLE) / b.lap_time_ms), 0.5) AS gap_pct
FROM ${catalog}.gold.race_laps a
JOIN ${catalog}.gold.race_laps b
  ON a.session_id = b.session_id AND a.lap = b.lap
 AND a.constructor_id = b.constructor_id AND a.driver_id < b.driver_id
JOIN ${catalog}.gold.race_conditions c ON c.session_id = a.session_id
WHERE a.is_clean AND b.is_clean AND NOT coalesce(c.is_wet, false)
GROUP BY ALL
HAVING count(*) >= 10;

CREATE OR REPLACE TABLE ${catalog}.gold.consistency_teammate_gaps
COMMENT 'Teammate consistency: gap_pct = 100*ln(spread_a/spread_b), spread = median absolute deviation of clean laps from the 5-lap local mean (positive = driver_a less consistent); dry races, >= 15 clean laps each'
AS
WITH resid AS (
  SELECT
    session_id, season, round, constructor_id, driver_id,
    ln(CAST(lap_time_ms AS DOUBLE)) - avg(ln(CAST(lap_time_ms AS DOUBLE))) OVER (
      PARTITION BY session_id, driver_id ORDER BY lap ROWS BETWEEN 2 PRECEDING AND 2 FOLLOWING
    ) AS r
  FROM ${catalog}.gold.race_laps
  WHERE is_clean
),
per_driver AS (
  SELECT session_id, season, round, constructor_id, driver_id,
         count(*) AS clean_laps, percentile(abs(r), 0.5) AS spread
  FROM resid GROUP BY ALL
  HAVING count(*) >= 15
)
SELECT
  a.session_id, a.season, a.round, a.constructor_id,
  a.driver_id AS driver_a, b.driver_id AS driver_b,
  least(a.clean_laps, b.clean_laps) AS clean_laps,
  100 * ln(a.spread / b.spread) AS gap_pct
FROM per_driver a
JOIN per_driver b
  ON a.session_id = b.session_id AND a.constructor_id = b.constructor_id AND a.driver_id < b.driver_id
JOIN ${catalog}.gold.race_conditions c ON c.session_id = a.session_id
WHERE NOT coalesce(c.is_wet, false) AND a.spread > 0 AND b.spread > 0;

CREATE OR REPLACE TABLE ${catalog}.gold.tyre_wear_teammate_gaps
COMMENT 'Teammate tyre wear: gap = difference in lap-time loss (% of lap per 10 laps of tyre age, fuel-corrected) on the same compound (positive = driver_a wears tyres faster); dry races 2018+'
AS
WITH stints AS (
  -- Slope of fuel-corrected lap time against tyre age within each stint.
  -- Burning fuel makes cars ~0.035% faster per lap; adding it back isolates tyre wear.
  SELECT
    session_id, season, round, constructor_id, driver_id, stint, compound,
    count(*) AS laps,
    regr_slope(100 * ln(CAST(lap_time_ms AS DOUBLE)) + 0.035 * lap, tyre_life) * 10 AS wear_pct_per_10
  FROM ${catalog}.gold.race_laps
  WHERE is_clean AND season >= 2018 AND tyre_life IS NOT NULL
    AND compound IN ('HYPERSOFT', 'ULTRASOFT', 'SUPERSOFT', 'SOFT', 'MEDIUM', 'HARD')
  GROUP BY ALL
  HAVING count(*) >= 8
),
per_compound AS (
  SELECT session_id, season, round, constructor_id, driver_id, compound,
         sum(laps) AS laps, sum(wear_pct_per_10 * laps) / sum(laps) AS wear_pct_per_10
  FROM stints GROUP BY ALL
)
SELECT
  a.session_id, a.season, a.round, a.constructor_id, a.compound,
  a.driver_id AS driver_a, b.driver_id AS driver_b,
  least(a.laps, b.laps) AS laps,
  a.wear_pct_per_10 - b.wear_pct_per_10 AS gap_pct
FROM per_compound a
JOIN per_compound b
  ON a.session_id = b.session_id AND a.constructor_id = b.constructor_id
 AND a.compound = b.compound AND a.driver_id < b.driver_id
JOIN ${catalog}.gold.race_conditions c ON c.session_id = a.session_id
WHERE NOT coalesce(c.is_wet, false);
