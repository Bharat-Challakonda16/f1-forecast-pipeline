-- Input for the joint field model: every driver's pace in every qualifying session and race,
-- relative to the typical car in that session. The model splits it into car (per team, per
-- round: upgrades), car x track type, driver skill, driver x track type and driver x circuit.

CREATE OR REPLACE TABLE ${catalog}.gold.driver_session_pace
COMMENT 'One row per driver per qualifying session (best lap) and race (median clean lap): pace_pct = 100*ln(time / session median), lower = faster; is_outlier flags crashes/failures (>5% off); is_wet from rain sensors (2018+) or race conditions'
AS
WITH q AS (
  SELECT q.season, q.round, q.driver_id, q.constructor_id,
         least(q.q1_ms, q.q2_ms, q.q3_ms) AS t_ms
  FROM ${catalog}.silver.qualifying q
  WHERE q.session_code = 'Q' AND least(q.q1_ms, q.q2_ms, q.q3_ms) IS NOT NULL
),
q_wet AS (
  SELECT s.season, s.round, max(CAST(w.rainfall AS INT)) = 1 AS wet
  FROM ${catalog}.silver.session_weather w
  JOIN ${catalog}.silver.sessions s USING (session_id)
  WHERE s.session_code = 'Q'
  GROUP BY ALL
),
quali AS (
  SELECT 'quali' AS kind, q.season, q.round, q.driver_id, q.constructor_id,
         100 * ln(q.t_ms / percentile(q.t_ms, 0.5) OVER (PARTITION BY q.season, q.round)) AS pace_pct,
         CAST(NULL AS BIGINT) AS n_laps,
         coalesce(w.wet, false) AS is_wet
  FROM q LEFT JOIN q_wet w USING (season, round)
),
r AS (
  SELECT l.season, l.round, l.session_id, l.driver_id, l.constructor_id,
         percentile(l.lap_time_ms, 0.5) AS t_ms, count(*) AS n_laps
  FROM ${catalog}.gold.race_laps l
  WHERE l.is_clean AND l.constructor_id IS NOT NULL
  GROUP BY ALL
  HAVING count(*) >= 10
),
race AS (
  SELECT 'race' AS kind, r.season, r.round, r.driver_id, r.constructor_id,
         100 * ln(r.t_ms / percentile(r.t_ms, 0.5) OVER (PARTITION BY r.season, r.round)) AS pace_pct,
         r.n_laps,
         coalesce(c.is_wet, false) AS is_wet
  FROM r LEFT JOIN ${catalog}.gold.race_conditions c USING (session_id)
)
SELECT
  p.kind, p.season, p.round, e.circuit_id, e.race_date,
  coalesce(c.speed_profile, 'unknown') AS speed_profile,
  coalesce(c.circuit_type, 'unknown') AS circuit_type,
  p.driver_id, p.constructor_id, p.pace_pct, p.n_laps, p.is_wet,
  abs(p.pace_pct) > 5 AS is_outlier
FROM (SELECT * FROM quali UNION ALL SELECT * FROM race) p
JOIN ${catalog}.silver.events e USING (season, round)
LEFT JOIN ${catalog}.silver.circuits c ON c.circuit_id = e.circuit_id;
