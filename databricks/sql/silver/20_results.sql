-- Race / sprint classification and qualifying, from the Jolpica dump plus API pages for
-- rounds newer than the dump.

CREATE OR REPLACE TABLE ${catalog}.silver.results
COMMENT 'Race (R) and sprint (S) classification per driver; status_class groups retirements'
AS
WITH dump AS (
  SELECT
    int(y.year) AS season, int(r.number) AS round,
    ${catalog}.silver.session_code(s.type) AS session_code,
    d.reference AS driver_id, t.reference AS constructor_id, int(re.car_number) AS car_number,
    int(se.grid) AS grid, int(se.position) AS position, se.is_classified = 't' AS is_classified,
    int(se.status) AS status_code, se.detail AS status_detail,
    double(se.points) AS points, int(se.laps_completed) AS laps_completed,
    ${catalog}.silver.time_to_ms(se.time) AS race_time_ms,
    int(se.fastest_lap_rank) AS fastest_lap_rank,
    'jolpica_dump' AS source
  FROM ${catalog}.bronze.jolpica_dump_sessionentry se
  JOIN ${catalog}.bronze.jolpica_dump_session s ON se.session_id = s.id
  JOIN ${catalog}.bronze.jolpica_dump_round r ON s.round_id = r.id
  JOIN ${catalog}.bronze.jolpica_dump_season y ON r.season_id = y.id
  JOIN ${catalog}.bronze.jolpica_dump_roundentry re ON se.round_entry_id = re.id
  JOIN ${catalog}.bronze.jolpica_dump_teamdriver td ON re.team_driver_id = td.id
  JOIN ${catalog}.bronze.jolpica_dump_driver d ON td.driver_id = d.id
  JOIN ${catalog}.bronze.jolpica_dump_team t ON td.team_id = t.id
  WHERE s.type IN ('R', 'SR') AND int(y.year) >= 2001 AND r.number IS NOT NULL
),
api_pages AS (
  SELECT
    season, round,
    CASE endpoint WHEN 'results' THEN 'R' ELSE 'S' END AS session_code,
    get(from_json(payload, 'STRUCT<MRData: STRUCT<RaceTable: STRUCT<Races: ARRAY<STRUCT<
        Results: ARRAY<STRUCT<number: STRING, position: STRING, positionText: STRING, points: STRING,
          grid: STRING, laps: STRING, status: STRING, Driver: STRUCT<driverId: STRING>,
          Constructor: STRUCT<constructorId: STRING>, Time: STRUCT<millis: STRING>,
          FastestLap: STRUCT<rank: STRING>>>,
        SprintResults: ARRAY<STRUCT<number: STRING, position: STRING, positionText: STRING, points: STRING,
          grid: STRING, laps: STRING, status: STRING, Driver: STRUCT<driverId: STRING>,
          Constructor: STRUCT<constructorId: STRING>, Time: STRUCT<millis: STRING>,
          FastestLap: STRUCT<rank: STRING>>>>>>>>').MRData.RaceTable.Races, 0) AS race
  FROM ${catalog}.bronze.jolpica_api_pages
  WHERE endpoint IN ('results', 'sprint')
),
api AS (
  SELECT
    p.season, p.round, p.session_code,
    x.Driver.driverId, x.Constructor.constructorId, int(x.number),
    int(x.grid), int(x.position), x.positionText RLIKE '^[0-9]+$',
    CAST(NULL AS INT), x.status,
    double(x.points), int(x.laps), bigint(x.Time.millis), int(x.FastestLap.rank),
    'jolpica_api'
  FROM api_pages p
  LATERAL VIEW explode(coalesce(p.race.Results, p.race.SprintResults)) AS x
),
combined AS (
  SELECT * FROM dump
  UNION ALL
  SELECT * FROM api a
  WHERE NOT EXISTS (
    SELECT 1 FROM dump d
    WHERE d.season = a.season AND d.round = a.round AND d.session_code = a.session_code
  )
)
SELECT
  ${catalog}.silver.session_id(season, round, session_code) AS session_id,
  *,
  ${catalog}.silver.status_class(status_code, status_detail) AS status_class
FROM combined;

CREATE OR REPLACE TABLE ${catalog}.silver.qualifying
COMMENT 'Qualifying (Q) and sprint qualifying (SQ): final position and best time per part. 2001-2005 single-session formats land in q1_ms.'
AS
WITH parts AS (
  SELECT
    int(y.year) AS season, int(r.number) AS round, s.type AS part,
    d.reference AS driver_id, t.reference AS constructor_id, int(re.car_number) AS car_number,
    int(se.position) AS position, se.id AS session_entry_id
  FROM ${catalog}.bronze.jolpica_dump_sessionentry se
  JOIN ${catalog}.bronze.jolpica_dump_session s ON se.session_id = s.id
  JOIN ${catalog}.bronze.jolpica_dump_round r ON s.round_id = r.id
  JOIN ${catalog}.bronze.jolpica_dump_season y ON r.season_id = y.id
  JOIN ${catalog}.bronze.jolpica_dump_roundentry re ON se.round_entry_id = re.id
  JOIN ${catalog}.bronze.jolpica_dump_teamdriver td ON re.team_driver_id = td.id
  JOIN ${catalog}.bronze.jolpica_dump_driver d ON td.driver_id = d.id
  JOIN ${catalog}.bronze.jolpica_dump_team t ON td.team_id = t.id
  WHERE s.type IN ('Q1', 'Q2', 'Q3', 'QA', 'QB', 'QO', 'SQ1', 'SQ2', 'SQ3')
    AND int(y.year) >= 2001 AND r.number IS NOT NULL
),
best AS (
  -- The entry's flagged fastest lap; older seasons only store that lap.
  SELECT
    session_entry_id,
    coalesce(
      min(CASE WHEN is_entry_fastest_lap = 't' THEN ${catalog}.silver.time_to_ms(time) END),
      min(${catalog}.silver.time_to_ms(time))
    ) AS best_ms
  FROM ${catalog}.bronze.jolpica_dump_lap
  GROUP BY session_entry_id
),
p AS (
  SELECT
    parts.*, best.best_ms,
    CASE WHEN part LIKE 'SQ%' THEN 'SQ' ELSE 'Q' END AS session_code,
    -- How far the driver got: the deepest part holds the final position.
    CASE part
      WHEN 'Q3' THEN 6 WHEN 'SQ3' THEN 6 WHEN 'Q2' THEN 5 WHEN 'SQ2' THEN 5
      WHEN 'Q1' THEN 4 WHEN 'SQ1' THEN 4 WHEN 'QA' THEN 3 WHEN 'QB' THEN 2 WHEN 'QO' THEN 1
    END AS depth
  FROM parts
  LEFT JOIN best ON best.session_entry_id = parts.session_entry_id
),
dump AS (
  SELECT
    season, round, session_code, driver_id,
    max_by(constructor_id, depth) AS constructor_id,
    max_by(car_number, depth) AS car_number,
    max_by(position, depth) AS position,
    coalesce(
      min(CASE WHEN part IN ('Q1', 'SQ1') THEN best_ms END),
      min(CASE WHEN part = 'QA' THEN best_ms END),
      min(CASE WHEN part = 'QB' THEN best_ms END),
      min(CASE WHEN part = 'QO' THEN best_ms END)
    ) AS q1_ms,
    min(CASE WHEN part IN ('Q2', 'SQ2') THEN best_ms END) AS q2_ms,
    min(CASE WHEN part IN ('Q3', 'SQ3') THEN best_ms END) AS q3_ms,
    CASE
      WHEN max(depth) >= 4 THEN 'knockout'
      WHEN max(CASE WHEN part = 'QA' THEN 1 END) = 1 THEN 'aggregate'
      ELSE 'single_session'
    END AS format,
    'jolpica_dump' AS source
  FROM p
  GROUP BY season, round, session_code, driver_id
),
api AS (
  SELECT
    p.season, p.round, 'Q', x.Driver.driverId, x.Constructor.constructorId, int(x.number),
    int(x.position),
    ${catalog}.silver.time_to_ms(x.Q1), ${catalog}.silver.time_to_ms(x.Q2),
    ${catalog}.silver.time_to_ms(x.Q3), 'knockout', 'jolpica_api'
  FROM ${catalog}.bronze.jolpica_api_pages p
  LATERAL VIEW explode(
    get(from_json(p.payload, 'STRUCT<MRData: STRUCT<RaceTable: STRUCT<Races: ARRAY<STRUCT<
        QualifyingResults: ARRAY<STRUCT<number: STRING, position: STRING, Q1: STRING, Q2: STRING,
          Q3: STRING, Driver: STRUCT<driverId: STRING>, Constructor: STRUCT<constructorId: STRING>>>>>>>>').MRData.RaceTable.Races, 0).QualifyingResults
  ) AS x
  WHERE p.endpoint = 'qualifying'
),
combined AS (
  SELECT * FROM dump
  UNION ALL
  SELECT * FROM api a
  WHERE NOT EXISTS (SELECT 1 FROM dump d WHERE d.season = a.season AND d.round = a.round AND d.session_code = 'Q')
)
SELECT
  ${catalog}.silver.session_id(season, round, session_code) AS session_id,
  *,
  least(q1_ms, q2_ms, q3_ms) AS best_ms
FROM combined;
