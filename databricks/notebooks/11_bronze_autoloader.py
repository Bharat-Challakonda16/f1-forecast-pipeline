# Databricks notebook source
# MAGIC %md
# MAGIC # 11 · Bronze: Auto Loader (OpenF1, FastF1)
# MAGIC Incrementally loads landed Parquet files from `landing/<source>/<table>/` into
# MAGIC `bronze.<source>_<table>` with **Auto Loader**. Each file is processed exactly once
# MAGIC (tracked in a checkpoint), so re-running never duplicates rows and only new files are read.
# MAGIC
# MAGIC Values stay as received (all text); silver applies types. Every row also carries
# MAGIC `_source_file`, `_ingested_at` and `_source`.
# MAGIC
# MAGIC Parameters: `catalog`, `source` (`openf1` or `fastf1`), `tables` (comma-separated).

# COMMAND ----------

import re

from pyspark.sql import functions as F

dbutils.widgets.text("catalog", "f1_forecast")
dbutils.widgets.text("source", "openf1")
dbutils.widgets.text("tables", "")
catalog = dbutils.widgets.get("catalog").strip()
source = dbutils.widgets.get("source").strip()
tables = [t.strip() for t in dbutils.widgets.get("tables").split(",") if t.strip()]

for value in [catalog, source, *tables]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Invalid identifier: {value!r}")
if not tables:
    raise ValueError("Pass the tables to load, e.g. tables=laps,car_data")

landing = f"/Volumes/{catalog}/bronze/landing/{source}"
checkpoints = f"/Volumes/{catalog}/bronze/checkpoints/{source}"


def has_files(path):
    try:
        return bool(dbutils.fs.ls(path))
    except Exception as exc:
        if "FileNotFoundException" in str(exc) or "NOT_FOUND" in str(exc):
            return False
        raise


# COMMAND ----------

results = {}
for name in tables:
    path = f"{landing}/{name}"
    table = f"`{catalog}`.`bronze`.`{source}_{name}`"
    if not has_files(path):
        results[name] = "no files yet"
        continue
    exists = spark.catalog.tableExists(table.replace("`", ""))
    before = spark.table(table).count() if exists else 0

    stream = (
        spark.readStream.format("cloudFiles")
        .option("cloudFiles.format", "parquet")
        .option("cloudFiles.schemaLocation", f"{checkpoints}/{name}/schema")
        # New columns from the source are added to the table instead of failing the load.
        .option("cloudFiles.schemaEvolutionMode", "addNewColumns")
        .option("recursiveFileLookup", "true")
        .load(path)
        .withColumn("_source_file", F.col("_metadata.file_path"))
        .withColumn("_ingested_at", F.current_timestamp())
        .withColumn("_source", F.lit(source))
    )
    query = (
        stream.writeStream.option("checkpointLocation", f"{checkpoints}/{name}/checkpoint")
        .option("mergeSchema", "true")
        .trigger(availableNow=True)  # process everything new, then stop
        .toTable(table)
    )
    query.awaitTermination()
    after = spark.table(table).count()
    results[name] = f"+{after - before:,} rows (total {after:,})"

for name, msg in results.items():
    print(f"{source}_{name:<16} {msg}")
