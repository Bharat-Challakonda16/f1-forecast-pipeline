-- Gold-layer checks. Same format as silver_checks.sql:
--   check_name, severity ('error' fails the build, 'warn' is reported), violations, sample
-- Section 1 is plumbing (keys, ranges); section 2 is "known F1 facts" the features must
-- reproduce - if a code change breaks one, the model is probably measuring something else.

-- ------------------------------------------------------------------ unique keys

SELECT 'gold unique: race_laps(session, driver, lap)' AS check_name, 'error' AS severity, count(*) AS violations,
       array_join(slice(collect_list(concat(session_id, ':', driver_id, ':', lap)), 1, 5), ', ') AS sample
FROM (SELECT session_id, driver_id, lap FROM ${catalog}.gold.race_laps GROUP BY ALL HAVING count(*) > 1);

SELECT 'gold unique: race_conditions(session)', 'error', count(*), array_join(slice(collect_list(session_id), 1, 5), ', ')
FROM (SELECT session_id FROM ${catalog}.gold.race_conditions GROUP BY ALL HAVING count(*) > 1);

SELECT 'gold unique: retirement_causes(session, driver)', 'error', count(*), array_join(slice(collect_list(session_id), 1, 5), ', ')
FROM (SELECT session_id, driver_id FROM ${catalog}.gold.retirement_causes GROUP BY ALL HAVING count(*) > 1);

SELECT 'gold unique: driver_traits(trait, version, cutoff, driver, season)', 'error', count(*),
       array_join(slice(collect_list(concat(trait, ':', driver_id, ':', as_of_season)), 1, 5), ', ')
FROM (SELECT trait, model_version, data_cutoff, driver_id, as_of_season FROM ${catalog}.gold.driver_traits GROUP BY ALL HAVING count(*) > 1);

SELECT 'gold unique: driver_session_pace(kind, season, round, driver)', 'error', count(*),
       array_join(slice(collect_list(concat(kind, ':', season, ':', round, ':', driver_id)), 1, 5), ', ')
FROM (SELECT kind, season, round, driver_id FROM ${catalog}.gold.driver_session_pace GROUP BY ALL HAVING count(*) > 1);

SELECT 'gold unique: driver_practice_pace(season, round, session, driver)', 'error', count(*),
       array_join(slice(collect_list(concat(season, ':', round, ':', session_code, ':', driver_id)), 1, 5), ', ')
FROM (SELECT season, round, session_code, driver_id FROM ${catalog}.gold.driver_practice_pace GROUP BY ALL HAVING count(*) > 1);

SELECT 'gold unique: car_profiles(team, season, round)', 'error', count(*), array_join(slice(collect_list(constructor_id), 1, 5), ', ')
FROM (SELECT constructor_id, season, as_of_round FROM ${catalog}.gold.car_profiles GROUP BY ALL HAVING count(*) > 1);

SELECT 'gold unique: tyre_models(circuit, compound)', 'error', count(*), array_join(slice(collect_list(circuit_id), 1, 5), ', ')
FROM (SELECT circuit_id, compound FROM ${catalog}.gold.tyre_models GROUP BY ALL HAVING count(*) > 1);

SELECT 'gold unique: track_event_rates(circuit)', 'error', count(*), array_join(slice(collect_list(circuit_id), 1, 5), ', ')
FROM (SELECT circuit_id FROM ${catalog}.gold.track_event_rates GROUP BY ALL HAVING count(*) > 1);

-- ------------------------------------------------------------------ ranges

SELECT 'gold range: event probabilities in [0, 1]', 'error', count(*), array_join(slice(collect_list(circuit_id), 1, 5), ', ')
FROM ${catalog}.gold.track_event_rates
WHERE NOT (sc_prob BETWEEN 0 AND 1 AND vsc_prob BETWEEN 0 AND 1 AND red_flag_prob BETWEEN 0 AND 1
           AND lap1_incident_prob BETWEEN 0 AND 1);

SELECT 'gold range: pit loss 10-40 s', 'warn', count(*), array_join(slice(collect_list(concat(circuit_id, '=', round(pit_loss_s, 1))), 1, 5), ', ')
FROM ${catalog}.gold.circuit_measures WHERE pit_loss_s IS NOT NULL AND pit_loss_s NOT BETWEEN 10 AND 40;

