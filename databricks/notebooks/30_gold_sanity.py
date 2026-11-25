# Databricks notebook source
# MAGIC %md
# MAGIC # 30 · Gold sanity review
# MAGIC Human-readable views of the Phase 2 features, to eyeball against what is known about
# MAGIC F1. The automated version of these checks lives in `databricks/sql/quality/gold_checks.sql`
# MAGIC and runs after every build; this notebook is for looking at the numbers.

# COMMAND ----------

import re

dbutils.widgets.text("catalog", "f1_forecast")
catalog = dbutils.widgets.get("catalog").strip()
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
    raise ValueError(f"Invalid catalog name: {catalog!r}")
spark.sql(f"USE CATALOG `{catalog}`")

LATEST = """
  (SELECT * FROM gold.driver_traits t
   WHERE data_cutoff = (SELECT max(data_cutoff) FROM gold.driver_traits x WHERE x.trait = t.trait))
"""

# COMMAND ----------

# MAGIC %md ## Drivers - latest season, all traits (negative = better, except error rate: lower = better)

# COMMAND ----------

display(spark.sql(f"""
    WITH t AS (SELECT * FROM {LATEST} WHERE as_of_season = (SELECT max(as_of_season) FROM gold.driver_traits))
    SELECT driver_id,
      round(max(CASE WHEN trait = 'quali_pace' THEN value END), 2) AS quali_pace_pct,
      round(max(CASE WHEN trait = 'race_pace' THEN value END), 2) AS race_pace_pct,
      round(max(CASE WHEN trait = 'race_pace' THEN std_err END), 2) AS race_pace_se,
      round(max(CASE WHEN trait = 'consistency' THEN value END), 1) AS consistency,
      round(max(CASE WHEN trait = 'tyre_wear' THEN value END), 3) AS tyre_wear,
      round(max(CASE WHEN trait = 'starts' THEN value END), 2) AS places_lost_lap1,
      round(max(CASE WHEN trait = 'error_rate' THEN value END), 3) AS errors_per_race,
      round(max(CASE WHEN trait = 'wet_skill' THEN value END), 2) AS wet_extra_pct
    FROM t GROUP BY driver_id ORDER BY race_pace_pct
    """))

# COMMAND ----------

# MAGIC %md ## Known cases
# MAGIC Expectations: Verstappen / Alonso / Hamilton strong in the wet; Bottas and Norris weak
# MAGIC starters; Alonso among the safest; Pérez stronger in races than in qualifying.

# COMMAND ----------

display(spark.sql(f"""
    SELECT trait, driver_id, round(avg(value), 3) AS career_avg, round(avg(std_err), 3) AS avg_se,
           sum(n_samples) AS observations
    FROM {LATEST}
    WHERE driver_id IN ('alonso', 'hamilton', 'max_verstappen', 'bottas', 'norris', 'perez')
    GROUP BY trait, driver_id ORDER BY trait, career_avg
    """))

# COMMAND ----------

# MAGIC %md ## Cars - latest round of the latest season

# COMMAND ----------

display(spark.sql("""
    WITH last AS (SELECT max(season) AS s FROM gold.car_profiles),
    r AS (SELECT max(as_of_round) AS r FROM gold.car_profiles, last WHERE season = last.s)
    SELECT p.constructor_id, round(p.pace_gap_pct, 2) AS pace_gap_pct,
      round(c.low_speed_corner_kph, 1) AS low_corner_kph, round(c.medium_speed_corner_kph, 1) AS med_corner_kph,
      round(c.high_speed_corner_kph, 1) AS high_corner_kph, round(p.straight_speed_kph, 1) AS trap_kph,
      round(p.tyre_wear_idx, 3) AS tyre_wear, round(p.reliability_dnf_rate, 3) AS mech_dnf_rate,
      round(p.pit_stationary_s, 2) AS pit_stationary_s
    FROM gold.car_profiles p, last, r
    LEFT JOIN gold.car_corner_profiles c USING (constructor_id, season, as_of_round)
    WHERE p.season = last.s AND p.as_of_round = r.r
    ORDER BY pace_gap_pct
    """))

# COMMAND ----------

# MAGIC %md ## Circuits - events, overtaking, pit loss, tyre wear

# COMMAND ----------

display(spark.sql("""
    SELECT r.circuit_id, r.races, round(r.sc_prob, 2) AS sc, round(r.vsc_prob, 2) AS vsc,
      round(r.red_flag_prob, 2) AS red, round(r.lap1_incident_prob, 2) AS lap1,
      m.overtaking_difficulty, round(m.passes_per_green_lap, 2) AS passes_per_lap,
      round(m.pit_loss_s, 1) AS pit_loss_s, round(t.circuit_wear_level, 2) AS tyre_wear_level
    FROM gold.track_event_rates r
    LEFT JOIN gold.circuit_measures m USING (circuit_id)
    LEFT JOIN (SELECT DISTINCT circuit_id, circuit_wear_level FROM gold.tyre_models) t USING (circuit_id)
    WHERE r.last_season >= 2025
    ORDER BY m.overtaking_difficulty DESC, passes_per_lap
    """))

# COMMAND ----------

# MAGIC %md ## Latest data-quality run (silver + gold)

# COMMAND ----------

display(spark.sql("""
    SELECT check_name, severity, violations, sample
    FROM silver.dq_results
    WHERE run_at >= (SELECT max(run_at) FROM silver.dq_results) - INTERVAL 2 HOURS
    ORDER BY violations > 0 DESC, severity, check_name
    """))
