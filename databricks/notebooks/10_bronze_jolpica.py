# Databricks notebook source
# MAGIC %md
# MAGIC # 10 · Bronze: Jolpica
# MAGIC Loads raw Jolpica files from the landing Volume into bronze tables, unchanged.
# MAGIC
# MAGIC | Table | Contents | Load |
# MAGIC |---|---|---|
# MAGIC | `bronze.jolpica_dump_<table>` | latest full CSV snapshot, all columns as strings | replaced when a newer dump lands |
# MAGIC | `bronze.jolpica_api_pages` | one row per API page (raw JSON) for rounds newer than the dump | `MERGE` on file path |
# MAGIC | `bronze.jolpica_schedule_snapshots` | daily calendar snapshots (raw JSON) | `MERGE` on file path |
# MAGIC
# MAGIC Every row carries `_ingested_at`, `_source` and `_source_file`. Safe to re-run.

# COMMAND ----------

import json
import re

from pyspark.sql import functions as F

dbutils.widgets.text("catalog", "f1_forecast")
catalog = dbutils.widgets.get("catalog").strip()
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
    raise ValueError(f"Invalid catalog name: {catalog!r}")

landing = f"/Volumes/{catalog}/bronze/landing/jolpica"
bronze = f"`{catalog}`.`bronze`"


def ls(path):
    try:
        return dbutils.fs.ls(path)
    except Exception as exc:  # missing folder: nothing landed yet
        if "FileNotFoundException" in str(exc) or "NOT_FOUND" in str(exc):
            return []
        raise


# COMMAND ----------

# MAGIC %md ## Dump snapshot

# COMMAND ----------

dumps = sorted(
    f.path.rstrip("/").rsplit("/", 1)[-1]
    for f in ls(f"{landing}/dump/")
    if any(x.name == "_SUCCESS" for x in ls(f.path))
)
if not dumps:
    raise RuntimeError(f"No complete dump under {landing}/dump/. Run the Jolpica ingest first.")
dump_date = dumps[-1]
dump_dir = f"{landing}/dump/{dump_date}"
meta = json.loads(dbutils.fs.head(f"{dump_dir}/_SUCCESS"))
print(f"Latest dump: {dump_date} sha256={meta['sha256'][:12]}…")


def loaded_dump_date(table):
    if not spark.catalog.tableExists(table.replace("`", "")):
        return None
    return spark.table(table).agg(F.max("_dump_date")).first()[0]


csv_files = sorted(f.name for f in ls(dump_dir) if f.name.endswith(".csv"))
for name in csv_files:
    table = f"{bronze}.`jolpica_dump_{name.removeprefix('formula_one_').removesuffix('.csv')}`"
    if loaded_dump_date(table) == dump_date:
        print(f"= {table}: already at {dump_date}")
        continue
    df = (
        spark.read.option("header", True)
        .option("inferSchema", False)  # bronze keeps values exactly as received
        .option("multiLine", True)
        .option("escape", '"')
        .csv(f"{dump_dir}/{name}")
        .withColumn("_dump_date", F.lit(dump_date))
        .withColumn("_dump_sha256", F.lit(meta["sha256"]))
        .withColumn("_source", F.lit("jolpica_dump"))
        .withColumn("_source_file", F.col("_metadata.file_path"))
        .withColumn("_ingested_at", F.current_timestamp())
    )
    df.write.mode("overwrite").option("overwriteSchema", True).saveAsTable(table)
    print(f"+ {table}: {spark.table(table).count():,} rows from {dump_date}")

# COMMAND ----------

# MAGIC %md ## API pages and schedule snapshots

# COMMAND ----------


def merge_raw_json(glob, table, extra_cols):
    """Upsert one row per landed JSON file, keyed on its path (re-runs never duplicate)."""
    files = spark.read.option("wholetext", True).text(glob)
    if files.isEmpty():
        print(f"- {table}: no files")
        return
    src = files.select(
        F.col("value").alias("payload"),
        F.col("_metadata.file_path").alias("_source_file"),
        F.col("_metadata.file_modification_time").alias("_landed_at"),
    )
    for name, expr in extra_cols.items():
        src = src.withColumn(name, expr)
    src = src.withColumn("_source", F.lit("jolpica_api")).withColumn(
        "_ingested_at", F.current_timestamp()
    )
    if not spark.catalog.tableExists(table.replace("`", "")):
        src.limit(0).write.saveAsTable(table)
    src.createOrReplaceTempView("incoming")
    spark.sql(f"""
        MERGE INTO {table} AS t
        USING incoming AS s
        ON t._source_file = s._source_file
        WHEN NOT MATCHED THEN INSERT *
        """)
    print(f"~ {table}: {spark.table(table).count():,} rows")


path = F.col("_source_file")
merge_raw_json(
    f"{landing}/api/[0-9]*/*/*/offset=*.json",
    f"{bronze}.`jolpica_api_pages`",
    {
        "season": F.regexp_extract(path, r"/api/(\d{4})/", 1).cast("int"),
        "round": F.regexp_extract(path, r"/api/\d{4}/(\d+)/", 1).cast("int"),
        "endpoint": F.regexp_extract(path, r"/api/\d{4}/\d+/([a-z]+)/", 1),
        "page_offset": F.regexp_extract(path, r"offset=(\d+)", 1).cast("int"),
    },
)
merge_raw_json(
    f"{landing}/api/schedule/*/*.json",
    f"{bronze}.`jolpica_schedule_snapshots`",
    {
        "season": F.regexp_extract(path, r"/schedule/(\d{4})/", 1).cast("int"),
        "snapshot_date": F.to_date(F.regexp_extract(path, r"/(\d{4}-\d{2}-\d{2})\.json$", 1)),
    },
)
