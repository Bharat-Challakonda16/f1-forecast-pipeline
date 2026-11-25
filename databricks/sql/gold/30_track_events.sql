-- Track-level event rates and measured circuit characteristics (Phase 2 step 4).

CREATE OR REPLACE TABLE ${catalog}.gold.race_neutralisations
COMMENT 'Per race: safety cars, VSCs, red flags and lap-1 incidents. Official from FastF1 (2018-22) and OpenF1 race control (2023+); inferred for all races from runs of field-wide slow laps (a lap >2.5x normal = red flag). source says which is used.'
AS
WITH races AS (
  SELECT r.session_id, r.season, r.round, e.circuit_id
  FROM ${catalog}.gold.race_conditions r
  JOIN ${catalog}.silver.events e USING (season, round)
  WHERE e.status = 'completed'
),
-- Official: FastF1 track status changes
ff AS (
  SELECT
    ${catalog}.silver.session_id(int(_year), int(_round), 'R') AS session_id,
    sum(CAST(Message = 'SCDeployed' AS INT)) AS sc,
    sum(CAST(Message = 'VSCDeployed' AS INT)) AS vsc,
    sum(CAST(Message = 'Red' AS INT)) AS red
  FROM ${catalog}.bronze.fastf1_track_status
  WHERE _session = 'R'
  GROUP BY 1
),
-- Official: OpenF1 race control (deduplicated per lap). Every race with any race-control
-- message counts as official, including races with no neutralisation at all.
of AS (
  SELECT
    session_id,
    count(DISTINCT CASE WHEN kind = 'sc' THEN lap END) AS sc,
    count(DISTINCT CASE WHEN kind = 'vsc' THEN lap END) AS vsc,
    count(DISTINCT CASE WHEN kind = 'red' THEN lap END) AS red
  FROM (
    SELECT s.session_id, int(rc.lap_number) AS lap,
      CASE
        WHEN rc.message = 'SAFETY CAR DEPLOYED' THEN 'sc'
        WHEN rc.message IN ('VIRTUAL SAFETY CAR DEPLOYED', 'VSC DEPLOYED') THEN 'vsc'
        WHEN rc.flag = 'RED' THEN 'red'
      END AS kind
    FROM ${catalog}.bronze.openf1_race_control rc
    JOIN ${catalog}.silver.sessions s ON s.openf1_session_key = int(rc.session_key)
    WHERE s.session_code = 'R'
  )
  GROUP BY session_id
),
-- First official neutralised lap (for lap-1 incidents)
first_official AS (
  SELECT session_id, min(lap) AS first_lap
  FROM ${catalog}.silver.laps
  WHERE session_code = 'R' AND track_status IN ('sc', 'vsc', 'red')
  GROUP BY session_id
  UNION ALL
  SELECT s.session_id, min(int(rc.lap_number))
  FROM ${catalog}.bronze.openf1_race_control rc
  JOIN ${catalog}.silver.sessions s ON s.openf1_session_key = int(rc.session_key)
  WHERE s.session_code = 'R'
    AND (rc.message IN ('SAFETY CAR DEPLOYED', 'VIRTUAL SAFETY CAR DEPLOYED', 'VSC DEPLOYED') OR rc.flag = 'RED')
  GROUP BY s.session_id
),
-- Inferred for every era: consecutive runs of laps where the whole field is >10% slow
lap_slow AS (
  SELECT DISTINCT session_id, lap, field_median_ms, race_median_ms,
         field_median_ms > 1.10 * race_median_ms AS slow
  FROM ${catalog}.gold.race_laps
  WHERE lap > 1
),
runs AS (
  SELECT session_id, lap, field_median_ms, race_median_ms,
         lap - row_number() OVER (PARTITION BY session_id ORDER BY lap) AS run_id
  FROM lap_slow WHERE slow
),
inferred AS (
  SELECT
    session_id,
    count(*) AS periods,
    sum(CAST(is_red AS INT)) AS red,
    sum(CAST(NOT is_red AS INT)) AS sc_or_vsc,
    min(first_lap) AS first_lap
  FROM (
    SELECT session_id, run_id, min(lap) AS first_lap,
           max(field_median_ms) > 2.5 * max(race_median_ms) AS is_red
    FROM runs GROUP BY session_id, run_id
  )
  GROUP BY session_id
),
lap1_crash AS (
  SELECT DISTINCT session_id
  FROM ${catalog}.gold.retirement_causes
  WHERE laps_completed <= 1 AND cause_class = 'accident'
)
SELECT
  r.session_id, r.season, r.round, r.circuit_id,
  CASE WHEN ff.session_id IS NOT NULL THEN 'fastf1'
       WHEN of.session_id IS NOT NULL THEN 'openf1'
       ELSE 'inferred' END AS source,
  coalesce(ff.sc, of.sc) AS official_sc,
  coalesce(ff.vsc, of.vsc) AS official_vsc,
  coalesce(ff.red, of.red) AS official_red,
  coalesce(i.periods, 0) AS inferred_periods,
  coalesce(i.red, 0) AS inferred_red,
  fo.first_lap AS official_first_lap,
  i.first_lap AS inferred_first_lap,
  l1.session_id IS NOT NULL AS had_lap1_crash,
  -- Final per-race flags: official where available, otherwise inferred
  CASE WHEN ff.session_id IS NOT NULL OR of.session_id IS NOT NULL
       THEN coalesce(ff.sc, of.sc) > 0
       ELSE coalesce(i.sc_or_vsc, 0) > 0 END AS had_sc,
  CASE WHEN ff.session_id IS NOT NULL OR of.session_id IS NOT NULL
       THEN coalesce(ff.vsc, of.vsc) > 0 END AS had_vsc,  -- unknown before 2018
  CASE WHEN ff.session_id IS NOT NULL OR of.session_id IS NOT NULL
       THEN coalesce(ff.red, of.red) > 0
       ELSE coalesce(i.red, 0) > 0 END AS had_red,
  -- coalesce: a race with no neutralisation must read false, never unknown
  (l1.session_id IS NOT NULL
    OR coalesce(coalesce(fo.first_lap, i.first_lap) <= 2, false)) AS had_lap1_incident
