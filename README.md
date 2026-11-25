# F1 Forecast Pipeline (working name: Grid Oracle)

> Data-driven predictions for the **upcoming** Formula 1 races (whatever is next on the calendar, today), updated after every session, built on a Databricks lakehouse with Monte Carlo race simulation. Public launch target: before the 2027 season opener.

**Unofficial fan project. Not affiliated with Formula 1.** Non-commercial. No F1 or team logos, no driver photos.

---

## What it will do

- **Qualifying:** pole and grid-position probabilities per driver
- **Race:** win / podium / points probabilities from 10,000 lap-by-lap Monte Carlo simulations per event
- **Strategy:** each team's likely tyre strategy
- **Season:** drivers' and constructors' championship odds, updated after every race
- **Weather-aware:** climate history → seasonal outlook → 16-day forecast, staged by time to the race
- **Driver traits since 2001:** teammate-network model (qualifying pace, race pace, wet skill, starts, tyre management, consistency, error rate)
- **Rules lab:** change technical regulations and re-simulate with a physics lap-time model
- **Honest validation:** published backtests against simple baselines

## Planned architecture

```
 Sources (Jolpica, FastF1, OpenF1, Open-Meteo, RSS/GDELT)
        │  GitHub Actions (scheduled) + local scripts download & cache raw files
        ▼
 ┌──────────── Databricks Free Edition (Unity Catalog) ────────────┐
 │ BRONZE  raw, as received, with ingest metadata                  │
 │ SILVER  clean, typed, deduplicated, quality-checked             │
 │ GOLD    features + simulation inputs + versioned predictions    │
 └─────────────────────────────────────────────────────────────────┘
        ▼
 Simulation (NumPy / Spark): qualifying → Monte Carlo race → season
        ▼
 Precomputed JSON/Parquet → Streamlit (MVP) → React static site (final)
```

Pipelines are incremental and idempotent: downloads never repeat (landed files are immutable), bronze loads exactly once (Auto Loader / `MERGE` on file path), and silver is rebuilt deterministically from bronze with 33 data quality checks that fail the build. Every prediction run will be stored with its input versions.

**Current data (Phase 1):** 2001 → latest race results, qualifying and ~880k laps; 2018+ telemetry and car positions (~880M rows, FastF1 + OpenF1); hourly weather per circuit since 2001. See [docs/data_dictionary.md](docs/data_dictionary.md) for every table, key and row count, and [docs/methodology.md](docs/methodology.md) for how each feature is measured and validated.

## Phase checklist

- [x] **0. Setup:** repo, tooling, CI, Databricks catalog/schemas, hello Delta job
- [x] **1. Data pipeline:** Jolpica (2001+), OpenF1 (2023+), FastF1 (2018–22), Open-Meteo history, 2027 calendar, circuit facts; bronze → silver; 33 DQ checks; idempotency proven
- [x] **2. Features:** Bayesian driver traits (7, since 2001), car profiles incl. telemetry corner speeds, tyre models, safety-car / incident rates, pace trends; 36 known-fact and quality checks
- [ ] **3. Simulator:** qualifying model, vectorized race sim, season aggregator, 2026 backtest
- [ ] **4. MVP launch:** Streamlit app
- [ ] **5. Weather pipeline:** staged weather, track-temp calibration, daily refresh, prediction versioning
- [ ] **6. Constructors + news:** development carry-over, LLM news signals, news backtest
- [ ] **7. Rules lab:** physics lap simulator, regulation sliders, 2021→2022 backtest
- [ ] **8. Polish + launch:** React front end, full docs, backtest report
- [ ] **9. Run it:** automatic post-race refresh and live accuracy tracking

## Repo layout

```
configs/                 sources.yaml (endpoints, limits), calendar_2027.csv, circuits_seed.csv
src/f1_forecast/ingest/  downloaders: jolpica, openf1, fastf1_ingest, openmeteo (+ shared http client)
src/f1_forecast/features/ Bayesian trait models (PyMC), corner detection, fit runners
src/f1_forecast/ops/     run_job (trigger Databricks jobs), run_sql (run layered SQL locally)
databricks/notebooks/    bronze loaders (10-12), silver build + quality (20-21)
databricks/sql/          silver/*.sql, gold/*.sql (rebuilt in order), quality/*_checks.sql
databricks/resources/    Databricks jobs (Asset Bundle)
.github/workflows/       ci, ingest_{jolpica,openf1,fastf1,openmeteo}, build_silver, fit_features
docs/                    data_dictionary.md, methodology.md
tests/                   pytest suite
```

## Local setup

Requires Python 3.11+.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pre-commit install
pytest
```

### Databricks (project-local, isolated from any other profiles)

Everything Databricks-related stays inside this repo folder; `~/.databrickscfg` is never read or written.

1. Download the Databricks CLI (Windows zip from the
   [databricks/cli releases](https://github.com/databricks/cli/releases)) and put `databricks.exe` in `.tools/` (gitignored).
2. Create `.databrickscfg` in the repo root (gitignored):

   ```ini
   [f1forecast]
   host  = https://<your-workspace>.cloud.databricks.com
   token = <personal access token>
   ```

3. Run through the wrapper, which pins the config file and profile:

   ```powershell
   scripts\dbx.cmd current-user me
   scripts\dbx.cmd bundle validate
   scripts\dbx.cmd bundle deploy
   scripts\dbx.cmd bundle run hello_delta
   ```

   If the workspace can't create catalogs, add `--var catalog=workspace` to `deploy` and `run`.

## Data sources and attribution

- [Jolpica-F1](https://github.com/jolpica/jolpica-f1): results, qualifying, laps, pit stops since 2001 (free non-commercial CSV dump + API)
- [OpenF1](https://openf1.org/): 2023+ telemetry, positions, laps, tyres, weather (CC BY-NC-SA 4.0)
- [FastF1](https://docs.fastf1.dev/): 2018–2022 detail; since Oct 2026 F1's timing archive needs an F1 TV subscription, used once for the backfill. Raw F1 timing data is never republished; the app shows only derived results.
- [Open-Meteo](https://open-meteo.com/): weather history and forecasts (CC BY 4.0, non-commercial)
- RSS/GDELT (Phase 6): headlines and links only

See [configs/sources.yaml](configs/sources.yaml) for endpoints and rate limits.

## Disclaimer

Unofficial fan project, not affiliated with Formula 1, the FIA, or any team. F1 and related marks belong to their owners.
