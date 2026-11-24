-- Reference tables: seasons, circuits, events, sessions, drivers, constructors, entries.
-- Rebuilt in full on every run (deterministic and idempotent). Bronze values are text.

CREATE OR REPLACE TABLE ${catalog}.silver.seasons
COMMENT 'One row per season from 2001; regulation_era groups seasons with comparable cars'
AS
WITH years AS (
  SELECT DISTINCT int(year) AS season FROM ${catalog}.bronze.jolpica_dump_season WHERE int(year) >= 2001
  UNION
  SELECT DISTINCT int(season) FROM ${catalog}.bronze.seed_calendar_2027
)
SELECT
  season,
  CASE
    WHEN season <= 2005 THEN 'v10'
    WHEN season <= 2008 THEN 'v8'
    WHEN season <= 2013 THEN 'v8_kers'
    WHEN season <= 2016 THEN 'hybrid'
    WHEN season <= 2021 THEN 'hybrid_wide'
    WHEN season <= 2025 THEN 'ground_effect'
    ELSE 'active_aero_2026'
  END AS regulation_era,
  season IN (2009, 2014, 2017, 2022, 2026) AS is_major_rule_change
FROM years;

CREATE OR REPLACE TABLE ${catalog}.silver.circuits
COMMENT 'Circuits raced since 2001 or on the 2027 calendar; Jolpica location + hand-curated facts'
AS
WITH used AS (
  SELECT DISTINCT r.circuit_id
  FROM ${catalog}.bronze.jolpica_dump_round r
  JOIN ${catalog}.bronze.jolpica_dump_season y ON r.season_id = y.id
  WHERE int(y.year) >= 2001
)
SELECT
  c.reference AS circuit_id,
  c.name,
  c.locality,
  c.country,
  double(c.latitude) AS latitude,
  double(c.longitude) AS longitude,
  int(c.altitude) AS altitude_m,
  s.circuit_type,
  s.speed_profile,
  double(s.length_km) AS length_km,
  int(s.turns) AS turns,
  int(s.race_laps) AS race_laps,
  int(s.overtaking_difficulty) AS overtaking_difficulty,
  double(s.pit_lane_loss_s_est) AS pit_lane_loss_s_est,
  s.confidence AS seed_confidence
FROM ${catalog}.bronze.jolpica_dump_circuit c
LEFT JOIN ${catalog}.bronze.seed_circuits s ON s.circuit_ref = c.reference
WHERE c.id IN (SELECT circuit_id FROM used)
   OR c.reference IN (SELECT circuit_ref FROM ${catalog}.bronze.seed_calendar_2027);

CREATE OR REPLACE TABLE ${catalog}.silver.events
COMMENT 'One row per championship round (2001 onwards, incl. announced 2027 calendar)'
AS
WITH r AS (
  SELECT
    int(y.year) AS season, int(r.number) AS round, r.id AS round_pk, r.name AS event_name,
    c.reference AS circuit_id, to_date(r.date) AS race_date, r.is_cancelled = 't' AS is_cancelled
  FROM ${catalog}.bronze.jolpica_dump_round r
  JOIN ${catalog}.bronze.jolpica_dump_season y ON r.season_id = y.id
  JOIN ${catalog}.bronze.jolpica_dump_circuit c ON r.circuit_id = c.id
  WHERE int(y.year) >= 2001 AND r.number IS NOT NULL
),
sess AS (
  SELECT
    round_id,
    max(CASE WHEN type = 'R' THEN try_cast(timestamp AS TIMESTAMP) END) AS race_start_utc,
    max(type = 'SR') AS has_sprint
  FROM ${catalog}.bronze.jolpica_dump_session
  GROUP BY round_id
),
dump_done AS (
  SELECT DISTINCT s.round_id
  FROM ${catalog}.bronze.jolpica_dump_session s
  JOIN ${catalog}.bronze.jolpica_dump_sessionentry e ON e.session_id = s.id
  WHERE s.type = 'R' AND e.position IS NOT NULL
),
api_done AS (
  SELECT DISTINCT season, round FROM ${catalog}.bronze.jolpica_api_pages WHERE endpoint = 'results'
),
jolpica AS (
  SELECT
    r.season, r.round, r.event_name, r.circuit_id, r.race_date, s.race_start_utc,
    coalesce(s.has_sprint, false) AS is_sprint,
    CASE
      WHEN r.is_cancelled THEN 'cancelled'
      WHEN d.round_id IS NOT NULL OR a.round IS NOT NULL THEN 'completed'
      ELSE 'scheduled'
    END AS status,
    'jolpica' AS source
  FROM r
  LEFT JOIN sess s ON s.round_id = r.round_pk
  LEFT JOIN dump_done d ON d.round_id = r.round_pk
  LEFT JOIN api_done a ON a.season = r.season AND a.round = r.round
),
seed AS (
  SELECT
    int(season), int(round), grand_prix, circuit_ref, to_date(race_date),
    CAST(NULL AS TIMESTAMP), is_sprint = 'true', 'scheduled', 'manual_seed_2027'
  FROM ${catalog}.bronze.seed_calendar_2027
  -- The manual calendar only applies until Jolpica publishes that season.
  WHERE int(season) NOT IN (SELECT DISTINCT season FROM r)
)
SELECT * FROM jolpica
UNION ALL
SELECT * FROM seed;

