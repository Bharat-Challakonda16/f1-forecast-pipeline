-- Tyre models (2019+, when Soft/Medium/Hard became the labels at every race).
-- Per stint: fuel-corrected lap time y = 100*ln(t) + 0.035*lap (cars get ~0.035%/lap faster
-- as fuel burns) regressed on tyre age -> wear slope (%/lap) and fresh-tyre pace (intercept).

CREATE OR REPLACE TABLE ${catalog}.gold.tyre_stints
COMMENT 'Per dry-race stint (2019+, >= 8 clean laps): fuel-corrected wear slope (% lap time per lap of tyre age), fresh-tyre intercept, stint length and race track temperature'
AS
WITH temps AS (
  SELECT session_id, percentile(track_temp_c, 0.5) AS track_temp_c, percentile(air_temp_c, 0.5) AS air_temp_c
  FROM ${catalog}.silver.session_weather
  GROUP BY session_id
),
lengths AS (
  SELECT session_id, driver_id, stint, max(tyre_life) AS stint_laps
  FROM ${catalog}.gold.race_laps
  WHERE season >= 2019 AND stint IS NOT NULL
  GROUP BY ALL
)
SELECT
  l.session_id, l.season, l.round, e.circuit_id, c.speed_profile, l.constructor_id, l.driver_id,
  l.stint, l.compound,
  count(*) AS clean_laps,
  regr_slope(100 * ln(CAST(l.lap_time_ms AS DOUBLE)) + 0.035 * l.lap, l.tyre_life) AS wear_pct_per_lap,
  regr_intercept(100 * ln(CAST(l.lap_time_ms AS DOUBLE)) + 0.035 * l.lap, l.tyre_life) AS fresh_level,
  percentile(l.lap_time_ms, 0.5) AS median_lap_ms,
  max(n.stint_laps) AS stint_laps,
  max(t.track_temp_c) AS track_temp_c,
  max(t.air_temp_c) AS air_temp_c
FROM ${catalog}.gold.race_laps l
JOIN ${catalog}.gold.race_conditions rc ON rc.session_id = l.session_id
JOIN ${catalog}.silver.events e ON e.season = l.season AND e.round = l.round
LEFT JOIN ${catalog}.silver.circuits c ON c.circuit_id = e.circuit_id
LEFT JOIN temps t ON t.session_id = l.session_id
LEFT JOIN lengths n ON n.session_id = l.session_id AND n.driver_id = l.driver_id AND n.stint = l.stint
WHERE l.is_clean AND l.season >= 2019 AND NOT coalesce(rc.is_wet, false)
  AND l.compound IN ('SOFT', 'MEDIUM', 'HARD') AND l.tyre_life IS NOT NULL
GROUP BY ALL
HAVING count(*) >= 8;

CREATE OR REPLACE TABLE ${catalog}.gold.tyre_temp_sensitivity
COMMENT 'Per compound: extra wear (% lap time per lap) per +10 C of track temperature, pooled over all dry stints 2019+'
AS
SELECT
  compound,
  count(*) AS stints,
  10 * regr_slope(wear_pct_per_lap, track_temp_c) AS wear_per_10c,
  avg(wear_pct_per_lap) AS mean_wear_pct_per_lap,
  avg(track_temp_c) AS mean_track_temp_c
FROM ${catalog}.gold.tyre_stints
WHERE track_temp_c IS NOT NULL AND wear_pct_per_lap BETWEEN -0.5 AND 1.0
GROUP BY compound;

CREATE OR REPLACE TABLE ${catalog}.gold.tyre_compound_offsets
COMMENT 'Per dry race (2019+): fresh-tyre pace of SOFT / HARD vs MEDIUM from teammates on the same lap on different compounds (same car, fuel and track state), adjusted for tyre-age difference with the pooled compound wear. Per race so circuit values can be built as of any date.'
AS
WITH pair_laps AS (
  SELECT
    a.session_id, a.season, a.round, e.circuit_id, e.race_date,
    a.compound AS ca, b.compound AS cb,
    100 * ln(CAST(a.lap_time_ms AS DOUBLE) / b.lap_time_ms)
      - (wa.mean_wear_pct_per_lap * a.tyre_life - wb.mean_wear_pct_per_lap * b.tyre_life) AS d
  FROM ${catalog}.gold.race_laps a
  JOIN ${catalog}.gold.race_laps b
    ON b.session_id = a.session_id AND b.lap = a.lap AND b.constructor_id = a.constructor_id
   AND b.driver_id <> a.driver_id AND b.compound <> a.compound
  JOIN ${catalog}.gold.race_conditions rc ON rc.session_id = a.session_id
  JOIN ${catalog}.silver.events e ON e.season = a.season AND e.round = a.round
  JOIN ${catalog}.gold.tyre_temp_sensitivity wa ON wa.compound = a.compound
  JOIN ${catalog}.gold.tyre_temp_sensitivity wb ON wb.compound = b.compound
  WHERE a.is_clean AND b.is_clean AND a.season >= 2019 AND NOT coalesce(rc.is_wet, false)
    AND a.compound IN ('SOFT', 'MEDIUM', 'HARD') AND b.compound IN ('SOFT', 'MEDIUM', 'HARD')
    AND a.tyre_life IS NOT NULL AND b.tyre_life IS NOT NULL
)
SELECT session_id, season, round, circuit_id, race_date, compound,
       percentile(d, 0.5) AS fresh_vs_medium_pct, count(*) AS lap_pairs
