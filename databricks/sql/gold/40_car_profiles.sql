-- Car (constructor) features: pace per event and trend, straight-line speed, strengths by
-- track type, tyre wear, reliability and pit stops. Values are running (season-to-date) as
-- of each round, so a model at round N only sees rounds <= N.

CREATE OR REPLACE TABLE ${catalog}.gold.team_event_pace
COMMENT 'Per constructor per event: qualifying "supertime" gap (team best lap of the weekend vs pole) and race-pace gap (team median clean lap vs best team), %, plus 5-race rolling average and trend slope (% per race; negative = catching up)'
AS
WITH q AS (
  SELECT season, round, constructor_id,
         min(least(q1_ms, q2_ms, q3_ms)) AS team_best_ms
  FROM ${catalog}.silver.qualifying
  WHERE session_code = 'Q'
  GROUP BY ALL
),
q_wet AS (
  SELECT s.season, s.round, max(CAST(w.rainfall AS INT)) = 1 AS wet
  FROM ${catalog}.silver.session_weather w
  JOIN ${catalog}.silver.sessions s USING (session_id)
  WHERE s.session_code = 'Q'
  GROUP BY ALL
),
qg AS (
  SELECT q.season, q.round, q.constructor_id,
         100 * ln(CAST(q.team_best_ms AS DOUBLE) / min(q.team_best_ms) OVER (PARTITION BY q.season, q.round)) AS quali_gap_pct
  FROM q
  LEFT JOIN q_wet w USING (season, round)
  WHERE q.team_best_ms IS NOT NULL AND NOT coalesce(w.wet, false)
),
r AS (
  SELECT l.season, l.round, l.constructor_id,
         percentile(l.lap_time_ms, 0.5) AS team_median_ms, count(*) AS clean_laps
  FROM ${catalog}.gold.race_laps l
  JOIN ${catalog}.gold.race_conditions c USING (session_id)
  WHERE l.is_clean AND NOT coalesce(c.is_wet, false) AND l.constructor_id IS NOT NULL
  GROUP BY ALL
  HAVING count(*) >= 10
),
rg AS (
  SELECT season, round, constructor_id, clean_laps,
         100 * ln(team_median_ms / min(team_median_ms) OVER (PARTITION BY season, round)) AS race_gap_pct
  FROM r
),
joined AS (
  SELECT
    coalesce(qg.season, rg.season) AS season,
    coalesce(qg.round, rg.round) AS round,
    coalesce(qg.constructor_id, rg.constructor_id) AS constructor_id,
    qg.quali_gap_pct, rg.race_gap_pct, rg.clean_laps,
    (coalesce(qg.quali_gap_pct, 0) + coalesce(rg.race_gap_pct, 0))
      / (CAST(qg.quali_gap_pct IS NOT NULL AS INT) + CAST(rg.race_gap_pct IS NOT NULL AS INT)) AS gap_to_fastest_pct
  FROM qg
  FULL OUTER JOIN rg USING (season, round, constructor_id)
)
SELECT
  j.*, e.circuit_id, c.speed_profile,
  avg(gap_to_fastest_pct) OVER w5 AS rolling_gap_5,
  regr_slope(gap_to_fastest_pct, j.round) OVER w5 AS trend_slope_5
FROM joined j
JOIN ${catalog}.silver.events e USING (season, round)
LEFT JOIN ${catalog}.silver.circuits c ON c.circuit_id = e.circuit_id
WINDOW w5 AS (PARTITION BY j.constructor_id, j.season ORDER BY j.round ROWS BETWEEN 4 PRECEDING AND CURRENT ROW);

