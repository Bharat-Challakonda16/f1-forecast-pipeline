-- High-frequency telemetry stays in bronze (~880M rows); silver exposes it as typed,
-- conformed VIEWS so there is no second copy. Filter by session_id when querying.

CREATE OR REPLACE VIEW ${catalog}.silver.car_telemetry
COMMENT 'Car telemetry (~4 Hz): speed, RPM, gear, throttle, brake, DRS. FastF1 2018-22, OpenF1 2023+.'
AS
SELECT
  ${catalog}.silver.session_id(int(t._year), int(t._round), ${catalog}.silver.session_code(t._session)) AS session_id,
  int(t._year) AS season, int(t._round) AS round,
  ${catalog}.silver.session_code(t._session) AS session_code,
  sd.driver_id, int(t.DriverNumber) AS car_number,
  try_cast(left(t.Date, 23) AS TIMESTAMP) AS sample_utc,
  double(t.Speed) AS speed_kph, double(t.RPM) AS rpm, int(double(t.nGear)) AS gear,
  double(t.Throttle) AS throttle_pct, try_cast(t.Brake AS BOOLEAN) AS brake,
  int(double(t.DRS)) AS drs, 'fastf1' AS source
FROM ${catalog}.bronze.fastf1_car_data t
LEFT JOIN ${catalog}.silver.session_drivers sd
  ON sd.session_id = ${catalog}.silver.session_id(int(t._year), int(t._round), ${catalog}.silver.session_code(t._session))
 AND sd.car_number = int(t.DriverNumber)
UNION ALL
SELECT
  s.session_id, s.season, s.round, s.session_code,
  sd.driver_id, int(c.driver_number),
  try_cast(c.date AS TIMESTAMP),
  double(c.speed), double(c.rpm), int(c.n_gear),
  double(c.throttle), double(c.brake) > 0,
  int(c.drs), 'openf1'
FROM ${catalog}.bronze.openf1_car_data c
JOIN ${catalog}.silver.sessions s ON s.openf1_session_key = int(c.session_key)
LEFT JOIN ${catalog}.silver.session_drivers sd
  ON sd.session_id = s.session_id AND sd.car_number = int(c.driver_number);

CREATE OR REPLACE VIEW ${catalog}.silver.car_positions
COMMENT 'Car position on track (~4 Hz), x/y/z in the source coordinate frame. FastF1 2018-22, OpenF1 2023+.'
AS
SELECT
  ${catalog}.silver.session_id(int(p._year), int(p._round), ${catalog}.silver.session_code(p._session)) AS session_id,
  int(p._year) AS season, int(p._round) AS round,
  ${catalog}.silver.session_code(p._session) AS session_code,
  sd.driver_id, int(p.DriverNumber) AS car_number,
  try_cast(left(p.Date, 23) AS TIMESTAMP) AS sample_utc,
  double(p.X) AS x, double(p.Y) AS y, double(p.Z) AS z,
  p.Status AS on_track_status, 'fastf1' AS source
FROM ${catalog}.bronze.fastf1_position p
LEFT JOIN ${catalog}.silver.session_drivers sd
  ON sd.session_id = ${catalog}.silver.session_id(int(p._year), int(p._round), ${catalog}.silver.session_code(p._session))
 AND sd.car_number = int(p.DriverNumber)
UNION ALL
SELECT
  s.session_id, s.season, s.round, s.session_code,
  sd.driver_id, int(l.driver_number),
  try_cast(l.date AS TIMESTAMP),
  double(l.x), double(l.y), double(l.z),
  CAST(NULL AS STRING), 'openf1'
FROM ${catalog}.bronze.openf1_location l
JOIN ${catalog}.silver.sessions s ON s.openf1_session_key = int(l.session_key)
LEFT JOIN ${catalog}.silver.session_drivers sd
  ON sd.session_id = s.session_id AND sd.car_number = int(l.driver_number);
