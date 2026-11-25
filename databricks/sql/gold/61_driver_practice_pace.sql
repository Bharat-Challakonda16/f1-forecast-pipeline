-- Practice pace (2018+), one row per driver per practice session, relative to the field in
-- that session (lower = faster):
--   best_pct: best lap (a "qualifying simulation" if the driver did one)
--   long_pct: long runs (stints of >= 5 consecutive valid laps), each stint's median lap
--             compared with other drivers' long runs on the same compound in the session,
--             averaged over the driver's stints weighted by laps
-- Valid lap: timed, not deleted, not an in/out lap, dry tyres, not under yellow/red/SC/VSC
-- (where known), within 107% of the driver's best in the session. Only drivers who raced
-- that weekend (team from race results). Fuel loads are unknown, so both measures are noisy:
-- the after-practice stage learns how much to trust them.

CREATE OR REPLACE TABLE ${catalog}.gold.driver_practice_pace
COMMENT 'Per driver per practice session (2018+): best_pct = 100*ln(best lap / session median of best laps), long_pct = long-run pace vs other long runs on the same compound; lower = faster'
AS
WITH laps AS (
  SELECT l.season, l.round, l.session_id, l.session_code, l.driver_id, l.stint, l.lap,
         l.compound, l.lap_time_ms
  FROM ${catalog}.silver.laps l
  WHERE l.session_code IN ('FP1', 'FP2', 'FP3')
    AND l.lap_time_ms IS NOT NULL
    AND NOT coalesce(l.is_deleted, false)
    AND NOT coalesce(l.is_pit_in, false) AND NOT coalesce(l.is_pit_out, false)
    AND NOT coalesce(l.is_wet_tyre, false)
    AND coalesce(l.compound, 'UNKNOWN') NOT IN ('INTERMEDIATE', 'WET')
    AND coalesce(l.track_status, 'green') = 'green'
),
valid AS (
  SELECT *, min(lap_time_ms) OVER (PARTITION BY session_id, driver_id) AS best_ms
  FROM laps
),
v AS (SELECT * FROM valid WHERE lap_time_ms <= 1.07 * best_ms),
best AS (
  SELECT season, round, session_id, session_code, driver_id, min(lap_time_ms) AS best_ms,
         count(*) AS n_laps
  FROM v GROUP BY ALL
),
best_rel AS (
  SELECT *, 100 * ln(best_ms / percentile(best_ms, 0.5) OVER (PARTITION BY session_id)) AS best_pct
  FROM best
),
-- consecutive valid laps within a stint form a run
runs AS (
  SELECT *, lap - row_number() OVER (PARTITION BY session_id, driver_id, stint ORDER BY lap) AS grp
  FROM v WHERE compound IS NOT NULL AND stint IS NOT NULL
),
stints AS (
  SELECT season, round, session_id, driver_id, compound, stint, grp,
         percentile(lap_time_ms, 0.5) AS med_ms, count(*) AS laps
  FROM runs GROUP BY ALL
  HAVING count(*) >= 5
),
stint_rel AS (
  SELECT *,
    100 * ln(med_ms / percentile(med_ms, 0.5) OVER (PARTITION BY session_id, compound)) AS rel,
    size(collect_set(driver_id) OVER (PARTITION BY session_id, compound)) AS drivers_on_compound
  FROM stints
),
long AS (
  SELECT session_id, driver_id, sum(rel * laps) / sum(laps) AS long_pct, sum(laps) AS long_laps
  FROM stint_rel WHERE drivers_on_compound >= 4
  GROUP BY ALL
)
SELECT b.season, b.round, b.session_code, b.driver_id, r.constructor_id,
       b.best_pct, b.n_laps, l.long_pct, coalesce(l.long_laps, 0) AS long_laps,
       abs(b.best_pct) > 5 AS is_outlier
FROM best_rel b
JOIN ${catalog}.silver.results r
  ON r.season = b.season AND r.round = b.round AND r.session_code = 'R' AND r.driver_id = b.driver_id
LEFT JOIN long l ON l.session_id = b.session_id AND l.driver_id = b.driver_id;
