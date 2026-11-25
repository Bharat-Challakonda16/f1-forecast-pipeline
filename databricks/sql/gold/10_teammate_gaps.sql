-- Model inputs for driver traits: head-to-head gaps between teammates.
-- Teammates share a car, so their gap is (mostly) the driver.

CREATE SCHEMA IF NOT EXISTS ${catalog}.gold;

CREATE OR REPLACE TABLE ${catalog}.gold.quali_teammate_gaps
COMMENT 'One row per teammate pair per qualifying session: gap_pct = 100*ln(time_a/time_b) in the deepest part both drivers set a time in (positive = driver_a slower)'
AS
WITH q AS (
  SELECT
    q.season, q.round, q.session_id, q.driver_id, q.constructor_id,
    q.q1_ms, q.q2_ms, q.q3_ms, q.format
  FROM ${catalog}.silver.qualifying q
  WHERE q.session_code = 'Q'
),
pairs AS (
  SELECT
    a.season, a.round, a.session_id, a.constructor_id, a.format,
    a.driver_id AS driver_a, b.driver_id AS driver_b,
    CASE
      WHEN a.q3_ms IS NOT NULL AND b.q3_ms IS NOT NULL THEN 'Q3'
      WHEN a.q2_ms IS NOT NULL AND b.q2_ms IS NOT NULL THEN 'Q2'
      WHEN a.q1_ms IS NOT NULL AND b.q1_ms IS NOT NULL THEN 'Q1'
    END AS compared_part,
    CASE
      WHEN a.q3_ms IS NOT NULL AND b.q3_ms IS NOT NULL THEN a.q3_ms
      WHEN a.q2_ms IS NOT NULL AND b.q2_ms IS NOT NULL THEN a.q2_ms
      ELSE a.q1_ms
    END AS time_a_ms,
    CASE
      WHEN a.q3_ms IS NOT NULL AND b.q3_ms IS NOT NULL THEN b.q3_ms
      WHEN a.q2_ms IS NOT NULL AND b.q2_ms IS NOT NULL THEN b.q2_ms
      ELSE b.q1_ms
    END AS time_b_ms
  FROM q a
  JOIN q b
    ON a.session_id = b.session_id AND a.constructor_id = b.constructor_id
   AND a.driver_id < b.driver_id
),
wet AS (
  -- Trackside rain during the session (2018+). Older sessions: filled from hourly
  -- weather history in the wet-skill step.
  SELECT session_id, max(CAST(rainfall AS INT)) = 1 AS any_rain
  FROM ${catalog}.silver.session_weather
  GROUP BY session_id
)
SELECT
  p.season, p.round, p.session_id, p.constructor_id, p.format,
  p.driver_a, p.driver_b, p.compared_part, p.time_a_ms, p.time_b_ms,
  100 * ln(CAST(p.time_a_ms AS DOUBLE) / p.time_b_ms) AS gap_pct,
  w.any_rain AS is_wet,
  -- Gaps beyond 5% are almost always a problem lap (crash, failure, no clean run):
  -- kept, but flagged so models can exclude them.
  abs(100 * ln(CAST(p.time_a_ms AS DOUBLE) / p.time_b_ms)) > 5 AS is_outlier
FROM pairs p
LEFT JOIN wet w USING (session_id)
WHERE p.time_a_ms IS NOT NULL AND p.time_b_ms IS NOT NULL;
