-- Inputs for the starts and error-rate traits.

CREATE OR REPLACE TABLE ${catalog}.gold.race_starts
COMMENT 'Lap-1 result per race start: places gained vs grid, and vs what is normal for that grid slot in that regulation era'
AS
WITH s AS (
  SELECT
    r.session_id, r.season, r.round, r.driver_id, r.constructor_id, r.grid,
    l.position AS lap1_position, se.regulation_era
  FROM ${catalog}.silver.results r
  JOIN ${catalog}.silver.laps l
    ON l.session_id = r.session_id AND l.driver_id = r.driver_id AND l.lap = 1
  JOIN ${catalog}.silver.seasons se ON se.season = r.season
  -- grid 0 = pit-lane start; drivers out on lap 1 have no lap-1 position (counted as errors)
  WHERE r.session_code = 'R' AND r.grid > 0 AND l.position IS NOT NULL
),
expected AS (
  SELECT regulation_era, grid, avg(grid - lap1_position) AS expected_gain, count(*) AS n
  FROM s GROUP BY regulation_era, grid
)
SELECT
  s.*, s.grid - s.lap1_position AS places_gained, e.expected_gain,
  (s.grid - s.lap1_position) - e.expected_gain AS gain_vs_expected
FROM s
JOIN expected e USING (regulation_era, grid);

CREATE OR REPLACE TABLE ${catalog}.gold.start_teammate_gaps
COMMENT 'Teammate starts: gap = places LOST vs expected by driver_a minus driver_b on lap 1 (positive = driver_a worse starter)'
AS
SELECT
  a.session_id, a.season, a.round, a.constructor_id,
  a.driver_id AS driver_a, b.driver_id AS driver_b,
  b.gain_vs_expected - a.gain_vs_expected AS gap_places
FROM ${catalog}.gold.race_starts a
JOIN ${catalog}.gold.race_starts b
  ON a.session_id = b.session_id AND a.constructor_id = b.constructor_id AND a.driver_id < b.driver_id;