FROM (
  SELECT session_id, season, round, circuit_id, race_date, ca AS compound, d FROM pair_laps WHERE cb = 'MEDIUM'
  UNION ALL
  SELECT session_id, season, round, circuit_id, race_date, cb AS compound, -d FROM pair_laps WHERE ca = 'MEDIUM'
)
GROUP BY ALL;

CREATE OR REPLACE TABLE ${catalog}.gold.tyre_models
COMMENT 'Per circuit and compound: modelled wear at a 35 C track = circuit wear level (all compounds pooled, recency-weighted, shrunk to the track-type level) x global compound wear (pooled over all stints) - keeps Soft > Medium > Hard; raw per-compound measurement kept for reference. Also temperature sensitivity, fresh-tyre pace vs Medium (teammates on the same lap), typical and p90 (~cliff) stint lengths.'
AS
WITH s AS (
  SELECT
    t.*, ts.wear_per_10c,
    -- wear normalised to a 35 C track so circuits raced in different heat are comparable
    t.wear_pct_per_lap - coalesce(ts.wear_per_10c, 0) * (coalesce(t.track_temp_c, 35) - 35) / 10 AS wear_at_35c,
    power(0.85, (SELECT max(season) FROM ${catalog}.gold.tyre_stints) - t.season) AS wt
  FROM ${catalog}.gold.tyre_stints t
  JOIN ${catalog}.gold.tyre_temp_sensitivity ts USING (compound)
  WHERE t.wear_pct_per_lap BETWEEN -0.5 AND 1.0
),
by_circuit AS (
  SELECT circuit_id, speed_profile, compound,
    count(*) AS stints,
    sum(wt) AS w_stints,
    sum(wt * wear_at_35c) / sum(wt) AS wear_circuit,
    percentile(median_lap_ms, 0.5) AS lap_ms,
    percentile(stint_laps, 0.5) AS typical_stint_laps,
    percentile(stint_laps, 0.9) AS max_stint_laps,
    max(wear_per_10c) AS wear_per_10c
  FROM s GROUP BY ALL
),
by_type AS (
  SELECT speed_profile, compound, sum(wt * wear_at_35c) / sum(wt) AS wear_type
  FROM s GROUP BY ALL
),
compound_level AS (
  -- Global wear per compound, pooled over every stint (robust ordering Soft > Medium > Hard)
  SELECT compound, sum(wt * wear_at_35c) / sum(wt) AS compound_wear FROM s GROUP BY compound
),
circuit_level AS (
  -- How hard a circuit is on tyres, as a multiple of the global compound wear, all compounds
  -- pooled; shrunk to the track type's level with 15 pseudo-stints.
  SELECT x.circuit_id,
    (sum(x.wt * x.wear_at_35c / c.compound_wear) + 15 * max(t.type_level)) / (sum(x.wt) + 15) AS level
  FROM s x
  JOIN compound_level c USING (compound)
  JOIN (
    SELECT y.speed_profile, sum(y.wt * y.wear_at_35c / c2.compound_wear) / sum(y.wt) AS type_level
    FROM s y JOIN compound_level c2 USING (compound) GROUP BY y.speed_profile
  ) t ON t.speed_profile <=> x.speed_profile
  GROUP BY x.circuit_id
),
offsets AS (
  SELECT circuit_id, compound, count(*) AS races_compared,
         percentile(fresh_vs_medium_pct, 0.5) AS fresh_vs_medium_pct
  FROM ${catalog}.gold.tyre_compound_offsets GROUP BY ALL
),
global_offsets AS (
  SELECT compound, percentile(fresh_vs_medium_pct, 0.5) AS g_offset
  FROM ${catalog}.gold.tyre_compound_offsets GROUP BY compound
)
SELECT
  b.circuit_id, b.speed_profile, b.compound, b.stints,
  cl.level * cw.compound_wear AS wear_pct_per_lap_35c,
  cl.level * cw.compound_wear / 100 * b.lap_ms AS wear_ms_per_lap_35c,
  cl.level AS circuit_wear_level,
  (b.w_stints * b.wear_circuit + 10 * t.wear_type) / (b.w_stints + 10) AS raw_wear_pct_per_lap_35c,
  b.wear_per_10c AS wear_pct_per_lap_per_10c,
  CASE WHEN b.compound = 'MEDIUM' THEN 0.0
       ELSE (coalesce(o.races_compared, 0) * coalesce(o.fresh_vs_medium_pct, 0) + 5 * g.g_offset)
            / (coalesce(o.races_compared, 0) + 5) END AS fresh_pace_vs_medium_pct,
  b.typical_stint_laps, b.max_stint_laps, b.lap_ms AS typical_lap_ms
FROM by_circuit b
JOIN by_type t USING (speed_profile, compound)
JOIN circuit_level cl ON cl.circuit_id = b.circuit_id
JOIN compound_level cw ON cw.compound = b.compound
LEFT JOIN offsets o ON o.circuit_id = b.circuit_id AND o.compound = b.compound
LEFT JOIN global_offsets g ON g.compound = b.compound;
