"""Fit driver-trait models and store the results in ``gold.driver_traits``.

Reads model inputs from the gold layer through the SQL warehouse, fits on this machine
(a CI runner, or locally for samples), writes the estimates as Parquet to the
``gold.model_outputs`` Volume and merges them into ``gold.driver_traits``.

Every fit is stored with its ``data_cutoff`` (only sessions before that date were used) and
``model_version``, so earlier fits are never overwritten and backtests can use exactly what
was known at the time.

    python -m f1_forecast.features.run_traits --trait quali_pace [--as-of 2026-06-01]
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)

CATALOG = "f1_forecast"
OUTPUT_VOLUME = f"/Volumes/{CATALOG}/gold/model_outputs"


@dataclass(frozen=True)
class Trait:
    name: str
    model_version: str
    query: str  # must return the columns the fit function expects; {catalog} {as_of} filled in
    fit: Callable[..., tuple[pd.DataFrame, dict[str, Any]]]
    description: str
    fit_kwargs: dict[str, Any] = field(default_factory=dict)


def _teammate_fit(df: pd.DataFrame, **kw: Any):
    from f1_forecast.features.teammate_model import fit

    return fit(df, **kw)


def _rate_fit(df: pd.DataFrame, **kw: Any):
    from f1_forecast.features.rate_model import fit

    return fit(df, **kw)


TRAITS: dict[str, Trait] = {
    "quali_pace": Trait(
        name="quali_pace",
        model_version="teammate_rw_studentt_v1",
        description="Qualifying time deficit vs an average driver, % of lap time (lower = faster)",
        query="""
            SELECT g.season, g.driver_a, g.driver_b, g.gap_pct
            FROM {catalog}.gold.quali_teammate_gaps g
            JOIN {catalog}.silver.events e ON e.season = g.season AND e.round = g.round
            WHERE NOT g.is_outlier AND NOT coalesce(g.is_wet, false)
              AND e.race_date < DATE'{as_of}'
        """,
        fit=_teammate_fit,
        fit_kwargs={"prior_scale": 1.0},
    ),
    "race_pace": Trait(
        name="race_pace",
        model_version="teammate_rw_studentt_v1",
        description="Race-lap deficit vs an average driver on shared clean laps, % (lower=faster)",
        query="""
            SELECT g.season, g.driver_a, g.driver_b, g.gap_pct
            FROM {catalog}.gold.race_teammate_gaps g
            JOIN {catalog}.silver.events e ON e.season = g.season AND e.round = g.round
            WHERE abs(g.gap_pct) <= 5 AND e.race_date < DATE'{as_of}'
        """,
        fit=_teammate_fit,
        fit_kwargs={"prior_scale": 1.0},
    ),
    "consistency": Trait(
        name="consistency",
        model_version="teammate_rw_studentt_v1",
        description="Lap-time spread vs an average driver, 100*log ratio (lower = more consistent)",
        query="""
            SELECT g.season, g.driver_a, g.driver_b, g.gap_pct
            FROM {catalog}.gold.consistency_teammate_gaps g
            JOIN {catalog}.silver.events e ON e.season = g.season AND e.round = g.round
            WHERE e.race_date < DATE'{as_of}'
        """,
        fit=_teammate_fit,
        fit_kwargs={"prior_scale": 25.0},
    ),
    "tyre_wear": Trait(
        name="tyre_wear",
        model_version="teammate_rw_studentt_v1",
        description=(
            "Tyre wear vs an average driver: extra % lap time per 10 laps of tyre age "
            "(lower = gentler on tyres), 2018+"
        ),
        query="""
            SELECT g.season, g.driver_a, g.driver_b, g.gap_pct
            FROM {catalog}.gold.tyre_wear_teammate_gaps g
            JOIN {catalog}.silver.events e ON e.season = g.season AND e.round = g.round
            WHERE abs(g.gap_pct) <= 5 AND e.race_date < DATE'{as_of}'
        """,
        fit=_teammate_fit,
        fit_kwargs={"prior_scale": 0.5},
    ),
    "starts": Trait(
        name="starts",
        model_version="teammate_rw_studentt_v1",
        description="Places lost on lap 1 vs expectation for the grid slot (lower=better)",
        query="""
            SELECT g.season, g.driver_a, g.driver_b, g.gap_places AS gap_pct
            FROM {catalog}.gold.start_teammate_gaps g
            JOIN {catalog}.silver.events e ON e.season = g.season AND e.round = g.round
            WHERE abs(g.gap_places) <= 12 AND e.race_date < DATE'{as_of}'
        """,
        fit=_teammate_fit,
        fit_kwargs={"prior_scale": 1.5},
    ),
    "error_rate": Trait(
        name="error_rate",
        model_version="poisson_rw_season_v1",
        description="Driver-error retirements (crash/collision/spin) per race start (lower=safer)",
        query="""
            SELECT driver_id, season, error_dnfs AS events, race_starts AS exposure
            FROM {catalog}.gold.driver_season_errors
            WHERE season <= year(DATE'{as_of}')
        """,
        fit=_rate_fit,
        fit_kwargs={"base_rate": 0.07},
    ),
    "wet_skill": Trait(
        name="wet_skill",
        model_version="teammate_rw_studentt_v1",
        description=(
            "Extra time deficit in the wet beyond the dry gap to the teammate, % "
            "(lower = better in the rain); fit after race_pace and quali_pace"
        ),
        # Residual = wet gap minus the dry gap the two drivers' dry traits predict (latest
        # dry fit at or before the cutoff, so backtests stay free of look-ahead).
        query="""
            WITH dry AS (
              SELECT trait, driver_id, as_of_season, value
              FROM {catalog}.gold.driver_traits t
              WHERE trait IN ('race_pace', 'quali_pace')
                AND model_version = 'teammate_rw_studentt_v1'
                AND data_cutoff = (
                  SELECT max(data_cutoff) FROM {catalog}.gold.driver_traits x
                  WHERE x.trait = t.trait AND x.data_cutoff <= DATE'{as_of}'
                )
            )
            SELECT g.season, g.driver_a, g.driver_b, g.gap_pct - (a.value - b.value) AS gap_pct
            FROM {catalog}.gold.wet_teammate_gaps g
            JOIN {catalog}.silver.events e ON e.season = g.season AND e.round = g.round
            JOIN dry a ON a.trait = g.dry_trait AND a.driver_id = g.driver_a
                      AND a.as_of_season = g.season
            JOIN dry b ON b.trait = g.dry_trait AND b.driver_id = g.driver_b
                      AND b.as_of_season = g.season
            WHERE abs(g.gap_pct) <= 8 AND e.race_date < DATE'{as_of}'
        """,
        fit=_teammate_fit,
        fit_kwargs={"prior_scale": 1.0},
    ),
}


def query_df(w, warehouse_id: str, sql: str) -> pd.DataFrame:
    r = w.statement_execution.execute_statement(
        statement=sql, warehouse_id=warehouse_id, wait_timeout="50s"
    )
    while r.status.state.value in ("PENDING", "RUNNING"):
        time.sleep(2)
        r = w.statement_execution.get_statement(r.statement_id)
    if r.status.state.value != "SUCCEEDED":
        raise RuntimeError(r.status.error.message)
    if r.manifest.total_chunk_count and r.manifest.total_chunk_count > 1:
        raise RuntimeError("result too large for inline fetch; add EXTERNAL_LINKS handling")
    cols = [c.name for c in r.manifest.schema.columns]
    return pd.DataFrame(r.result.data_array or [], columns=cols)


def execute(w, warehouse_id: str, sql: str) -> None:
    query_df(w, warehouse_id, sql)


def to_gold_rows(est: pd.DataFrame, trait: Trait, as_of: date, fitted_at: datetime) -> pd.DataFrame:
    out = est.rename(columns={"season": "as_of_season", "n_obs": "n_samples"}).copy()
    out.insert(0, "trait", trait.name)
    out["model_version"] = trait.model_version
    out["data_cutoff"] = as_of
    out["fitted_at"] = fitted_at
    return out[
        [
            "trait",
            "driver_id",
            "as_of_season",
            "value",
            "std_err",
            "q05",
            "q95",
            "n_samples",
            "model_version",
            "data_cutoff",
            "fitted_at",
        ]
    ]


MERGE_SQL = """
CREATE TABLE IF NOT EXISTS {catalog}.gold.driver_traits (
  trait STRING, driver_id STRING, as_of_season INT, value DOUBLE, std_err DOUBLE,
  q05 DOUBLE, q95 DOUBLE, n_samples BIGINT, model_version STRING, data_cutoff DATE,
  fitted_at TIMESTAMP
) COMMENT 'Driver traits with uncertainty; one version per (trait, model_version, data_cutoff)'
;
MERGE INTO {catalog}.gold.driver_traits t
USING (
  SELECT trait, driver_id, CAST(as_of_season AS INT) AS as_of_season, value, std_err, q05, q95,
         CAST(n_samples AS BIGINT) AS n_samples, model_version,
         CAST(data_cutoff AS DATE) AS data_cutoff, CAST(fitted_at AS TIMESTAMP) AS fitted_at
  FROM read_files('{path}', format => 'parquet')
) s
ON t.trait = s.trait AND t.model_version = s.model_version AND t.data_cutoff = s.data_cutoff
   AND t.driver_id = s.driver_id AND t.as_of_season = s.as_of_season
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
"""


def run(trait_name: str, as_of: date, *, draws: int, tune: int, chains: int) -> dict[str, Any]:
    from databricks.sdk import WorkspaceClient

    trait = TRAITS[trait_name]
    w = WorkspaceClient()
    warehouse_id = next(iter(w.warehouses.list())).id

    df = query_df(w, warehouse_id, trait.query.format(catalog=CATALOG, as_of=as_of.isoformat()))
    log.info("%s: %d observations up to %s", trait.name, len(df), as_of)
    t0 = time.monotonic()
    est, diagnostics = trait.fit(df, draws=draws, tune=tune, chains=chains, **trait.fit_kwargs)
    fitted_at = datetime.now(UTC).replace(tzinfo=None)
    rows = to_gold_rows(est, trait, as_of, fitted_at)

    execute(w, warehouse_id, f"CREATE VOLUME IF NOT EXISTS {CATALOG}.gold.model_outputs")
    path = (
        f"{OUTPUT_VOLUME}/driver_traits/{trait.name}/{trait.model_version}/cutoff={as_of}.parquet"
    )
    buf = io.BytesIO()
    rows.to_parquet(buf, index=False)
    w.files.upload(path, io.BytesIO(buf.getvalue()), overwrite=True)
    meta = {**diagnostics, "trait": trait.name, "data_cutoff": str(as_of), "rows": len(rows)}
    w.files.upload(
        path.replace(".parquet", ".json"), io.BytesIO(json.dumps(meta).encode()), overwrite=True
    )

    for stmt in MERGE_SQL.format(catalog=CATALOG, path=path).split("\n;\n"):
        execute(w, warehouse_id, stmt)
    meta["fit_seconds"] = round(time.monotonic() - t0)
    return meta


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Fit a driver-trait model into gold.driver_traits.")
    p.add_argument("--trait", choices=sorted(TRAITS), required=True)
    p.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(UTC).date())
    p.add_argument("--draws", type=int, default=1000)
    p.add_argument("--tune", type=int, default=1000)
    p.add_argument("--chains", type=int, default=4)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print(
        json.dumps(
            run(args.trait, args.as_of, draws=args.draws, tune=args.tune, chains=args.chains),
            indent=1,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
