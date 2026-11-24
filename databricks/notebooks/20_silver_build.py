# Databricks notebook source
# MAGIC %md
# MAGIC # 20 · Silver build
# MAGIC Runs the SQL files in `databricks/sql/silver/` in name order. Every table is rebuilt in
# MAGIC full from bronze (`CREATE OR REPLACE`), so a run is deterministic and idempotent.
# MAGIC High-frequency telemetry is exposed as views over bronze (no second copy).
# MAGIC
# MAGIC The same files run locally with `python -m f1_forecast.ops.run_sql databricks/sql/silver`.

# COMMAND ----------

import os
import re
import time

dbutils.widgets.text("catalog", "f1_forecast")
dbutils.widgets.text("sql_dir", "")
catalog = dbutils.widgets.get("catalog").strip()
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
    raise ValueError(f"Invalid catalog name: {catalog!r}")
sql_dir = dbutils.widgets.get("sql_dir").strip() or os.path.abspath("../sql/silver")

# Same rule as f1_forecast.ops.run_sql: statements end with ';' at the end of a line.
STATEMENT_END = re.compile(r";[ \t]*(?:--[^\n]*)?$", re.MULTILINE)


def statements(sql):
    for part in STATEMENT_END.split(sql):
        code = [ln for ln in part.splitlines() if ln.strip() and not ln.strip().startswith("--")]
        if code:
            yield part.strip()


# COMMAND ----------

for name in sorted(f for f in os.listdir(sql_dir) if f.endswith(".sql")):
    with open(os.path.join(sql_dir, name), encoding="utf-8") as fh:
        sql = fh.read()
    for i, stmt in enumerate(statements(sql), 1):
        t0 = time.time()
        spark.sql(stmt.replace("${catalog}", catalog))
        first = next(ln for ln in stmt.splitlines() if not ln.startswith("--"))
        print(f"OK {name}#{i} {time.time() - t0:5.1f}s  {first[:90]}")