FROM races r
LEFT JOIN ff USING (session_id)
LEFT JOIN of USING (session_id)
LEFT JOIN inferred i USING (session_id)
LEFT JOIN (SELECT session_id, min(first_lap) AS first_lap FROM first_official GROUP BY session_id) fo
  USING (session_id)
LEFT JOIN lap1_crash l1 USING (session_id);

CREATE OR REPLACE TABLE ${catalog}.gold.track_event_rates
COMMENT 'Per circuit: probability per race of a safety car, VSC, red flag and lap-1 incident. Recency-weighted (x0.85 per year back) and shrunk toward the all-circuit rate with 6 pseudo-races, so rarely-raced circuits get sensible, uncertain values.'
AS
WITH w AS (
  SELECT n.*, power(0.85, (SELECT max(season) FROM ${catalog}.gold.race_neutralisations) - season) AS wt
  FROM ${catalog}.gold.race_neutralisations n
),
global AS (
  SELECT
    sum(wt * CAST(had_sc AS INT)) / sum(wt) AS p_sc,
    sum(CASE WHEN had_vsc IS NOT NULL THEN wt * CAST(had_vsc AS INT) END)
      / sum(CASE WHEN had_vsc IS NOT NULL THEN wt END) AS p_vsc,
    sum(wt * CAST(had_red AS INT)) / sum(wt) AS p_red,
    sum(wt * CAST(had_lap1_incident AS INT)) / sum(wt) AS p_lap1
  FROM w
),
per AS (
  SELECT
    circuit_id,
    count(*) AS races,
    max(season) AS last_season,
    sum(wt) AS w_races,
    sum(wt * CAST(had_sc AS INT)) AS w_sc,
    sum(CASE WHEN had_vsc IS NOT NULL THEN wt END) AS w_races_vsc,
    sum(CASE WHEN had_vsc IS NOT NULL THEN wt * CAST(had_vsc AS INT) END) AS w_vsc,
    sum(wt * CAST(had_red AS INT)) AS w_red,
    sum(wt * CAST(had_lap1_incident AS INT)) AS w_lap1
  FROM w GROUP BY circuit_id
)
SELECT
  p.circuit_id, p.races, p.last_season, round(p.w_races, 2) AS effective_races,
  (p.w_sc + 6 * g.p_sc) / (p.w_races + 6) AS sc_prob,
  (coalesce(p.w_vsc, 0) + 6 * g.p_vsc) / (coalesce(p.w_races_vsc, 0) + 6) AS vsc_prob,
  (p.w_red + 6 * g.p_red) / (p.w_races + 6) AS red_flag_prob,
  (p.w_lap1 + 6 * g.p_lap1) / (p.w_races + 6) AS lap1_incident_prob,
  -- Beta-posterior standard deviation for the safety-car probability
  sqrt(((p.w_sc + 6 * g.p_sc) * (p.w_races - p.w_sc + 6 * (1 - g.p_sc)))
       / (power(p.w_races + 6, 2) * (p.w_races + 7))) AS sc_prob_sd
FROM per p CROSS JOIN global g;

CREATE OR REPLACE TABLE ${catalog}.gold.green_pit_stops
COMMENT 'Every green-flag pit stop in a dry race (2018+) with its time loss: in-lap + out-lap minus two of the driver''s typical laps. Per-stop so circuit values can be built as of any date.'
AS
SELECT s.*, e.round, e.race_date
FROM (-- Time lost by a stop = in-lap + out-lap minus two of the driver's typical laps.
  SELECT
    i.session_id, i.season, e.circuit_id,
    (i.lap_time_ms + o.lap_time_ms - 2 * i.driver_median_ms) / 1000.0 AS loss_s
  FROM ${catalog}.gold.race_laps i
  JOIN ${catalog}.gold.race_laps o
    ON o.session_id = i.session_id AND o.driver_id = i.driver_id AND o.lap = i.lap + 1
  JOIN ${catalog}.silver.events e ON e.season = i.season AND e.round = i.round
  JOIN ${catalog}.gold.race_conditions c ON c.session_id = i.session_id
  WHERE i.is_in_lap AND o.is_out_lap AND NOT i.is_neutralised AND NOT o.is_neutralised
    AND i.season >= 2018 AND NOT coalesce(c.is_wet, false)) s