CREATE OR REPLACE TABLE ${catalog}.silver.sessions
COMMENT 'One row per session (FP1/FP2/FP3/SQ/S/Q/R) with keys into every source'
AS
WITH jol AS (
  SELECT
    int(y.year) AS season, int(r.number) AS round,
    ${catalog}.silver.session_code(s.type) AS session_code,
    min(try_cast(s.timestamp AS TIMESTAMP)) AS start_utc,
    bool_and(s.is_cancelled = 't') AS is_cancelled
  FROM ${catalog}.bronze.jolpica_dump_session s
  JOIN ${catalog}.bronze.jolpica_dump_round r ON s.round_id = r.id
  JOIN ${catalog}.bronze.jolpica_dump_season y ON r.season_id = y.id
  WHERE int(y.year) >= 2001 AND r.number IS NOT NULL AND s.type <> 'PQ'
  GROUP BY ALL
),
openf1_raw AS (
  SELECT *, _metadata.file_path AS snapshot_file
  FROM read_files(
    '/Volumes/${catalog}/bronze/landing/openf1/_index/sessions/*/*.json',
    format => 'json', multiLine => true
  )
),
openf1 AS (
  SELECT
    int(year) AS season, int(session_key) AS session_key, int(meeting_key) AS meeting_key,
    ${catalog}.silver.session_code(session_name) AS session_code,
    try_cast(date_start AS TIMESTAMP) AS start_utc,
    try_cast(date_end AS TIMESTAMP) AS end_utc
  FROM (
    SELECT *, row_number() OVER (PARTITION BY session_key ORDER BY snapshot_file DESC) AS rn
    FROM openf1_raw
  )
  WHERE rn = 1 AND session_name NOT LIKE 'Day%' AND NOT coalesce(is_cancelled, false)
),
openf1_matched AS (
  -- A session belongs to the round whose race falls 0-4 days after it starts (UTC).
  SELECT o.*, e.round
  FROM openf1 o
  JOIN ${catalog}.silver.events e
    ON e.season = o.season AND datediff(e.race_date, to_date(o.start_utc)) BETWEEN -1 AND 4
),
fastf1 AS (
  SELECT DISTINCT int(_year) AS season, int(_round) AS round, ${catalog}.silver.session_code(_session) AS session_code
  FROM ${catalog}.bronze.fastf1_laps
  UNION
  SELECT DISTINCT int(_year), int(_round), ${catalog}.silver.session_code(_session)
  FROM ${catalog}.bronze.fastf1_session_status
),
keys AS (
  SELECT season, round, session_code FROM jol
  UNION SELECT season, round, session_code FROM openf1_matched
  UNION SELECT season, round, session_code FROM fastf1
)
SELECT
  ${catalog}.silver.session_id(k.season, k.round, k.session_code) AS session_id,
  k.season, k.round, k.session_code,
  coalesce(o.start_utc, j.start_utc) AS start_utc,
  o.end_utc,
  coalesce(j.is_cancelled, false) AS is_cancelled,
  o.session_key AS openf1_session_key,
  o.meeting_key AS openf1_meeting_key,
  f.season IS NOT NULL AS has_fastf1
FROM keys k
LEFT JOIN jol j USING (season, round, session_code)
LEFT JOIN openf1_matched o USING (season, round, session_code)
LEFT JOIN fastf1 f USING (season, round, session_code);

CREATE OR REPLACE TABLE ${catalog}.silver.drivers
COMMENT 'Every driver who raced since 2001 (driver_id = Jolpica reference)'
AS
WITH career AS (
  SELECT td.driver_id, min(int(y.year)) AS debut_season, max(int(y.year)) AS last_season
  FROM ${catalog}.bronze.jolpica_dump_roundentry re
  JOIN ${catalog}.bronze.jolpica_dump_teamdriver td ON re.team_driver_id = td.id
  JOIN ${catalog}.bronze.jolpica_dump_season y ON td.season_id = y.id
  GROUP BY td.driver_id
)
SELECT
  d.reference AS driver_id,
  d.abbreviation AS code,
  int(d.permanent_car_number) AS permanent_number,
  d.forename AS first_name,
  d.surname AS last_name,
  concat_ws(' ', d.forename, d.surname) AS full_name,
  to_date(d.date_of_birth) AS date_of_birth,
  d.nationality,
  d.country_code,
  c.debut_season,
  c.last_season
