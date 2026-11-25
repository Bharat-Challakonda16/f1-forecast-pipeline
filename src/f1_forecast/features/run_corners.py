"""Compute corner-speed profiles for every qualifying session into ``gold.corner_speeds``.

Reads each session's best-lap speed traces from ``gold.quali_best_lap_telemetry`` (built in
Databricks), runs :mod:`f1_forecast.features.corners`, and merges the results. Sessions
already present are skipped unless ``--all`` is given.

    python -m f1_forecast.features.run_corners [--all] [--sessions 2026_21_Q ...]
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import time
from datetime import UTC, datetime

import pandas as pd

from f1_forecast.features.corners import session_corner_speeds
from f1_forecast.features.run_traits import CATALOG, OUTPUT_VOLUME, execute, query_df

log = logging.getLogger(__name__)

MERGE_SQL = """
CREATE TABLE IF NOT EXISTS {catalog}.gold.corner_speeds (
  session_id STRING, season INT, round INT, constructor_id STRING, corner_class STRING,
  kph_vs_field DOUBLE, corners INT, computed_at TIMESTAMP
) COMMENT 'Per quali session and team: apex speed vs field (kph) by corner class'
;
MERGE INTO {catalog}.gold.corner_speeds t
USING (
  SELECT session_id, CAST(season AS INT) AS season, CAST(round AS INT) AS round,
         constructor_id, corner_class, kph_vs_field, CAST(corners AS INT) AS corners,
         CAST(computed_at AS TIMESTAMP) AS computed_at
  FROM read_files('{path}', format => 'parquet')
) s
ON t.session_id = s.session_id AND t.constructor_id = s.constructor_id
   AND t.corner_class = s.corner_class
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""


def run(sessions: list[str] | None = None, recompute_all: bool = False) -> dict:
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    wh = next(iter(w.warehouses.list())).id
    available = query_df(
        w,
        wh,
        f"SELECT DISTINCT session_id, season, round FROM {CATALOG}.gold.quali_best_lap_telemetry",
    )
    todo = available
    if sessions:
        todo = todo[todo["session_id"].isin(sessions)]
    elif not recompute_all:
        try:
            done = query_df(w, wh, f"SELECT DISTINCT session_id FROM {CATALOG}.gold.corner_speeds")
            todo = todo[~todo["session_id"].isin(done["session_id"])]
        except RuntimeError:
            pass  # table not created yet
    log.info("%d sessions to process", len(todo))

    results, t0 = [], time.monotonic()
    for i, row in enumerate(todo.sort_values("session_id").itertuples(), 1):
        laps = query_df(
            w,
            wh,
            f"""SELECT driver_id, constructor_id, t_s, speed_kph
                FROM {CATALOG}.gold.quali_best_lap_telemetry
                WHERE session_id = '{row.session_id}' AND constructor_id IS NOT NULL""",
        ).astype({"t_s": float, "speed_kph": float})
        out = session_corner_speeds(laps)
        out.insert(0, "session_id", row.session_id)
        out.insert(1, "season", int(row.season))
        out.insert(2, "round", int(row.round))
        results.append(out)
        if i % 25 == 0:
            log.info("[%d/%d] %.0fs", i, len(todo), time.monotonic() - t0)
    if not results:
        return {"sessions": 0}

    df = pd.concat(results, ignore_index=True)
    df["computed_at"] = datetime.now(UTC).replace(tzinfo=None)
    execute(w, wh, f"CREATE VOLUME IF NOT EXISTS {CATALOG}.gold.model_outputs")
    path = f"{OUTPUT_VOLUME}/corner_speeds/{datetime.now(UTC):%Y%m%dT%H%M%S}.parquet"
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    w.files.upload(path, io.BytesIO(buf.getvalue()), overwrite=True)
    for stmt in MERGE_SQL.format(catalog=CATALOG, path=path).split("\n;\n"):
        execute(w, wh, stmt)
    return {
        "sessions": len(todo),
        "rows": len(df),
        # distinct corners per session (max over teams per class, summed over classes)
        "corners_per_session": round(
            df.groupby(["session_id", "corner_class"])["corners"]
            .max()
            .groupby("session_id")
            .sum()
            .mean(),
            1,
        ),
        "seconds": round(time.monotonic() - t0),
    }


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--all", action="store_true", help="Recompute every session")
    p.add_argument("--sessions", nargs="+", help="Only these session ids")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(json.dumps(run(args.sessions, args.all), indent=1))


if __name__ == "__main__":
    main()