SELECT 'gold range: trait std_err positive and finite', 'error', count(*), array_join(slice(collect_list(concat(trait, ':', driver_id)), 1, 5), ', ')
FROM ${catalog}.gold.driver_traits WHERE NOT (std_err > 0 AND std_err < 1e6);

SELECT 'gold range: reliability rate in [0, 1]', 'error', count(*), array_join(slice(collect_list(constructor_id), 1, 5), ', ')
FROM ${catalog}.gold.car_profiles WHERE reliability_dnf_rate NOT BETWEEN 0 AND 1;

-- ------------------------------------------------------------------ known F1 facts

SELECT 'fact: Monaco is the hardest circuit to overtake (difficulty 5, fewest passes)', 'error',
       CAST(NOT (overtaking_difficulty = 5 AND passes_per_green_lap = min_passes) AS INT),
       concat('monaco difficulty=', overtaking_difficulty, ' passes/lap=', round(passes_per_green_lap, 2))
FROM ${catalog}.gold.circuit_measures
CROSS JOIN (SELECT min(passes_per_green_lap) AS min_passes FROM ${catalog}.gold.circuit_measures)
WHERE circuit_id = 'monaco';

SELECT 'fact: tyre wear Soft > Medium > Hard', 'error',
       CAST(NOT (max(CASE WHEN compound = 'SOFT' THEN mean_wear_pct_per_lap END)
                 > max(CASE WHEN compound = 'MEDIUM' THEN mean_wear_pct_per_lap END)
             AND max(CASE WHEN compound = 'MEDIUM' THEN mean_wear_pct_per_lap END)
                 > max(CASE WHEN compound = 'HARD' THEN mean_wear_pct_per_lap END)) AS INT),
       array_join(collect_list(concat(compound, '=', round(mean_wear_pct_per_lap, 3))), ', ')
FROM ${catalog}.gold.tyre_temp_sensitivity;

SELECT 'fact: fresh Soft faster than Medium faster than Hard', 'error',
       CAST(NOT (percentile(CASE WHEN compound = 'SOFT' THEN fresh_pace_vs_medium_pct END, 0.5) < 0
             AND percentile(CASE WHEN compound = 'HARD' THEN fresh_pace_vs_medium_pct END, 0.5) > 0) AS INT),
       concat('soft=', round(percentile(CASE WHEN compound = 'SOFT' THEN fresh_pace_vs_medium_pct END, 0.5), 2),
              ' hard=', round(percentile(CASE WHEN compound = 'HARD' THEN fresh_pace_vs_medium_pct END, 0.5), 2))
FROM ${catalog}.gold.tyre_models;

SELECT 'fact: hotter track -> more wear (Medium and Hard)', 'error',
       CAST(sum(CAST(compound IN ('MEDIUM', 'HARD') AND wear_per_10c <= 0 AS INT)) > 0 AS INT),
       array_join(collect_list(concat(compound, '=', round(wear_per_10c, 4))), ', ')
FROM ${catalog}.gold.tyre_temp_sensitivity;

SELECT 'fact: Bahrain is a high-wear track (top quarter, Medium)', 'warn',
       CAST(NOT (rnk <= 0.25) AS INT), concat('bahrain medium wear percentile rank=', round(rnk, 2))
FROM (
  SELECT circuit_id, percent_rank() OVER (ORDER BY wear_pct_per_lap_35c DESC) AS rnk
  FROM ${catalog}.gold.tyre_models WHERE compound = 'MEDIUM'
) WHERE circuit_id = 'bahrain';

SELECT 'fact: Marina Bay pit stop costs more than Monaco', 'error',
       CAST(NOT (max(CASE WHEN circuit_id = 'marina_bay' THEN pit_loss_s END)
                 > max(CASE WHEN circuit_id = 'monaco' THEN pit_loss_s END)) AS INT),
       concat('marina_bay=', round(max(CASE WHEN circuit_id = 'marina_bay' THEN pit_loss_s END), 1),
              ' monaco=', round(max(CASE WHEN circuit_id = 'monaco' THEN pit_loss_s END), 1))
FROM ${catalog}.gold.circuit_measures;

