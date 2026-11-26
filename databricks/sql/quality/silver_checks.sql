-- Silver data quality checks. Each statement returns one row:
--   check_name, severity ('error' fails the build, 'warn' is reported), violations, sample
-- Run by databricks/notebooks/21_silver_quality.py (results -> silver.dq_results) and by
-- `python -m f1_forecast.ops.run_sql databricks/sql/quality --checks`.

-- ------------------------------------------------------------------ unique natural keys

SELECT 'unique: events(season, round)' AS check_name, 'error' AS severity, count(*) AS violations,
       array_join(slice(collect_list(concat(season, '/', round)), 1, 5), ', ') AS sample
FROM (SELECT season, round FROM ${catalog}.silver.events GROUP BY ALL HAVING count(*) > 1);

SELECT 'unique: sessions(session_id)', 'error', count(*), array_join(slice(collect_list(session_id), 1, 5), ', ')
FROM (SELECT session_id FROM ${catalog}.silver.sessions GROUP BY ALL HAVING count(*) > 1);

SELECT 'unique: drivers(driver_id)', 'error', count(*), array_join(slice(collect_list(driver_id), 1, 5), ', ')
FROM (SELECT driver_id FROM ${catalog}.silver.drivers GROUP BY ALL HAVING count(*) > 1);

SELECT 'unique: constructors(constructor_id)', 'error', count(*), array_join(slice(collect_list(constructor_id), 1, 5), ', ')
FROM (SELECT constructor_id FROM ${catalog}.silver.constructors GROUP BY ALL HAVING count(*) > 1);

SELECT 'unique: entries(season, round, car_number)', 'error', count(*),
       array_join(slice(collect_list(concat(season, '/', round, '#', car_number)), 1, 5), ', ')
FROM (SELECT season, round, car_number FROM ${catalog}.silver.entries GROUP BY ALL HAVING count(*) > 1);

SELECT 'unique: session_drivers(session_id, car_number)', 'error', count(*),
       array_join(slice(collect_list(concat(session_id, '#', car_number)), 1, 5), ', ')
FROM (SELECT session_id, car_number FROM ${catalog}.silver.session_drivers GROUP BY ALL HAVING count(*) > 1);

SELECT 'unique: session_drivers one driver per car', 'error', count(*),
       array_join(slice(collect_list(concat(session_id, ':', driver_id)), 1, 5), ', ')
FROM (SELECT session_id, driver_id FROM ${catalog}.silver.session_drivers GROUP BY ALL HAVING count(*) > 1);

SELECT 'unique: results(session_id, driver_id)', 'error', count(*),
       array_join(slice(collect_list(concat(session_id, ':', driver_id)), 1, 5), ', ')
FROM (SELECT session_id, driver_id FROM ${catalog}.silver.results GROUP BY ALL HAVING count(*) > 1);

SELECT 'unique: qualifying(session_id, driver_id)', 'error', count(*),
       array_join(slice(collect_list(concat(session_id, ':', driver_id)), 1, 5), ', ')
FROM (SELECT session_id, driver_id FROM ${catalog}.silver.qualifying GROUP BY ALL HAVING count(*) > 1);

SELECT 'unique: laps(session_id, driver_id, lap)', 'error', count(*),
       array_join(slice(collect_list(concat(session_id, ':', driver_id, ':', lap)), 1, 5), ', ')
FROM (SELECT session_id, driver_id, lap FROM ${catalog}.silver.laps GROUP BY ALL HAVING count(*) > 1);

SELECT 'unique: pit_stops(session_id, driver_id, stop_number)', 'error', count(*),
       array_join(slice(collect_list(concat(session_id, ':', driver_id, ':', stop_number)), 1, 5), ', ')
FROM (SELECT session_id, driver_id, stop_number FROM ${catalog}.silver.pit_stops GROUP BY ALL HAVING count(*) > 1);

SELECT 'unique: weather_hourly(circuit_id, time_utc)', 'error', count(*),
       array_join(slice(collect_list(concat(circuit_id, '@', time_utc)), 1, 5), ', ')
FROM (SELECT circuit_id, time_utc FROM ${catalog}.silver.weather_hourly GROUP BY ALL HAVING count(*) > 1);

SELECT 'unique: weather_forecasts(season, round, source, issued, lead, time)', 'error', count(*),
       array_join(slice(collect_list(concat(season, '/', round, ' ', time_utc)), 1, 5), ', ')
