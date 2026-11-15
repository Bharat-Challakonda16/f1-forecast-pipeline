# Databricks notebook source
# MAGIC %md
# MAGIC # 00 · Setup catalog and medallion schemas
# MAGIC Creates `<catalog>.bronze`, `<catalog>.silver` and `<catalog>.gold`. Safe to re-run.

# COMMAND ----------

import re

dbutils.widgets.text("catalog", "f1_forecast")
catalog = dbutils.widgets.get("catalog").strip()

if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
    raise ValueError(f"Invalid catalog name: {catalog!r}")

# COMMAND ----------

existing = {row[0] for row in spark.sql("SHOW CATALOGS").collect()}
if catalog not in existing:
    try:
        spark.sql(f"CREATE CATALOG IF NOT EXISTS `{catalog}`")
    except Exception as exc:
        raise RuntimeError(
            f"Could not create catalog '{catalog}'. If this workspace does not allow "
            "creating catalogs, re-run with: bundle run hello_delta --var catalog=workspace"
        ) from exc

for schema in ("bronze", "silver", "gold"):
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{catalog}`.`{schema}`")

print(f"Ready: {catalog}.bronze / {catalog}.silver / {catalog}.gold")
