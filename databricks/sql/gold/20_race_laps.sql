-- Race conditions and clean-lap flags: the shared basis for race pace, consistency,
-- tyre wear and (later) safety-car rates.

CREATE OR REPLACE TABLE ${catalog}.gold.race_laps
COMMENT 'Every race lap with neutralisation, pit, traffic and outlier flags; is_clean = usable for pace comparisons'
CLUSTER BY (season, session_id)
AS
WITH l AS (
  SELECT
    l.session_id, l.season, l.round, l.driver_id, l.lap, l.lap_time_ms, l.position,
    l.is_pit_in, l.is_pit_out, l.stint, l.compound, l.tyre_life, l.track_status, l.lap_time_flag,
    e.constructor_id
  FROM ${catalog}.silver.laps l
  LEFT JOIN ${catalog}.silver.entries e
    ON e.season = l.season AND e.round = l.round AND e.driver_id = l.driver_id AND e.is_race_entry
  WHERE l.session_code = 'R' AND l.lap_time_ms IS NOT NULL
),
-- Field-wide slow laps catch safety cars in every era (no official status before 2018).
field AS (
  SELECT session_id, lap, percentile(lap_time_ms, 0.5) AS field_median_ms
  FROM l GROUP BY session_id, lap
),
race_ref AS (
  SELECT session_id, percentile(field_median_ms, 0.5) AS race_median_ms
  FROM field GROUP BY session_id
),
-- OpenF1 (2023+): safety car / VSC periods from race-control messages.
rc AS (
  SELECT
    s.session_id, int(rc.lap_number) AS lap, try_cast(rc.date AS TIMESTAMP) AS ts,
    CASE
      WHEN rc.message = 'SAFETY CAR DEPLOYED' THEN 'sc_start'
      WHEN rc.message = 'SAFETY CAR IN THIS LAP' THEN 'sc_end'
      WHEN rc.message IN ('VIRTUAL SAFETY CAR DEPLOYED', 'VSC DEPLOYED') THEN 'vsc_start'
      WHEN rc.message IN ('VIRTUAL SAFETY CAR ENDING', 'VSC ENDING') THEN 'vsc_end'
      WHEN rc.flag = 'RED' THEN 'red'
    END AS event
  FROM ${catalog}.bronze.openf1_race_control rc
  JOIN ${catalog}.silver.sessions s ON s.openf1_session_key = int(rc.session_key)
  WHERE s.session_code = 'R' AND rc.lap_number IS NOT NULL
),
rc_periods AS (
  SELECT session_id, kind, start_lap,
         coalesce(end_lap, start_lap + 4) AS end_lap
  FROM (
    SELECT
      session_id,
      regexp_extract(event, '^(sc|vsc)', 1) AS kind,
      lap AS start_lap,
      lead(CASE WHEN event LIKE '%_end' THEN lap END) IGNORE NULLS OVER (
        PARTITION BY session_id, regexp_extract(event, '^(sc|vsc)', 1) ORDER BY ts
      ) AS end_lap,
      event
    FROM rc WHERE event IN ('sc_start', 'sc_end', 'vsc_start', 'vsc_end')
  )
  WHERE event LIKE '%_start'
  UNION ALL
  SELECT session_id, 'red', lap, lap FROM rc WHERE event = 'red'
),
rc_laps AS (
  SELECT DISTINCT session_id, explode(sequence(start_lap, end_lap)) AS lap, kind
  FROM rc_periods WHERE end_lap >= start_lap
),
pits AS (
  SELECT DISTINCT session_id, driver_id, lap FROM ${catalog}.silver.pit_stops WHERE session_code = 'R'
),
cum AS (
  SELECT l.*,
    sum(lap_time_ms) OVER (PARTITION BY session_id, driver_id ORDER BY lap) AS cum_ms
  FROM l
),
gaps AS (
  SELECT c.*,
    c.cum_ms - lag(c.cum_ms) OVER (PARTITION BY c.session_id, c.lap ORDER BY c.position) AS gap_ahead_ms
  FROM cum c
),
flagged AS (
  SELECT
    g.*,
    f.field_median_ms, rr.race_median_ms,
    -- coalesce: a missing track status must read as 'not neutralised', never as unknown
    (f.field_median_ms > 1.10 * rr.race_median_ms
      OR coalesce(g.track_status IN ('sc', 'vsc', 'red'), false) OR rl.lap IS NOT NULL) AS is_neutralised,
    (coalesce(g.is_pit_in, false) OR p_in.lap IS NOT NULL) AS is_in_lap,
    (coalesce(g.is_pit_out, false) OR p_out.lap IS NOT NULL) AS is_out_lap
  FROM gaps g
  JOIN field f USING (session_id, lap)
  JOIN race_ref rr USING (session_id)
  LEFT JOIN (SELECT DISTINCT session_id, lap FROM rc_laps) rl USING (session_id, lap)
  LEFT JOIN pits p_in ON p_in.session_id = g.session_id AND p_in.driver_id = g.driver_id AND p_in.lap = g.lap
  LEFT JOIN pits p_out ON p_out.session_id = g.session_id AND p_out.driver_id = g.driver_id AND p_out.lap = g.lap - 1
),
driver_ref AS (
  -- Each driver's typical green-flag lap this race (basis for the 107% rule).
  SELECT session_id, driver_id, percentile(lap_time_ms, 0.5) AS driver_median_ms
  FROM flagged
  WHERE lap > 1 AND NOT is_neutralised AND NOT is_in_lap AND NOT is_out_lap
  GROUP BY session_id, driver_id
),
final AS (
  SELECT
    f.*, d.driver_median_ms,
    lag(f.lap_time_ms) OVER (PARTITION BY f.session_id, f.driver_id ORDER BY f.lap) AS prev_lap_ms
  FROM flagged f
  LEFT JOIN driver_ref d USING (session_id, driver_id)
)
SELECT
  session_id, season, round, driver_id, constructor_id, lap, lap_time_ms, position,
  stint, compound, tyre_life, gap_ahead_ms, field_median_ms, race_median_ms, driver_median_ms,
  is_neutralised, is_in_lap, is_out_lap,
  CASE
    WHEN lap = 1 THEN 'lap_1'
    WHEN is_neutralised THEN 'neutralised'
    WHEN is_in_lap THEN 'pit_in'
    WHEN is_out_lap THEN 'pit_out'
    -- Before 2011 there is no pit data: a very slow previous lap marks an out-lap.
    WHEN season < 2011 AND prev_lap_ms > 1.15 * driver_median_ms THEN 'pit_out_inferred'
    WHEN lap_time_flag IS NOT NULL THEN 'outlier_flag'
    WHEN lap_time_ms > 1.07 * driver_median_ms THEN 'over_107pct'
    WHEN gap_ahead_ms IS NOT NULL AND gap_ahead_ms < 1500 THEN 'traffic'
  END AS exclusion_reason,
  CASE
    WHEN lap = 1 OR is_neutralised OR is_in_lap OR is_out_lap THEN false
    WHEN season < 2011 AND prev_lap_ms > 1.15 * driver_median_ms THEN false
    WHEN lap_time_flag IS NOT NULL OR lap_time_ms > 1.07 * driver_median_ms THEN false
    WHEN gap_ahead_ms IS NOT NULL AND gap_ahead_ms < 1500 THEN false
    ELSE true
  END AS is_clean
