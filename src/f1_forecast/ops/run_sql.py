"""Run the layered SQL files (e.g. ``databricks/sql/silver/*.sql``) on a SQL warehouse.

The Databricks job runs the same files through ``databricks/notebooks/20_silver_build.py``;
this runner is for development and CI. Statements are separated by a semicolon at the end
of a line; ``${catalog}`` is substituted.

    python -m f1_forecast.ops.run_sql databricks/sql/silver [--only 10_reference.sql]
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

STATEMENT_END = re.compile(r";[ \t]*(?:--[^\n]*)?$", re.MULTILINE)


def split_statements(sql: str) -> list[str]:
    """Split on ``;`` at end of line; drop comment-only chunks."""
    parts = STATEMENT_END.split(sql)
    out = []
    for part in parts:
        code = "\n".join(
            line for line in part.splitlines() if line.strip() and not line.strip().startswith("--")
        )
        if code.strip():
            out.append(part.strip())
    return out


def render(sql: str, catalog: str) -> str:
    return sql.replace("${catalog}", catalog)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("sql_dir", type=Path)
    p.add_argument("--catalog", default="f1_forecast")
    p.add_argument("--only", nargs="+", help="Only these file names")
    p.add_argument(
        "--checks",
        action="store_true",
        help="Files hold quality checks (check_name, severity, violations, sample); "
        "fail if any 'error' check has violations",
    )
    args = p.parse_args(argv)

    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    warehouse = next(iter(w.warehouses.list()))
    files = sorted(args.sql_dir.glob("*.sql"))
    if args.only:
        files = [f for f in files if f.name in args.only]
    failed = False
    for f in files:
        for i, stmt in enumerate(split_statements(f.read_text(encoding="utf-8")), 1):
            first = next(line for line in stmt.splitlines() if not line.startswith("--"))
            t0 = time.monotonic()
            r = w.statement_execution.execute_statement(
                statement=render(stmt, args.catalog),
                warehouse_id=warehouse.id,
                wait_timeout="50s",
            )
            while r.status.state.value in ("PENDING", "RUNNING"):
                time.sleep(2)
                r = w.statement_execution.get_statement(r.statement_id)
            ok = r.status.state.value == "SUCCEEDED"
            if not ok:
                print(f"ERR {f.name}#{i}  {first[:80]}\n     {r.status.error.message[:1500]}")
                failed = True
                break
            if args.checks:
                name, severity, violations, sample = r.result.data_array[0]
                bad = int(violations) > 0
                mark = ("FAIL" if severity == "error" else "WARN") if bad else "pass"
                print(f"{mark:<4} {name:<58} {violations:>8}  {(sample or '')[:90] if bad else ''}")
                failed = failed or (bad and severity == "error")
            else:
                print(f"OK  {f.name}#{i} {time.monotonic() - t0:5.1f}s  {first[:80]}")
        if failed and not args.checks:
            break
        if failed:
            break
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
