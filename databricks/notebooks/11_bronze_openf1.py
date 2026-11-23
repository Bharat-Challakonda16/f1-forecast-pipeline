# Databricks notebook source
# MAGIC %md
# MAGIC # 11 · Bronze: OpenF1
# MAGIC Incrementally loads landed OpenF1 Parquet files into `bronze.openf1_<endpoint>` with
# MAGIC **Auto Loader**. Each file is processed exactly once (tracked in a checkpoint), so
# MAGIC re-running never duplicates rows and only new sessions are read.
# MAGIC
# MAGIC Values stay as received (all text); silver applies types. Every row also carries
# MAGIC `_source_file`, `_ingested_at` and `_source`.

# COMMAND ----------

import re

from pyspark.sql import functions as F

dbutils.widgets.text("catalog", "f1_forecast")
catalog = dbutils.widgets.get("catalog").strip()
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
    raise ValueError(f"Invalid catalog name: {catalog!r}")

landing = f"/Volumes/{catalog}/bronze/landing/openf1"
checkpoints = f"/Volumes/{catalog}/bronze/checkpoints/openf1"

ENDPOINTS = [
    "drivers",
    "laps",
    "stints",
    "pit",
    "weather",
    "race_control",
    "intervals",
    "position",
    "session_result",
    "starting_grid",
    "overtakes",
    "car_data",
    "location",
]


def has_files(path):
    try:
        return bool(dbutils.fs.ls(path))
    except Exception as exc:
        if "FileNotFoundException" in str(exc) or "NOT_FOUND" in str(exc):
            return False
        raise


# COMMAND ----------

results = {}
for endpoint in ENDPOINTS:
    source = f"{landing}/{endpoint}"
    table = f"`{catalog}`.`bronze`.`openf1_{endpoint}`"
    if not has_files(source):
        results[endpoint] = "no files yet"
        continue
    exists = spark.catalog.tableExists(table.replace("`", ""))
    before = spark.table(table).count() if exists else 0

    stream = (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "parquet")
        .option("cloudFiles.schemaLocation", f"{checkpoints}/{endpoint}/schema")
        # New columns from OpenF1 are added to the table instead of failing the load.
        .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
        .option("recursiveFileLookup", "true")
        .load(source)
        .withColumn("_source_file", F.col("_metadata.file_path"))
        .withColumn("_ingested_at", F.current_timestamp())
        .withColumn("_source", F.lit("openf1"))
    )
    query = (
        stream.writeStream.option("checkpointLocation", f"{checkpoints}/{endpoint}/checkpoint")
        .option("mergeSchema", "true")
        .trigger(availableNow=True)  # process everything new, then stop
        .toTable(table)
    )
    query.awaitTermination()
    after = spark.table(table).count()
    results[endpoint] = f"+{after - before:,} rows (total {after:,})"

for endpoint, msg in results.items():
    print(f"openf1_{endpoint:<16} {msg}")