CREATE OR REPLACE TABLE ${catalog}.gold.car_profiles
COMMENT 'Per constructor, season and round (season-to-date as of that round): pace, strengths by track type, straight-line speed, tyre wear, reliability and pit stops. Index conventions: pace/tyre in %, negative = better; speed in kph above field median'
AS
WITH pace AS (
  SELECT
    constructor_id, season, round, circuit_id, speed_profile, gap_to_fastest_pct,
    avg(gap_to_fastest_pct) OVER s2d AS pace_gap_pct,
    count(gap_to_fastest_pct) OVER s2d AS events
  FROM ${catalog}.gold.team_event_pace
  WINDOW s2d AS (PARTITION BY constructor_id, season ORDER BY round ROWS UNBOUNDED PRECEDING)
),
track_type AS (
  -- Performance at a track type relative to the team's own season-to-date average.
  SELECT p.constructor_id, p.season, p.round,
    avg(CASE WHEN h.speed_profile = 'high' THEN h.gap_to_fastest_pct END) - max(p.pace_gap_pct) AS high_speed_track_idx,
    avg(CASE WHEN h.speed_profile = 'low' THEN h.gap_to_fastest_pct END) - max(p.pace_gap_pct) AS low_speed_track_idx,
    avg(CASE WHEN h.speed_profile IN ('medium', 'mixed') THEN h.gap_to_fastest_pct END) - max(p.pace_gap_pct) AS medium_speed_track_idx
  FROM pace p
  JOIN ${catalog}.gold.team_event_pace h
    ON h.constructor_id = p.constructor_id AND h.season = p.season AND h.round <= p.round
  GROUP BY p.constructor_id, p.season, p.round
),
speed AS (
  -- Straight-line speed: each team's 90th-percentile speed-trap reading per event,
  -- relative to the median team at that event (2018+).
  SELECT season, round, constructor_id,
         team_p90 - percentile(team_p90, 0.5) OVER (PARTITION BY season, round) AS trap_kph_vs_field
  FROM (
    SELECT l.season, l.round, e.constructor_id, percentile(l.speed_trap_kph, 0.9) AS team_p90
    FROM ${catalog}.silver.laps l
    JOIN ${catalog}.silver.entries e
      ON e.season = l.season AND e.round = l.round AND e.driver_id = l.driver_id
    WHERE l.session_code IN ('Q', 'R') AND l.speed_trap_kph BETWEEN 200 AND 400
    GROUP BY ALL
  )
),
tyre AS (
  -- Team tyre wear: fuel-corrected loss per 10 laps of tyre age, vs the field (2018+).
  SELECT season, round, constructor_id,
         wear - percentile(wear, 0.5) OVER (PARTITION BY season, round) AS tyre_wear_vs_field
  FROM (
    SELECT season, round, constructor_id,
      sum(wear_pct_per_10 * laps) / sum(laps) AS wear
    FROM (
      SELECT season, round, constructor_id, driver_id, stint, count(*) AS laps,
        regr_slope(100 * ln(CAST(lap_time_ms AS DOUBLE)) + 0.035 * lap, tyre_life) * 10 AS wear_pct_per_10
      FROM ${catalog}.gold.race_laps
      WHERE is_clean AND season >= 2018 AND tyre_life IS NOT NULL
        AND compound IN ('HYPERSOFT', 'ULTRASOFT', 'SUPERSOFT', 'SOFT', 'MEDIUM', 'HARD')
      GROUP BY ALL HAVING count(*) >= 8
    )
    GROUP BY ALL
  )
),
starts AS (
  SELECT r.season, r.round, r.constructor_id,
    count(*) AS starts,
    sum(CAST(c.cause_class IN ('mechanical', 'mechanical_or_other') AS INT)) AS mech_dnfs
  FROM ${catalog}.silver.results r
  LEFT JOIN ${catalog}.gold.retirement_causes c USING (session_id, driver_id)
  WHERE r.session_code = 'R' AND r.status_class NOT IN ('did_not_start', 'did_not_qualify')
  GROUP BY ALL
),
season_rate AS (
  SELECT season, sum(mech_dnfs) / sum(starts) AS field_rate FROM starts GROUP BY season
),
pits AS (
  SELECT p.season, p.round, e.constructor_id,
    avg(p.stationary_ms) / 1000 AS stationary_s,
    percentile(p.pit_duration_ms, 0.5) AS team_pit_ms
  FROM ${catalog}.silver.pit_stops p
  JOIN ${catalog}.silver.entries e
    ON e.season = p.season AND e.round = p.round AND e.driver_id = p.driver_id
  WHERE p.session_code = 'R'
    AND coalesce(p.pit_duration_ms, 30000) BETWEEN 10000 AND 60000
    AND coalesce(p.stationary_ms, 3000) BETWEEN 1500 AND 15000
  GROUP BY ALL
),
pits_rel AS (
  SELECT season, round, constructor_id, stationary_s,
         (team_pit_ms - percentile(team_pit_ms, 0.5) OVER (PARTITION BY season, round)) / 1000 AS pit_time_vs_field_s
  FROM pits
),
per_round AS (
  SELECT
    p.constructor_id, p.season, p.round, p.events, p.pace_gap_pct,
    t.high_speed_track_idx, t.medium_speed_track_idx, t.low_speed_track_idx,
    s.trap_kph_vs_field, ty.tyre_wear_vs_field,
    st.starts, st.mech_dnfs, sr.field_rate,
    pr.stationary_s, pr.pit_time_vs_field_s
  FROM pace p
  LEFT JOIN track_type t USING (constructor_id, season, round)
  LEFT JOIN speed s USING (constructor_id, season, round)
  LEFT JOIN tyre ty USING (constructor_id, season, round)
  LEFT JOIN starts st USING (constructor_id, season, round)
  LEFT JOIN season_rate sr USING (season)
  LEFT JOIN pits_rel pr USING (constructor_id, season, round)
)
SELECT
  constructor_id, season, round AS as_of_round, events,
  pace_gap_pct,
  high_speed_track_idx, medium_speed_track_idx, low_speed_track_idx,
  avg(trap_kph_vs_field) OVER s2d AS straight_speed_kph,
  avg(tyre_wear_vs_field) OVER s2d AS tyre_wear_idx,
  -- Mechanical failures per car per race, shrunk toward the season's field rate
  -- with 20 pseudo-starts so a lucky or unlucky early season isn't over-read.
  (sum(mech_dnfs) OVER s2d + 20 * field_rate) / (sum(starts) OVER s2d + 20) AS reliability_dnf_rate,
  sum(starts) OVER s2d AS starts,
  avg(stationary_s) OVER s2d AS pit_stationary_s,
  avg(pit_time_vs_field_s) OVER s2d AS pit_time_vs_field_s
FROM per_round
WINDOW s2d AS (PARTITION BY constructor_id, season ORDER BY round ROWS UNBOUNDED PRECEDING);
