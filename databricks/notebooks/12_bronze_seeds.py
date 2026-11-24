# Databricks notebook source
# MAGIC %md
# MAGIC # 12 · Bronze: manual seeds
# MAGIC Loads hand-curated CSVs from the repo's `configs/` folder (uploaded with the bundle):
# MAGIC
# MAGIC | File | Table |
# MAGIC |---|---|
# MAGIC | `calendar_2027.csv` | `bronze.seed_calendar_2027` |
# MAGIC | `circuits_seed.csv` | `bronze.seed_circuits` |
# MAGIC
# MAGIC The table is replaced only when the file content changes (tracked by SHA-256), so the
# MAGIC load is idempotent and every version is traceable to a git commit.

# COMMAND ----------

import csv
import hashlib
import os
import re

from pyspark.sql import functions as F

dbutils.widgets.text("catalog", "f1_forecast")
dbutils.widgets.text("configs_dir", "")
catalog = dbutils.widgets.get("catalog").strip()
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
    raise ValueError(f"Invalid catalog name: {catalog!r}")

configs_dir = dbutils.widgets.get("configs_dir").strip() or os.path.abspath("../../configs")
print("configs:", configs_dir)

SEEDS = {"calendar_2027.csv": "seed_calendar_2027", "circuits_seed.csv": "seed_circuits"}

# COMMAND ----------

for filename, table_name in SEEDS.items():
    path = os.path.join(configs_dir, filename)
    with open(path, "rb") as f:
        raw = f.read()
    sha = hashlib.sha256(raw).hexdigest()
    table = f"`{catalog}`.`bronze`.`{table_name}`"

    if spark.catalog.tableExists(table.replace("`", "")):
        current = spark.table(table).select(F.max("_content_sha256")).first()[0]
        if current == sha:
            print(f"= {table}: unchanged ({sha[:12]})")
            continue

    rows = list(csv.DictReader(raw.decode("utf-8").splitlines()))
    columns = list(rows[0])
    df = (
        spark.createDataFrame(
            [[r[c] or None for c in columns] for r in rows],
            ", ".join(f"`{c}` string" for c in columns),
        )
        .withColumn("_source", F.lit("manual_seed"))
        .withColumn("_source_file", F.lit(f"configs/{filename}"))
        .withColumn("_content_sha256", F.lit(sha))
        .withColumn("_ingested_at", F.current_timestamp())
    )
    df.write.mode("overwrite").option("overwriteSchema", True).saveAsTable(table)
    print(f"+ {table}: {df.count()} rows ({sha[:12]})")