FROM final;

CREATE OR REPLACE TABLE ${catalog}.gold.race_conditions
COMMENT 'Per race: wet or dry. 2018+ from wet-tyre use (>= 10% of laps). 2001-2017 from lap times vs pole and hourly rain, calibrated on 2018+: is_wet (broad, for excluding wet races) and is_wet_confident (strict, for wet skill)'
AS
WITH races AS (
  SELECT s.session_id, s.season, s.round, e.circuit_id,
         coalesce(s.start_utc, e.race_start_utc) AS start_utc
  FROM ${catalog}.silver.sessions s
  JOIN ${catalog}.silver.events e ON e.season = s.season AND e.round = s.round
  WHERE s.session_code = 'R'
),
tyres AS (
  -- Only real compound names count (FastF1 reports UNKNOWN for some races, e.g. Spa 2021).
  SELECT session_id, avg(CAST(is_wet_tyre AS INT)) AS wet_tyre_share, count(*) AS laps_with_compound
  FROM ${catalog}.silver.laps
  WHERE session_code = 'R'
    AND compound IN ('HYPERSOFT', 'ULTRASOFT', 'SUPERSOFT', 'SOFT', 'MEDIUM', 'HARD', 'INTERMEDIATE', 'WET')
  GROUP BY session_id
),
sensor AS (
  SELECT session_id, avg(CAST(rainfall AS INT)) AS sensor_rain_share
  FROM ${catalog}.silver.session_weather GROUP BY session_id
),
hourly AS (
  SELECT r.session_id, sum(w.precipitation_mm) AS rain_mm_2h
  FROM races r
  JOIN ${catalog}.silver.weather_hourly w
    ON w.circuit_id = r.circuit_id
   AND w.time_utc BETWEEN date_trunc('HOUR', r.start_utc) AND r.start_utc + INTERVAL 2 HOURS
  GROUP BY r.session_id
),
pole AS (
  SELECT season, round, min(least(q1_ms, q2_ms, q3_ms)) AS pole_ms
  FROM ${catalog}.silver.qualifying WHERE session_code = 'Q' GROUP BY season, round
),
pace AS (
  -- Typical green-flag race lap, for the "much slower than pole" wet signal.
  SELECT session_id, season, round, percentile(lap_time_ms, 0.5) AS race_median_ms
  FROM ${catalog}.gold.race_laps
  WHERE lap > 1 AND NOT is_neutralised AND NOT is_in_lap AND NOT is_out_lap
  GROUP BY ALL
)
SELECT
  r.session_id, r.season, r.round, r.circuit_id, r.start_utc,
  t.wet_tyre_share, s.sensor_rain_share, h.rain_mm_2h,
  pc.race_median_ms / po.pole_ms AS pole_ratio,
  -- Broad flag (used to keep wet races OUT of dry-pace measures: missing one matters more).
  -- 2001-2017 calibrated on 2018+ races: slow laps OR >= 0.8 mm rain, F1 0.55.
  CASE
    WHEN t.laps_with_compound > 0 THEN t.wet_tyre_share >= 0.10
    WHEN pc.race_median_ms IS NOT NULL OR h.rain_mm_2h IS NOT NULL
      THEN coalesce(pc.race_median_ms / po.pole_ms >= 1.14, false) OR coalesce(h.rain_mm_2h >= 0.8, false)
  END AS is_wet,
  -- Confident flag (used to measure wet-weather skill: a false wet race matters more).
  -- 2001-2017: race laps >= 14% slower than pole: 6/6 correct, 0 false alarms on 2018+.
  CASE
    WHEN t.laps_with_compound > 0 THEN t.wet_tyre_share >= 0.10
    ELSE coalesce(pc.race_median_ms / po.pole_ms >= 1.14, false)
  END AS is_wet_confident,
  CASE
    WHEN t.laps_with_compound > 0 THEN 'wet_tyre_share'
    WHEN pc.race_median_ms IS NOT NULL OR h.rain_mm_2h IS NOT NULL THEN 'lap_times_and_hourly_rain'
  END AS wet_source
FROM races r
LEFT JOIN tyres t USING (session_id)
LEFT JOIN sensor s USING (session_id)
LEFT JOIN hourly h USING (session_id)
LEFT JOIN pace pc USING (session_id)
LEFT JOIN pole po ON po.season = r.season AND po.round = r.round;
