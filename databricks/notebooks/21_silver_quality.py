# Databricks notebook source
# MAGIC %md
# MAGIC # 21 · Silver data quality
# MAGIC Runs every check in `databricks/sql/quality/silver_checks.sql`, appends the results to
# MAGIC `silver.dq_results` (a history of every run) and **fails the job** if any `error`-severity
# MAGIC check finds violations. `warn` checks are recorded but don't fail.

# COMMAND ----------

import datetime as dt
import os
import re
import uuid

dbutils.widgets.text("catalog", "f1_forecast")
dbutils.widgets.text("checks_file", "")
catalog = dbutils.widgets.get("catalog").strip()
if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", catalog):
    raise ValueError(f"Invalid catalog name: {catalog!r}")
checks_file = dbutils.widgets.get("checks_file").strip() or os.path.abspath(
    "../sql/quality/silver_checks.sql"
)

STATEMENT_END = re.compile(r";[ \t]*(?:--[^\n]*)?$", re.MULTILINE)


def statements(sql):
    for part in STATEMENT_END.split(sql):
        code = [ln for ln in part.splitlines() if ln.strip() and not ln.strip().startswith("--")]
        if code:
            yield part.strip()


# COMMAND ----------

with open(checks_file, encoding="utf-8") as fh:
    sql = fh.read()

run_id, run_at = str(uuid.uuid4()), dt.datetime.now(dt.UTC)
rows = []
for stmt in statements(sql):
    name, severity, violations, sample = spark.sql(stmt.replace("${catalog}", catalog)).first()
    rows.append((run_id, run_at, name, severity, int(violations), sample))
    status = "pass" if violations == 0 else ("FAIL" if severity == "error" else "WARN")
    print(f"{status:<4} {name:<58} {violations:>8}  {(sample or '') if violations else ''}"[:200])

results = spark.createDataFrame(
    rows,
    "run_id string, run_at timestamp, check_name string, severity string, violations long, sample string",
)
results.write.mode("append").saveAsTable(f"`{catalog}`.`silver`.`dq_results`")

failed = [r for r in rows if r[3] == "error" and r[4] > 0]
if failed:
    raise AssertionError(
        f"{len(failed)} data quality check(s) failed: "
        + "; ".join(f"{r[2]} ({r[4]})" for r in failed)
    )
print(f"All error-level checks passed ({len(rows)} checks, run {run_id}).")