FROM (SELECT season, round, source, issued_utc, lead_days, time_utc
      FROM ${catalog}.silver.weather_forecasts GROUP BY ALL HAVING count(*) > 1);

SELECT 'unique: weather_ensemble(season, round, issued, time, member)', 'error', count(*),
       array_join(slice(collect_list(concat(season, '/', round, ' ', time_utc)), 1, 5), ', ')
FROM (SELECT season, round, issued_utc, time_utc, member
      FROM ${catalog}.silver.weather_ensemble GROUP BY ALL HAVING count(*) > 1);

SELECT 'fk: weather_forecasts -> events', 'error', count(*),
       array_join(slice(collect_set(concat(f.season, '/', f.round)), 1, 5), ', ')
FROM (SELECT DISTINCT season, round FROM ${catalog}.silver.weather_forecasts) f
LEFT ANTI JOIN ${catalog}.silver.events e USING (season, round);

-- ------------------------------------------------------------------ required values

SELECT 'not null: laps keys', 'error', count(*), array_join(slice(collect_list(session_id), 1, 5), ', ')
FROM ${catalog}.silver.laps WHERE session_id IS NULL OR driver_id IS NULL OR lap IS NULL;

SELECT 'not null: results keys', 'error', count(*), array_join(slice(collect_list(session_id), 1, 5), ', ')
FROM ${catalog}.silver.results WHERE driver_id IS NULL OR constructor_id IS NULL;

-- ------------------------------------------------------------------ referential integrity

SELECT 'fk: results.driver_id -> drivers', 'error', count(*), array_join(slice(collect_set(r.driver_id), 1, 5), ', ')
FROM ${catalog}.silver.results r LEFT ANTI JOIN ${catalog}.silver.drivers d USING (driver_id);

SELECT 'fk: results.constructor_id -> constructors', 'error', count(*), array_join(slice(collect_set(r.constructor_id), 1, 5), ', ')
FROM ${catalog}.silver.results r LEFT ANTI JOIN ${catalog}.silver.constructors c USING (constructor_id);

SELECT 'fk: results -> events', 'error', count(*), array_join(slice(collect_set(r.session_id), 1, 5), ', ')
FROM ${catalog}.silver.results r LEFT ANTI JOIN ${catalog}.silver.events e ON e.season = r.season AND e.round = r.round;

SELECT 'fk: qualifying.driver_id -> drivers', 'error', count(*), array_join(slice(collect_set(q.driver_id), 1, 5), ', ')
FROM ${catalog}.silver.qualifying q LEFT ANTI JOIN ${catalog}.silver.drivers d USING (driver_id);

SELECT 'fk: laps.driver_id -> drivers', 'error', count(*), array_join(slice(collect_set(l.driver_id), 1, 5), ', ')
FROM ${catalog}.silver.laps l LEFT ANTI JOIN ${catalog}.silver.drivers d USING (driver_id);

SELECT 'fk: laps.session_id -> sessions', 'error', count(*), array_join(slice(collect_set(l.session_id), 1, 5), ', ')
FROM ${catalog}.silver.laps l LEFT ANTI JOIN ${catalog}.silver.sessions s USING (session_id);

SELECT 'fk: pit_stops.session_id -> sessions', 'error', count(*), array_join(slice(collect_set(p.session_id), 1, 5), ', ')
FROM ${catalog}.silver.pit_stops p LEFT ANTI JOIN ${catalog}.silver.sessions s USING (session_id);

SELECT 'fk: events.circuit_id -> circuits', 'error', count(*), array_join(slice(collect_set(e.circuit_id), 1, 5), ', ')
FROM ${catalog}.silver.events e LEFT ANTI JOIN ${catalog}.silver.circuits c USING (circuit_id);

SELECT 'fk: session_weather.session_id -> sessions', 'error', count(*), array_join(slice(collect_set(w.session_id), 1, 5), ', ')
FROM ${catalog}.silver.session_weather w LEFT ANTI JOIN ${catalog}.silver.sessions s USING (session_id);

-- ------------------------------------------------------------------ expected row counts

SELECT 'rows: 17-26 classified entries per race', 'error', count(*),
       array_join(slice(collect_list(concat(session_id, '=', n)), 1, 5), ', ')
FROM (
  SELECT session_id, count(*) AS n FROM ${catalog}.silver.results WHERE session_code = 'R'
  GROUP BY session_id HAVING count(*) NOT BETWEEN 17 AND 26
);

