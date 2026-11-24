-- Laps and pit stops, conformed across sources:
--   Jolpica      2001+  race laps: official lap time + position (dump + API pages)
--   FastF1       2018-2022 (+ filled gaps): every session, sectors, tyres, track status
--   OpenF1       2023+: every session, sectors, tyres (from stints), pit flags
-- Race laps are matched on (session, driver, lap); official Jolpica time/position win.

CREATE OR REPLACE TABLE ${catalog}.silver.laps
COMMENT 'One row per driver per lap per session. lap_time_ms/position are official (Jolpica) where available; detail columns come from FastF1 (2018-22) or OpenF1 (2023+).'
CLUSTER BY (season, session_id)
AS
WITH jol_dump AS (
  SELECT
    int(y.year) AS season, int(r.number) AS round, 'R' AS session_code,
    d.reference AS driver_id, int(l.number) AS lap,
    int(l.position) AS position, ${catalog}.silver.time_to_ms(l.time) AS lap_time_ms
  FROM ${catalog}.bronze.jolpica_dump_lap l
  JOIN ${catalog}.bronze.jolpica_dump_sessionentry se ON l.session_entry_id = se.id
  JOIN ${catalog}.bronze.jolpica_dump_session s ON se.session_id = s.id
  JOIN ${catalog}.bronze.jolpica_dump_round r ON s.round_id = r.id
  JOIN ${catalog}.bronze.jolpica_dump_season y ON r.season_id = y.id
  JOIN ${catalog}.bronze.jolpica_dump_roundentry re ON se.round_entry_id = re.id
  JOIN ${catalog}.bronze.jolpica_dump_teamdriver td ON re.team_driver_id = td.id
  JOIN ${catalog}.bronze.jolpica_dump_driver d ON td.driver_id = d.id
  WHERE s.type = 'R' AND l.number IS NOT NULL AND int(y.year) >= 2001 AND r.number IS NOT NULL
),
jol_api AS (
  SELECT p.season, p.round, 'R', t.driverId, int(lp.number), int(t.position),
         ${catalog}.silver.time_to_ms(t.time)
  FROM ${catalog}.bronze.jolpica_api_pages p
  LATERAL VIEW explode(
    get(from_json(p.payload, 'STRUCT<MRData: STRUCT<RaceTable: STRUCT<Races: ARRAY<STRUCT<
        Laps: ARRAY<STRUCT<number: STRING, Timings: ARRAY<STRUCT<driverId: STRING, position: STRING, time: STRING>>>>>>>>>')
      .MRData.RaceTable.Races, 0).Laps
  ) AS lp
  LATERAL VIEW explode(lp.Timings) AS t
  WHERE p.endpoint = 'laps'
),
jol AS (
  SELECT * FROM jol_dump
  UNION ALL
  SELECT * FROM jol_api a
  WHERE NOT EXISTS (SELECT 1 FROM jol_dump d WHERE d.season = a.season AND d.round = a.round)
),
ff AS (
  SELECT
    int(_year) AS season, int(_round) AS round, ${catalog}.silver.session_code(_session) AS session_code,
    int(DriverNumber) AS car_number, int(double(LapNumber)) AS lap,
    bigint(round(double(LapTime) * 1000)) AS lap_time_ms,
    int(double(Position)) AS position,
    bigint(round(double(Sector1Time) * 1000)) AS sector1_ms,
    bigint(round(double(Sector2Time) * 1000)) AS sector2_ms,
    bigint(round(double(Sector3Time) * 1000)) AS sector3_ms,
    double(SpeedI1) AS speed_i1_kph, double(SpeedI2) AS speed_i2_kph,
    double(SpeedFL) AS speed_fl_kph, double(SpeedST) AS speed_trap_kph,
    coalesce(PitInTime, '') NOT IN ('', 'nan', 'NaT') AS is_pit_in,
    coalesce(PitOutTime, '') NOT IN ('', 'nan', 'NaT') AS is_pit_out,
    int(double(Stint)) AS stint,
    nullif(nullif(nullif(upper(Compound), 'NAN'), 'NONE'), '') AS compound,
    int(double(TyreLife)) AS tyre_life,
    try_cast(FreshTyre AS BOOLEAN) AS fresh_tyre,
    nullif(TrackStatus, '') AS track_status_raw,
    try_cast(Deleted AS BOOLEAN) AS is_deleted,
    try_cast(left(LapStartDate, 23) AS TIMESTAMP) AS lap_start_utc,
    'fastf1' AS detail_source
  FROM ${catalog}.bronze.fastf1_laps
  WHERE try_cast(double(LapNumber) AS INT) IS NOT NULL
),
openf1_sessions AS (
  SELECT openf1_session_key, season, round, session_code
  FROM ${catalog}.silver.sessions
  WHERE openf1_session_key IS NOT NULL
),
of_stints AS (
  SELECT
    int(session_key) AS session_key, int(driver_number) AS driver_number, int(stint_number) AS stint,
    int(lap_start) AS lap_start, int(lap_end) AS lap_end, upper(compound) AS compound,
    int(tyre_age_at_start) AS tyre_age_at_start
  FROM ${catalog}.bronze.openf1_stints
),
of_pit_laps AS (
  SELECT DISTINCT int(session_key) AS session_key, int(driver_number) AS driver_number, int(lap_number) AS lap
  FROM ${catalog}.bronze.openf1_pit
  WHERE lap_number IS NOT NULL
),
of AS (
  SELECT
    s.season, s.round, s.session_code,
    int(l.driver_number) AS car_number, int(l.lap_number) AS lap,
    bigint(round(double(l.lap_duration) * 1000)) AS lap_time_ms,
    CAST(NULL AS INT) AS position,
    bigint(round(double(l.duration_sector_1) * 1000)) AS sector1_ms,
    bigint(round(double(l.duration_sector_2) * 1000)) AS sector2_ms,
    bigint(round(double(l.duration_sector_3) * 1000)) AS sector3_ms,
    double(l.i1_speed), double(l.i2_speed), CAST(NULL AS DOUBLE), double(l.st_speed),
    p.lap IS NOT NULL AS is_pit_in,
    coalesce(try_cast(l.is_pit_out_lap AS BOOLEAN), false) AS is_pit_out,
    st.stint, st.compound,
    st.tyre_age_at_start + int(l.lap_number) - st.lap_start AS tyre_life,
    st.tyre_age_at_start = 0 AS fresh_tyre,
    CAST(NULL AS STRING) AS track_status_raw,
    CAST(NULL AS BOOLEAN) AS is_deleted,
    try_cast(l.date_start AS TIMESTAMP) AS lap_start_utc,
    'openf1' AS detail_source
  FROM ${catalog}.bronze.openf1_laps l
  JOIN openf1_sessions s ON s.openf1_session_key = int(l.session_key)
  LEFT JOIN of_stints st
    ON st.session_key = int(l.session_key) AND st.driver_number = int(l.driver_number)
   AND int(l.lap_number) BETWEEN st.lap_start AND st.lap_end
  LEFT JOIN of_pit_laps p
    ON p.session_key = int(l.session_key) AND p.driver_number = int(l.driver_number)
   AND p.lap = int(l.lap_number)
  -- Overlapping stint records: keep the latest stint for a lap.
  QUALIFY row_number() OVER (
    PARTITION BY l.session_key, l.driver_number, l.lap_number ORDER BY st.stint DESC NULLS LAST
  ) = 1
),
detail AS (
  SELECT * FROM ff
  UNION ALL
  -- FastF1 wins where both exist (only the filled 2025 Baku qualifying).
  SELECT * FROM of o
  WHERE NOT EXISTS (
    SELECT 1 FROM ff f WHERE f.season = o.season AND f.round = o.round AND f.session_code = o.session_code
  )
),
detail_mapped AS (
  SELECT dt.*, sd.driver_id
  FROM detail dt
  LEFT JOIN ${catalog}.silver.session_drivers sd
    ON sd.session_id = ${catalog}.silver.session_id(dt.season, dt.round, dt.session_code)
   AND sd.car_number = dt.car_number
),
joined AS (
  SELECT
    coalesce(d.season, j.season) AS season,
    coalesce(d.round, j.round) AS round,
    coalesce(d.session_code, j.session_code) AS session_code,
    coalesce(d.driver_id, j.driver_id) AS driver_id,
    d.car_number,
    coalesce(d.lap, j.lap) AS lap,
    coalesce(j.lap_time_ms, d.lap_time_ms) AS lap_time_ms,
    coalesce(j.position, d.position) AS position,
    d.sector1_ms, d.sector2_ms, d.sector3_ms,
    d.speed_i1_kph, d.speed_i2_kph, d.speed_fl_kph, d.speed_trap_kph,
    d.is_pit_in, d.is_pit_out, d.stint, d.compound, d.tyre_life, d.fresh_tyre,
    d.track_status_raw, d.is_deleted, d.lap_start_utc,
    CASE
      WHEN j.lap IS NOT NULL AND d.lap IS NOT NULL THEN concat('jolpica+', d.detail_source)
      WHEN j.lap IS NOT NULL THEN 'jolpica'
      ELSE d.detail_source
    END AS source
  FROM detail_mapped d
  FULL OUTER JOIN jol j
    ON j.season = d.season AND j.round = d.round AND j.session_code = d.session_code
   AND j.driver_id = d.driver_id AND j.lap = d.lap
)
SELECT
  ${catalog}.silver.session_id(season, round, session_code) AS session_id,
  season, round, session_code, driver_id, car_number, lap, lap_time_ms, position,
  sector1_ms, sector2_ms, sector3_ms,
  speed_i1_kph, speed_i2_kph, speed_fl_kph, speed_trap_kph,
  is_pit_in, is_pit_out, stint, compound, tyre_life, fresh_tyre,
  -- FastF1 status digits: 1 green, 2 yellow, 4 SC, 5 red, 6/7 VSC. OpenF1 track status is
  -- derived from race-control messages in Phase 2.
  CASE
    WHEN track_status_raw RLIKE '5' THEN 'red'
    WHEN track_status_raw RLIKE '4' THEN 'sc'
    WHEN track_status_raw RLIKE '[67]' THEN 'vsc'
    WHEN track_status_raw RLIKE '2' THEN 'yellow'
    WHEN track_status_raw RLIKE '1' THEN 'green'
  END AS track_status,
  compound IN ('INTERMEDIATE', 'WET') AS is_wet_tyre,
  is_deleted,
  -- Outliers are flagged, never deleted (the reason travels with the row).
  CASE
    WHEN lap_time_ms IS NULL THEN NULL
    WHEN lap_time_ms < 50000 THEN 'under_50s'
    WHEN lap_time_ms > 3600000 THEN 'over_1h'
  END AS lap_time_flag,
  lap_start_utc, source
FROM joined;

CREATE OR REPLACE TABLE ${catalog}.silver.pit_stops
COMMENT 'Pit stops: Jolpica (2011+, total pit time) merged with OpenF1 (2023+, lane and stationary time)'
AS
WITH jol_dump AS (
  SELECT
    int(y.year) AS season, int(r.number) AS round, ${catalog}.silver.session_code(s.type) AS session_code,
    d.reference AS driver_id, int(p.number) AS stop_number, int(l.number) AS lap,
    ${catalog}.silver.time_to_ms(p.duration) AS pit_duration_ms
  FROM ${catalog}.bronze.jolpica_dump_pitstop p
  JOIN ${catalog}.bronze.jolpica_dump_sessionentry se ON p.session_entry_id = se.id
  JOIN ${catalog}.bronze.jolpica_dump_session s ON se.session_id = s.id
  JOIN ${catalog}.bronze.jolpica_dump_round r ON s.round_id = r.id
  JOIN ${catalog}.bronze.jolpica_dump_season y ON r.season_id = y.id
  JOIN ${catalog}.bronze.jolpica_dump_roundentry re ON se.round_entry_id = re.id
  JOIN ${catalog}.bronze.jolpica_dump_teamdriver td ON re.team_driver_id = td.id
  JOIN ${catalog}.bronze.jolpica_dump_driver d ON td.driver_id = d.id
  LEFT JOIN ${catalog}.bronze.jolpica_dump_lap l ON p.lap_id = l.id
  WHERE int(y.year) >= 2001 AND r.number IS NOT NULL
),
jol_api AS (
  SELECT p.season, p.round, 'R', x.driverId, int(x.stop), int(x.lap), ${catalog}.silver.time_to_ms(x.duration)
  FROM ${catalog}.bronze.jolpica_api_pages p
  LATERAL VIEW explode(
    get(from_json(p.payload, 'STRUCT<MRData: STRUCT<RaceTable: STRUCT<Races: ARRAY<STRUCT<
        PitStops: ARRAY<STRUCT<driverId: STRING, lap: STRING, stop: STRING, duration: STRING>>>>>>>')
      .MRData.RaceTable.Races, 0).PitStops
  ) AS x
  WHERE p.endpoint = 'pitstops'
),
jol AS (
  SELECT * FROM jol_dump
  UNION ALL
  SELECT * FROM jol_api a
  WHERE NOT EXISTS (SELECT 1 FROM jol_dump d WHERE d.season = a.season AND d.round = a.round)
),
of AS (
  SELECT
    s.season, s.round, s.session_code, e.driver_id, int(p.lap_number) AS lap,
    row_number() OVER (PARTITION BY p.session_key, p.driver_number ORDER BY p.date) AS stop_number,
    bigint(round(double(p.lane_duration) * 1000)) AS lane_duration_ms,
    bigint(round(double(p.stop_duration) * 1000)) AS stationary_ms,
    try_cast(p.date AS TIMESTAMP) AS pit_utc
  FROM ${catalog}.bronze.openf1_pit p
  JOIN ${catalog}.silver.sessions s ON s.openf1_session_key = int(p.session_key)
  LEFT JOIN ${catalog}.silver.session_drivers e
    ON e.session_id = s.session_id AND e.car_number = int(p.driver_number)
)
, merged AS (
SELECT
  coalesce(j.season, o.season) AS season,
  coalesce(j.round, o.round) AS round,
  coalesce(j.session_code, o.session_code) AS session_code,
  coalesce(j.driver_id, o.driver_id) AS driver_id,
  j.stop_number AS jolpica_stop_number,
  coalesce(j.lap, o.lap) AS lap,
  j.pit_duration_ms,
  o.lane_duration_ms,
  o.stationary_ms,
  o.pit_utc,
  CASE
    WHEN j.lap IS NOT NULL AND o.lap IS NOT NULL THEN 'jolpica+openf1'
    WHEN j.lap IS NOT NULL THEN 'jolpica'
    ELSE 'openf1'
  END AS source
FROM jol j
FULL OUTER JOIN of o
  ON o.season = j.season AND o.round = j.round AND o.session_code = j.session_code
 AND o.driver_id = j.driver_id AND o.lap = j.lap
)
SELECT
  ${catalog}.silver.session_id(season, round, session_code) AS session_id,
  season, round, session_code, driver_id,
  -- Renumbered after merging so both sources share one sequence per driver.
  row_number() OVER (
    PARTITION BY season, round, session_code, driver_id ORDER BY lap, pit_utc
  ) AS stop_number,
  jolpica_stop_number, lap, pit_duration_ms, lane_duration_ms, stationary_ms, pit_utc, source
FROM merged
-- OpenF1's pit feed occasionally includes car numbers with no entrant in the session
-- (e.g. support-series cars in 2023 Hungary FP1); those are not F1 stops.
WHERE driver_id IS NOT NULL;