FROM ${catalog}.bronze.jolpica_dump_driver d
JOIN career c ON c.driver_id = d.id
WHERE c.last_season >= 2001;

CREATE OR REPLACE TABLE ${catalog}.silver.constructors
COMMENT 'Every constructor that entered since 2001 (constructor_id = Jolpica reference)'
AS
WITH used AS (
  SELECT DISTINCT td.team_id
  FROM ${catalog}.bronze.jolpica_dump_teamdriver td
  JOIN ${catalog}.bronze.jolpica_dump_season y ON td.season_id = y.id
  WHERE int(y.year) >= 2001
)
SELECT
  t.reference AS constructor_id,
  t.name,
  t.nationality,
  t.country_code,
  t.primary_color AS color_hex
FROM ${catalog}.bronze.jolpica_dump_team t
WHERE t.id IN (SELECT team_id FROM used);

CREATE OR REPLACE TABLE ${catalog}.silver.entries
COMMENT 'Who drove which car for which team at each round; maps car numbers to driver_id. Includes practice-only (reserve) drivers: use is_race_entry for race analysis.'
AS
WITH sessions_by_entry AS (
  SELECT
    se.round_entry_id,
    array_sort(collect_set(${catalog}.silver.session_code(s.type))) AS session_codes
  FROM ${catalog}.bronze.jolpica_dump_sessionentry se
  JOIN ${catalog}.bronze.jolpica_dump_session s ON se.session_id = s.id
  GROUP BY se.round_entry_id
),
dump AS (
  SELECT
    int(y.year) AS season, int(r.number) AS round, d.reference AS driver_id,
    t.reference AS constructor_id, int(re.car_number) AS car_number,
    coalesce(array_contains(sbe.session_codes, 'R'), false) AS is_race_entry,
    sbe.session_codes,
    'jolpica_dump' AS source
  FROM ${catalog}.bronze.jolpica_dump_roundentry re
  LEFT JOIN sessions_by_entry sbe ON sbe.round_entry_id = re.id
  JOIN ${catalog}.bronze.jolpica_dump_round r ON re.round_id = r.id
  JOIN ${catalog}.bronze.jolpica_dump_season y ON r.season_id = y.id
  JOIN ${catalog}.bronze.jolpica_dump_teamdriver td ON re.team_driver_id = td.id
  JOIN ${catalog}.bronze.jolpica_dump_driver d ON td.driver_id = d.id
  JOIN ${catalog}.bronze.jolpica_dump_team t ON td.team_id = t.id
  WHERE int(y.year) >= 2001 AND r.number IS NOT NULL
),
api AS (
  SELECT DISTINCT
    p.season, p.round, x.Driver.driverId, x.Constructor.constructorId, int(x.number),
    true, array('R'), 'jolpica_api'
  FROM ${catalog}.bronze.jolpica_api_pages p
  LATERAL VIEW explode(
    get(from_json(p.payload, 'STRUCT<MRData: STRUCT<RaceTable: STRUCT<Races: ARRAY<STRUCT<Results: ARRAY<STRUCT<number: STRING, Driver: STRUCT<driverId: STRING>, Constructor: STRUCT<constructorId: STRING>>>>>>>>').MRData.RaceTable.Races, 0).Results
  ) AS x
  WHERE p.endpoint = 'results'
)
SELECT * FROM dump
UNION ALL
SELECT * FROM api a
WHERE NOT EXISTS (SELECT 1 FROM dump d WHERE d.season = a.season AND d.round = a.round);

CREATE OR REPLACE TABLE ${catalog}.silver.driver_seasons
COMMENT 'Driver x constructor per season (race entries only) with race teammates: basis of the teammate network'
AS
WITH ds AS (
  SELECT season, driver_id, constructor_id, count(*) AS races
  FROM ${catalog}.silver.entries
  WHERE is_race_entry
  GROUP BY ALL
)
SELECT
  a.season, a.driver_id, a.constructor_id, a.races,
  array_sort(collect_set(b.driver_id)) AS teammate_ids
FROM ds a
LEFT JOIN ds b
  ON a.season = b.season AND a.constructor_id = b.constructor_id AND a.driver_id <> b.driver_id
GROUP BY a.season, a.driver_id, a.constructor_id, a.races;

