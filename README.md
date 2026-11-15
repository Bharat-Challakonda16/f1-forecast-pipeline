# F1 Forecast Pipeline (working name: Grid Oracle)

> Data-driven, race-by-race predictions for the 2027 Formula 1 season, built on a Databricks lakehouse with Monte Carlo race simulation.

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

Pipelines are incremental and idempotent (Delta `MERGE` on natural keys). Every prediction run is stored with its input versions.

## Phase checklist

- [x] **0. Setup:** repo, tooling, CI, Databricks catalog/schemas, hello Delta job
- [ ] **1. Data pipeline:** Jolpica, FastF1, Open-Meteo archive, 2027 calendar, circuits seed; bronze → silver; DQ checks
- [ ] **2. Features:** driver traits, car profiles, tyre models, incident rates, pace trends
- [ ] **3. Simulator:** qualifying model, vectorized race sim, season aggregator, 2026 backtest
- [ ] **4. MVP launch:** Streamlit app
- [ ] **5. Weather pipeline:** staged weather, track-temp calibration, daily refresh, prediction versioning
- [ ] **6. Constructors + news:** development carry-over, LLM news signals, news backtest
- [ ] **7. Rules lab:** physics lap simulator, regulation sliders, 2021→2022 backtest
- [ ] **8. Polish + launch:** React front end, full docs, backtest report
- [ ] **9. Run it:** automatic post-race refresh and live accuracy tracking

## Repo layout

```
configs/            sources.yaml (API endpoints, rate limits)
src/f1_forecast/    Python package
tests/              pytest suite
databricks/         Asset Bundle (databricks.yml, jobs, notebooks)
scripts/            helper scripts (dbx.cmd)
.github/workflows/  CI
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

Jolpica-F1, FastF1, OpenF1 (historical), Open-Meteo (CC BY 4.0, non-commercial), RSS/GDELT (headlines and links only). See [configs/sources.yaml](configs/sources.yaml).

## Disclaimer

Unofficial fan project, not affiliated with Formula 1, the FIA, or any team. F1 and related marks belong to their owners.
