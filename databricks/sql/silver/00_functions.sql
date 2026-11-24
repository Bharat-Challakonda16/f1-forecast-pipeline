-- Shared helpers for the silver layer. ${catalog} is substituted by the runner.

CREATE OR REPLACE FUNCTION ${catalog}.silver.time_to_ms(t STRING)
RETURNS BIGINT
COMMENT 'Lap/race time text to milliseconds: "1:31.929", "00:01:42.21", "02:13:23.6", "83.456" (seconds)'
RETURN CASE
  WHEN t IS NULL OR trim(t) IN ('', 'nan', 'NaT', 'None') THEN NULL
  ELSE CAST(ROUND((
      try_cast(try_element_at(split(trim(t), ':'), -1) AS DOUBLE)
      + coalesce(try_cast(try_element_at(split(trim(t), ':'), -2) AS DOUBLE), 0) * 60
      + coalesce(try_cast(try_element_at(split(trim(t), ':'), -3) AS DOUBLE), 0) * 3600
    ) * 1000) AS BIGINT)
END;

CREATE OR REPLACE FUNCTION ${catalog}.silver.session_code(raw STRING)
RETURNS STRING
COMMENT 'Conformed session code across Jolpica, FastF1 and OpenF1: FP1 FP2 FP3 SQ S Q R'
RETURN CASE
  WHEN raw IN ('FP1', 'Practice 1') THEN 'FP1'
  WHEN raw IN ('FP2', 'Practice 2') THEN 'FP2'
  WHEN raw IN ('FP3', 'Practice 3') THEN 'FP3'
  WHEN raw IN ('SQ', 'SS', 'SQ1', 'SQ2', 'SQ3', 'Sprint Qualifying', 'Sprint Shootout') THEN 'SQ'
  WHEN raw IN ('S', 'SR', 'Sprint') THEN 'S'
  WHEN raw IN ('Q', 'Q1', 'Q2', 'Q3', 'QA', 'QB', 'QO', 'Qualifying') THEN 'Q'
  WHEN raw IN ('R', 'Race') THEN 'R'
END;

CREATE OR REPLACE FUNCTION ${catalog}.silver.session_id(season INT, round INT, code STRING)
RETURNS STRING
COMMENT 'Conformed session key, e.g. 2025_17_Q'
RETURN concat(season, '_', lpad(CAST(round AS STRING), 2, '0'), '_', code);

CREATE OR REPLACE FUNCTION ${catalog}.silver.status_class(status_code INT, detail STRING)
RETURNS STRING
COMMENT 'finished | accident | mechanical | other | disqualified | did_not_start | did_not_qualify'
RETURN CASE
  WHEN status_code IN (0, 1) THEN 'finished'
  WHEN status_code = 10 THEN 'accident'
  WHEN status_code = 20 THEN 'disqualified'
  WHEN status_code = 30 THEN 'did_not_start'
  WHEN status_code IN (40, 41) THEN 'did_not_qualify'
  -- API rows carry only text; dump rows with code 11 are split by their text.
  WHEN status_code IS NULL AND (detail = 'Finished' OR detail RLIKE '^\\+[0-9]+ Laps?$' OR detail = 'Lapped') THEN 'finished'
  WHEN status_code IS NULL AND detail RLIKE '(?i)disqualified' THEN 'disqualified'
  WHEN status_code IS NULL AND detail RLIKE '(?i)did not start|withdrew|not started' THEN 'did_not_start'
  WHEN status_code IS NULL AND detail RLIKE '(?i)did not qualify|not qualified|107' THEN 'did_not_qualify'
  WHEN status_code IS NULL AND detail RLIKE '(?i)accident|collision|spun|crash' THEN 'accident'
  WHEN detail RLIKE '(?i)engine|gearbox|transmission|clutch|hydraulic|electr|drivetrain|brake|suspension|power unit|turbo|\\bERS\\b|\\bMGU|kers|energy store|battery|oil|water|fuel|overheat|exhaust|drive ?shaft|half ?shaft|throttle|steering|radiator|cooling|mechanical|power loss|pneumatic|differential|wheel|axle|pressure|track rod|chassis|technical|alternator|spark|injection|vibration|gear|launch control|out of fuel'
    THEN 'mechanical'
  WHEN status_code IS NULL AND detail IS NULL THEN NULL
  ELSE 'other'
END;