CREATE OR REPLACE TABLE ${catalog}.silver.session_drivers
COMMENT 'Car number -> driver per session (FastF1/OpenF1 sessions). Mapped via Jolpica entry, FastF1 driver id, exact name match, or a generated id for practice-only drivers Jolpica does not know.'
AS
WITH src AS (
  SELECT
    ${catalog}.silver.session_id(int(_year), int(_round), ${catalog}.silver.session_code(_session)) AS session_id,
    int(_year) AS season, int(_round) AS round, int(DriverNumber) AS car_number,
    nullif(nullif(DriverId, ''), 'nan') AS src_driver_id,
    nullif(nullif(Abbreviation, ''), 'nan') AS code,
    nullif(FirstName, '') AS first_name, nullif(LastName, '') AS last_name,
    nullif(TeamName, '') AS team_name, 1 AS pref
  FROM ${catalog}.bronze.fastf1_results
  UNION ALL
  SELECT
    s.session_id, s.season, s.round, int(d.driver_number),
    CAST(NULL AS STRING), d.name_acronym, d.first_name, d.last_name, d.team_name, 2
  FROM ${catalog}.bronze.openf1_drivers d
  JOIN ${catalog}.silver.sessions s ON s.openf1_session_key = int(d.session_key)
),
one AS (
  SELECT *,
    -- ASCII-folded lowercase names for matching across sources
    translate(lower(first_name), 'áàâäãåéèêëíìîïóòôöõúùûüñçøšžćčłıý', 'aaaaaaeeeeiiiiooooouuuuncoszcclly') AS first_key,
    translate(lower(last_name), 'áàâäãåéèêëíìîïóòôöõúùûüñçøšžćčłıý', 'aaaaaaeeeeiiiiooooouuuuncoszcclly') AS last_key
  FROM src
  -- F1 car numbers are 1-99; feeds occasionally carry junk numbers (0, 65535).
  WHERE car_number BETWEEN 1 AND 99
  QUALIFY row_number() OVER (PARTITION BY session_id, car_number ORDER BY pref) = 1
),
known AS (
  SELECT driver_id,
    translate(lower(first_name), 'áàâäãåéèêëíìîïóòôöõúùûüñçøšžćčłıý', 'aaaaaaeeeeiiiiooooouuuuncoszcclly') AS first_key,
    translate(lower(last_name), 'áàâäãåéèêëíìîïóòôöõúùûüñçøšžćčłıý', 'aaaaaaeeeeiiiiooooouuuuncoszcclly') AS last_key
  FROM ${catalog}.silver.drivers
),
name_match AS (
  SELECT first_key, last_key, min(driver_id) AS driver_id, count(*) AS n
  FROM known GROUP BY ALL
)
SELECT
  o.session_id, o.season, o.round, o.car_number,
  coalesce(
    e.driver_id, dj.driver_id, nm.driver_id,
    regexp_replace(concat(o.first_key, '_', o.last_key), '[^a-z0-9_]', '_')
  ) AS driver_id,
  CASE
    WHEN e.driver_id IS NOT NULL THEN 'jolpica_entry'
    WHEN dj.driver_id IS NOT NULL THEN 'fastf1_driver_id'
    WHEN nm.driver_id IS NOT NULL THEN 'name_match'
    ELSE 'generated'
  END AS mapped_via,
  o.code, concat_ws(' ', o.first_name, o.last_name) AS full_name, o.team_name,
  o.first_name, o.last_name
FROM one o
LEFT JOIN ${catalog}.silver.entries e
  ON e.season = o.season AND e.round = o.round AND e.car_number = o.car_number
LEFT JOIN ${catalog}.silver.drivers dj ON dj.driver_id = o.src_driver_id
LEFT JOIN name_match nm ON nm.first_key = o.first_key AND nm.last_key = o.last_key AND nm.n = 1
-- Entries with no identity at all (OpenF1 2023 Hungary FP1 lists support-series cars by
-- 3-letter code only) are not F1 drivers.
WHERE coalesce(e.driver_id, dj.driver_id, nm.driver_id, o.first_key, o.last_key) IS NOT NULL;

-- Practice-only drivers Jolpica doesn't know (e.g. 2018 FP1 test drivers) join silver.drivers
-- with a generated id so every lap has a valid driver.
INSERT INTO ${catalog}.silver.drivers (driver_id, code, first_name, last_name, full_name)
SELECT driver_id, max(code), max(first_name), max(last_name), max(full_name)
FROM ${catalog}.silver.session_drivers
WHERE mapped_via = 'generated'
  AND driver_id NOT IN (SELECT driver_id FROM ${catalog}.silver.drivers)
GROUP BY driver_id;
