# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Hello Delta
# MAGIC Writes a tiny table with `MERGE` on a natural key, the same pattern every real
# MAGIC pipeline in this project uses. Re-running it must never duplicate rows.

# COMMAND ----------

import re

dbutils.widgets.text("catalog", "f1_forecast")
catalog = dbutils.widgets.get("catalog").strip()
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
    raise ValueError(f"Invalid catalog name: {catalog!r}")

table = f"`{catalog}`.`bronze`.`_hello`"

# COMMAND ----------

spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {table} (
        id INT NOT NULL,
        message STRING,
        _ingested_at TIMESTAMP,
        _source STRING
    ) USING DELTA
    """)

source = spark.createDataFrame(
    [(1, "hello"), (2, "lakehouse")],
    "id INT, message STRING",
)
source.createOrReplaceTempView("hello_source")

spark.sql(f"""
    MERGE INTO {table} AS t
    USING (
        SELECT id, message, current_timestamp() AS _ingested_at, 'phase0_hello' AS _source
        FROM hello_source
    ) AS s
    ON t.id = s.id
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
    """)

# COMMAND ----------

row_count = spark.table(table).count()
assert row_count == 2, f"Expected 2 rows after idempotent MERGE, found {row_count}"
print(f"OK: {table} has {row_count} rows")