JOIN ${catalog}.silver.events e ON e.season = s.season AND e.circuit_id = s.circuit_id
  AND e.round = CAST(split(s.session_id, '_')[1] AS INT);

CREATE OR REPLACE TABLE ${catalog}.gold.race_overtakes
COMMENT 'Per dry race (2018+): on-track passes and green laps. Passes = places gained by cars not pitting, minus places handed over by cars that pitted or retired. Per-race so circuit values can be built as of any date.'
AS
WITH lap_moves AS (
  -- Position change of every car between consecutive laps (lap 2 onwards, green laps).
  SELECT
    cur.session_id, cur.season, cur.round, cur.lap,
    prev.position - cur.position AS gained,
    (cur.is_in_lap OR cur.is_out_lap OR prev.is_in_lap) AS pitting
  FROM ${catalog}.gold.race_laps cur
  JOIN ${catalog}.gold.race_laps prev
    ON prev.session_id = cur.session_id AND prev.driver_id = cur.driver_id AND prev.lap = cur.lap - 1
  WHERE cur.season >= 2018 AND cur.lap > 1 AND NOT cur.is_neutralised AND NOT prev.is_neutralised
),
retirements AS (
  -- A car that disappears hands one place to everyone behind it.
  SELECT prev.session_id, prev.lap + 1 AS lap,
         sum(cnt.n_cars - prev.position) AS places_freed
  FROM ${catalog}.gold.race_laps prev
  LEFT ANTI JOIN ${catalog}.gold.race_laps nxt
    ON nxt.session_id = prev.session_id AND nxt.driver_id = prev.driver_id AND nxt.lap = prev.lap + 1
  JOIN (SELECT session_id, lap, count(*) AS n_cars FROM ${catalog}.gold.race_laps GROUP BY 1, 2) cnt
    ON cnt.session_id = prev.session_id AND cnt.lap = prev.lap
  WHERE prev.season >= 2018
  GROUP BY 1, 2
),
per_lap AS (
  -- On-track passes = places gained by cars not pitting, minus places handed over by cars
  -- that pitted (they drop back) or retired.
  SELECT
    m.session_id, m.season, m.round, m.lap,
    greatest(
      sum(CASE WHEN NOT m.pitting THEN greatest(m.gained, 0) ELSE 0 END)
      - sum(CASE WHEN m.pitting THEN greatest(-m.gained, 0) ELSE 0 END)
      - coalesce(max(r.places_freed), 0),
      0
    ) AS passes
  FROM lap_moves m
  LEFT JOIN retirements r ON r.session_id = m.session_id AND r.lap = m.lap
  GROUP BY m.session_id, m.season, m.round, m.lap
)
SELECT x.*, e.round, e.race_date
FROM (SELECT p.session_id, e.circuit_id, p.season,
         sum(p.passes) AS passes, count(*) AS green_laps
  FROM per_lap p
  JOIN ${catalog}.silver.events e ON e.season = p.season AND e.round = p.round
  JOIN ${catalog}.gold.race_conditions c ON c.session_id = p.session_id
  WHERE NOT coalesce(c.is_wet, false)
  GROUP BY p.session_id, e.circuit_id, p.season) x
JOIN ${catalog}.silver.events e ON e.season = x.season AND e.circuit_id = x.circuit_id
  AND e.round = CAST(split(x.session_id, '_')[1] AS INT);

CREATE OR REPLACE TABLE ${catalog}.gold.circuit_measures
COMMENT 'Per circuit, all races to date: green-flag pit-stop loss (median), on-track passes per green lap, overtaking difficulty 1 (easy) - 5 (hardest). Built from gold.green_pit_stops / gold.race_overtakes; the simulator rebuilds the same values as of its cutoff.'
AS
WITH pit AS (
  SELECT circuit_id, count(*) AS green_stops, percentile(loss_s, 0.5) AS pit_loss_s
  FROM ${catalog}.gold.green_pit_stops
  WHERE loss_s BETWEEN 5 AND 60
  GROUP BY circuit_id
),
ovt AS (
  SELECT circuit_id, count(*) AS races, sum(passes) / sum(green_laps) AS passes_per_green_lap
  FROM ${catalog}.gold.race_overtakes GROUP BY circuit_id
)
SELECT
  coalesce(p.circuit_id, o.circuit_id) AS circuit_id,
  p.pit_loss_s, p.green_stops,
  o.passes_per_green_lap, o.races AS races_measured,
  -- quintiles across circuits: fewest passes = 5 (hardest)
  6 - ntile(5) OVER (ORDER BY o.passes_per_green_lap) AS overtaking_difficulty
FROM pit p
FULL OUTER JOIN ovt o USING (circuit_id);