SELECT 'rows: every completed event has race results', 'error', count(*),
       array_join(slice(collect_list(concat(e.season, '/', e.round)), 1, 5), ', ')
FROM ${catalog}.silver.events e
LEFT ANTI JOIN (SELECT DISTINCT season, round FROM ${catalog}.silver.results WHERE session_code = 'R') r
  ON r.season = e.season AND r.round = e.round
WHERE e.status = 'completed';

SELECT 'rows: every completed race has lap times', 'warn', count(*),
       array_join(slice(collect_list(concat(e.season, '/', e.round)), 1, 5), ', ')
FROM ${catalog}.silver.events e
LEFT ANTI JOIN (SELECT DISTINCT season, round FROM ${catalog}.silver.laps WHERE session_code = 'R') l
  ON l.season = e.season AND l.round = e.round
WHERE e.status = 'completed';

SELECT 'rows: 2018+ sessions without lap detail', 'warn', count(*),
       array_join(slice(collect_list(session_id), 1, 8), ', ')
FROM ${catalog}.silver.sessions s
LEFT ANTI JOIN (SELECT DISTINCT session_id FROM ${catalog}.silver.laps WHERE source <> 'jolpica') l USING (session_id)
WHERE s.season >= 2018 AND NOT s.is_cancelled AND (s.has_fastf1 OR s.openf1_session_key IS NOT NULL)
  AND s.start_utc < current_timestamp() - INTERVAL 6 HOURS;

SELECT 'rows: 2027 calendar has 24 rounds', 'warn', abs(count(*) - 24), CAST(count(*) AS STRING)
FROM ${catalog}.silver.events WHERE season = 2027;

-- ------------------------------------------------------------------ physical ranges (flag, don't delete)

SELECT 'range: lap times flagged as outliers', 'warn', count(*),
       array_join(slice(collect_list(concat(session_id, ':', driver_id, ':', lap, '=', lap_time_flag)), 1, 5), ', ')
FROM ${catalog}.silver.laps WHERE lap_time_flag IS NOT NULL;

SELECT 'range: session weather values', 'error', count(*),
       array_join(slice(collect_list(concat(session_id, ' air=', air_temp_c, ' track=', track_temp_c)), 1, 5), ', ')
FROM ${catalog}.silver.session_weather
WHERE air_temp_c NOT BETWEEN -10 AND 50 OR track_temp_c NOT BETWEEN -10 AND 75
   OR humidity_pct NOT BETWEEN 0 AND 100 OR wind_speed_ms NOT BETWEEN 0 AND 40;

SELECT 'range: hourly weather values', 'error', count(*),
       array_join(slice(collect_list(concat(circuit_id, '@', time_utc)), 1, 5), ', ')
FROM ${catalog}.silver.weather_hourly
WHERE temp_c NOT BETWEEN -40 AND 60 OR precipitation_mm < 0 OR humidity_pct NOT BETWEEN 0 AND 100
   OR cloud_cover_pct NOT BETWEEN 0 AND 100;

SELECT 'range: forecast weather values', 'error', count(*),
       array_join(slice(collect_list(concat(season, '/', round, ' ', source, ' ', time_utc)), 1, 5), ', ')
FROM ${catalog}.silver.weather_forecasts
WHERE temp_c NOT BETWEEN -40 AND 60 OR precipitation_mm < 0 OR lead_days NOT BETWEEN 0 AND 16
   OR humidity_pct NOT BETWEEN 0 AND 100 OR cloud_cover_pct NOT BETWEEN 0 AND 100;

SELECT 'range: pit stop total time 10s-120s', 'warn', count(*),
       array_join(slice(collect_list(concat(session_id, ':', driver_id, '=', pit_duration_ms)), 1, 5), ', ')
FROM ${catalog}.silver.pit_stops
WHERE pit_duration_ms NOT BETWEEN 10000 AND 120000;

-- ------------------------------------------------------------------ freshness

SELECT 'freshness: latest completed race is loaded within 3 days', 'warn',
       CASE WHEN max_race < date_sub(current_date(), 3) AND max_done < max_race THEN 1 ELSE 0 END,
       concat('last race ', max_race, ', last loaded ', max_done)
FROM (
  SELECT
    max(CASE WHEN race_date < current_date() AND status <> 'cancelled' THEN race_date END) AS max_race,
    max(CASE WHEN status = 'completed' THEN race_date END) AS max_done
  FROM ${catalog}.silver.events
);