SELECT 'fact: a safety car happens in 40-75% of races (2018+)', 'error',
       CAST(NOT (avg(CAST(had_sc AS INT)) BETWEEN 0.40 AND 0.75) AS INT),
       concat('share=', round(avg(CAST(had_sc AS INT)), 2))
FROM ${catalog}.gold.race_neutralisations WHERE season >= 2018;

-- Latest fit per trait for the driver facts
SELECT 'fact: Alonso, Hamilton, Verstappen are better than average in the wet', 'warn',
       count_if(value >= 0), array_join(collect_list(concat(driver_id, '=', round(value, 2))), ', ')
FROM (
  SELECT driver_id, avg(value) AS value
  FROM ${catalog}.gold.driver_traits
  WHERE trait = 'wet_skill'
    AND data_cutoff = (SELECT max(data_cutoff) FROM ${catalog}.gold.driver_traits WHERE trait = 'wet_skill')
    AND driver_id IN ('alonso', 'hamilton', 'max_verstappen')
  GROUP BY driver_id
);

SELECT 'fact: Verstappen has a top-3 race pace in 2023', 'error',
       CAST(NOT (rnk <= 3) AS INT), concat('rank=', rnk)
FROM (
  SELECT driver_id, rank() OVER (ORDER BY value) AS rnk
  FROM ${catalog}.gold.driver_traits
  WHERE trait = 'race_pace' AND as_of_season = 2023 AND n_samples >= 10
    AND data_cutoff = (SELECT max(data_cutoff) FROM ${catalog}.gold.driver_traits WHERE trait = 'race_pace')
) WHERE driver_id = 'max_verstappen';

SELECT 'fact: Alonso makes fewer driver errors than the median driver', 'warn',
       CAST(NOT (alonso < med) AS INT), concat('alonso=', round(alonso, 3), ' median=', round(med, 3))
FROM (
  SELECT avg(CASE WHEN driver_id = 'alonso' THEN value END) AS alonso, percentile(value, 0.5) AS med
  FROM ${catalog}.gold.driver_traits
  WHERE trait = 'error_rate'
    AND data_cutoff = (SELECT max(data_cutoff) FROM ${catalog}.gold.driver_traits WHERE trait = 'error_rate')
);

SELECT 'fact: rookies are more uncertain than veterans (quali pace)', 'error',
       CAST(NOT (rookie_se > veteran_se) AS INT),
       concat('rookie se=', round(rookie_se, 3), ' veteran se=', round(veteran_se, 3))
FROM (
  SELECT avg(CASE WHEN first_season THEN std_err END) AS rookie_se,
         avg(CASE WHEN seasons >= 8 THEN std_err END) AS veteran_se
  FROM (
    SELECT std_err,
      as_of_season = min(as_of_season) OVER (PARTITION BY driver_id) AS first_season,
      count(*) OVER (PARTITION BY driver_id) AS seasons
    FROM ${catalog}.gold.driver_traits
    WHERE trait = 'quali_pace'
      AND data_cutoff = (SELECT max(data_cutoff) FROM ${catalog}.gold.driver_traits WHERE trait = 'quali_pace')
  )
);

SELECT 'fact: modelled wear Soft > Medium > Hard at every circuit', 'error', count(*),
       array_join(slice(collect_list(circuit_id), 1, 8), ', ')
FROM (
  SELECT circuit_id,
    max(CASE WHEN compound = 'SOFT' THEN wear_pct_per_lap_35c END) AS soft,
    max(CASE WHEN compound = 'MEDIUM' THEN wear_pct_per_lap_35c END) AS med,
    max(CASE WHEN compound = 'HARD' THEN wear_pct_per_lap_35c END) AS hard
  FROM ${catalog}.gold.tyre_models GROUP BY circuit_id
) WHERE med < hard OR soft < med;

SELECT 'info: circuits where the raw per-compound measurement had Medium < Hard', 'warn', count(*),
       array_join(slice(collect_list(circuit_id), 1, 8), ', ')
FROM (
  SELECT circuit_id,
    max(CASE WHEN compound = 'MEDIUM' THEN raw_wear_pct_per_lap_35c END) AS med,
    max(CASE WHEN compound = 'HARD' THEN raw_wear_pct_per_lap_35c END) AS hard
  FROM ${catalog}.gold.tyre_models GROUP BY circuit_id
) WHERE med < hard;
