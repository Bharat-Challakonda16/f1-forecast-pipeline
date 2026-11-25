"""Fit the joint field model (car per round + driver + track effects, see ``field_model``)
for qualifying and race pace and store it in ``gold.field_effects``.

Each fit is stored with its ``data_cutoff`` (only sessions before that date were used), so
backtests use exactly what was known at the time.

    python -m f1_forecast.features.run_field [--as-of 2026-06-01] [--kinds quali race]
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import time
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

from f1_forecast.features.run_traits import CATALOG, OUTPUT_VOLUME, execute, query_df

log = logging.getLogger(__name__)

MODEL_VERSION = "field_rw_v1"
FIRST_SEASON = 2010  # comparable lap times (no refuelling) and enough seasons for track effects
KINDS = ("quali", "race")

QUERY = """
    SELECT season, round, circuit_id, speed_profile, driver_id, constructor_id, pace_pct
    FROM {catalog}.gold.driver_session_pace
    WHERE kind = '{kind}' AND NOT is_wet AND NOT is_outlier
      AND season >= {first_season} AND race_date < DATE'{as_of}'
"""

COLUMNS = [
    "kind",
    "effect",
    "constructor_id",
    "driver_id",
    "circuit_id",
    "speed_profile",
    "season",
    "round",
    "value",
    "std_err",
    "n_samples",
    "model_version",
    "data_cutoff",
    "fitted_at",
]

TABLE_COMMENT = (
    "'Joint field model per data cutoff: car pace per team per round (upgrades), car x track "
    "type, driver skill per season, driver x track type and driver x circuit bonuses (% lap "
    "time, lower = faster); effect = hyper:<name> rows hold the fitted spreads'"
)

MERGE_SQL = """
CREATE TABLE IF NOT EXISTS {catalog}.gold.field_effects (
  kind STRING, effect STRING, constructor_id STRING, driver_id STRING, circuit_id STRING,
  speed_profile STRING, season INT, round INT, value DOUBLE, std_err DOUBLE, n_samples DOUBLE,
  model_version STRING, data_cutoff DATE, fitted_at TIMESTAMP
) COMMENT {comment}
;
DELETE FROM {catalog}.gold.field_effects
WHERE model_version = '{model_version}' AND data_cutoff = DATE'{as_of}' AND kind = '{kind}'
;
INSERT INTO {catalog}.gold.field_effects
SELECT kind, effect, constructor_id, driver_id, circuit_id, speed_profile,
       CAST(season AS INT), CAST(round AS INT), value, std_err, CAST(n_samples AS DOUBLE),
       model_version, CAST(data_cutoff AS DATE), CAST(fitted_at AS TIMESTAMP)
FROM read_files('{path}', format => 'parquet')
"""


def to_rows(effects: pd.DataFrame, diagnostics: dict[str, Any], kind: str, as_of: date):
    hyper = pd.DataFrame(
        [
            {"effect": f"hyper:{k}", "value": v["mean"], "std_err": v["sd"]}
            for k, v in diagnostics["hyper"].items()
        ]
    )
    out = pd.concat([effects, hyper], ignore_index=True)
    out["kind"], out["model_version"], out["data_cutoff"] = kind, MODEL_VERSION, as_of
    out["fitted_at"] = datetime.now(UTC).replace(tzinfo=None)
    for c in COLUMNS:
        if c not in out:
            out[c] = None
    out = out[COLUMNS]
    for c in ("season", "round", "n_samples"):
        out[c] = pd.to_numeric(out[c]).astype("Float64")
    return out


def run(
    as_of: date, kinds=KINDS, *, draws: int = 500, tune: int = 800, chains: int = 4
) -> dict[str, Any]:
    from databricks.sdk import WorkspaceClient

    from f1_forecast.features.field_model import fit

    w = WorkspaceClient()
    wh = next(iter(w.warehouses.list())).id
    execute(w, wh, f"CREATE VOLUME IF NOT EXISTS {CATALOG}.gold.model_outputs")
    meta: dict[str, Any] = {"data_cutoff": str(as_of)}
    for kind in kinds:
        sql = QUERY.format(
            catalog=CATALOG, kind=kind, first_season=FIRST_SEASON, as_of=as_of.isoformat()
        )
        df = query_df(w, wh, sql)
        log.info("field %s: %d observations before %s", kind, len(df), as_of)
        t0 = time.monotonic()
        effects, diagnostics = fit(df, draws=draws, tune=tune, chains=chains)
        rows = to_rows(effects, diagnostics, kind, as_of)
        path = f"{OUTPUT_VOLUME}/field_effects/{MODEL_VERSION}/{kind}/cutoff={as_of}.parquet"
        buf = io.BytesIO()
        rows.to_parquet(buf, index=False)
        w.files.upload(path, io.BytesIO(buf.getvalue()), overwrite=True)
        info = {**diagnostics, "kind": kind, "rows": len(rows)}
        info["fit_seconds"] = round(time.monotonic() - t0)
        w.files.upload(
            path.replace(".parquet", ".json"),
            io.BytesIO(json.dumps(info, default=str).encode()),
            overwrite=True,
        )
        for stmt in MERGE_SQL.format(
            catalog=CATALOG,
            model_version=MODEL_VERSION,
            as_of=as_of,
            kind=kind,
            path=path,
            comment=TABLE_COMMENT,
        ).split("\n;\n"):
            execute(w, wh, stmt)
        meta[kind] = info
    return meta


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Fit the joint field model into gold.field_effects.")
    p.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(UTC).date())
    p.add_argument("--kinds", nargs="+", choices=KINDS, default=list(KINDS))
    p.add_argument("--draws", type=int, default=500)
    p.add_argument("--tune", type=int, default=800)
    p.add_argument("--chains", type=int, default=4)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    out = run(args.as_of, args.kinds, draws=args.draws, tune=args.tune, chains=args.chains)
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
