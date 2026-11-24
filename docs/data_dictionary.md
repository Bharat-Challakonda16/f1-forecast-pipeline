# Data dictionary

Unity Catalog: `f1_forecast` · schemas `bronze` → `silver` → `gold` (gold arrives in Phase 2).
Row counts as of **2026-11-24** (data through the 2026 Las Vegas GP, round 21).

## How data flows

| Step | Where | How |
|---|---|---|
| Download | GitHub Actions (`ingest_*.yml`) | Polite, rate-limited, never re-downloads; writes straight to the landing Volume |
| Landing | `/Volumes/f1_forecast/bronze/landing/<source>/…` | Raw files, immutable once written |
| Bronze | `bronze.*` | Values exactly as received (text). Jolpica dump = full snapshot per dump; API pages merged on file path; OpenF1/FastF1/Open-Meteo via Auto Loader (exactly once) |
| Silver | `silver.*` | Typed, conformed IDs, rebuilt in full from bronze on every run (`build_silver.yml`, daily). Proven idempotent: identical content hash across runs |
| Quality | `silver.dq_results` | 33 checks after every build; `error` checks fail the job |

**Conformed keys**

| Key | Format | Example |
|---|---|---|
| `driver_id` | Jolpica driver reference (generated `first_last` for practice-only drivers Jolpica doesn't know) | `alonso`, `mick_schumacher`, `sean_gelael` |
| `constructor_id` | Jolpica team reference | `red_bull` |
| `circuit_id` | Jolpica circuit reference | `monza` |
| `session_code` | `FP1` `FP2` `FP3` `SQ` (sprint qualifying/shootout) `S` (sprint) `Q` `R` | `Q` |
| `session_id` | `<season>_<round>_<session_code>` | `2025_17_Q` |

## Silver

| Table | Key | Rows | What it holds |
|---|---|---:|---|
| `seasons` | season | 27 | 2001–2027 with `regulation_era` (v10 → active_aero_2026) and `is_major_rule_change` |
| `circuits` | circuit_id | 39 | Every circuit raced since 2001 + 2027 venues; location (Jolpica) + hand-curated facts (`configs/circuits_seed.csv`) |
| `events` | season, round | 533 | Every round 2001–2027; `status` completed / scheduled / cancelled; 2027 from the announced calendar until Jolpica publishes it |
| `sessions` | session_id | 2,284 | Every session with keys into each source (`openf1_session_key`, `has_fastf1`) |
| `drivers` | driver_id | 159 | Everyone who raced since 2001 (+ 11 practice-only test drivers); DOB, debut season |
| `constructors` | constructor_id | 40 | Teams since 2001 with `color_hex` |
| `entries` | season, round, car_number | 10,737 | Who drove which car for which team; `is_race_entry` separates reserve (FP-only) drivers |
| `driver_seasons` | season, driver_id, constructor_id | 641 | Race entries per season with `teammate_ids`: basis of the teammate network |
| `session_drivers` | session_id, car_number | 19,565 | Car number → driver for FastF1/OpenF1 sessions (`mapped_via`: entry / FastF1 id / name / generated) |
| `results` | session_id, driver_id | 11,251 | Race + sprint classification; `status_class` finished / accident / mechanical / other / disqualified / did_not_start |
| `qualifying` | session_id, driver_id | 10,417 | Q + SQ: final position, best time per part (`q1_ms` `q2_ms` `q3_ms`), `format` |
| `laps` | session_id, driver_id, lap | 879,340 | Every lap: official time/position (Jolpica, 2001+) + sectors, speed traps, tyres, stint, pit flags, track status (FastF1 2018–22, OpenF1 2023+); `lap_time_flag` marks outliers (never deleted) |
| `pit_stops` | session_id, driver_id, stop_number | 34,502 | Total pit time (Jolpica 2011+) + lane / stationary time (OpenF1 2023+) |
| `session_weather` | session_id, sample_utc | 95,083 | Trackside air/track temp, humidity, rain, wind (~1/min), 2018+ |
| `weather_hourly` | circuit_id, time_utc | 8,760 * | Hourly history per circuit since 2001 (Open-Meteo). *Backfill in progress (~4 days); ~8.9M rows when complete |
| `car_telemetry` (view) | — | ~442M | Speed, RPM, gear, throttle, brake, DRS at ~4 Hz, 2018+ (view over bronze) |
| `car_positions` (view) | — | ~438M | x/y/z position at ~4 Hz, 2018+ (view over bronze) |
| `dq_results` | run_id, check_name | 33/run | History of every quality check run |

### Data era coverage

| | 2001–2010 | 2011–2017 | 2018–2022 | 2023 → |
|---|---|---|---|---|
| Results, qualifying, race lap times | ✅ | ✅ | ✅ | ✅ |
| Pit stops | — | ✅ | ✅ | ✅ (+ lane/stationary time) |
| Tyres, sectors, practice laps, telemetry, positions, trackside weather | — | — | ✅ FastF1 | ✅ OpenF1 |
| Track status per lap (green/SC/VSC/red) | — | — | ✅ | Phase 2 (from race control) |
| Hourly weather at the circuit | ✅ | ✅ | ✅ | ✅ |

### Known gaps (documented, not hidden)

- 2001–2002 qualifying is mostly missing at the source; full from 2003.
- Sessions that never ran: 2019 Japan FP3 (typhoon), 2020 Styria FP3 (rain), 2020 Eifel FP1+FP2 (fog), 2021 Russia FP3 (rain).
- 2018 Monza race: Ricciardo's tyre compounds missing (FastF1 tyre-data bug; lap times complete).
- 2025 Baku qualifying laps come from FastF1 (OpenF1 has none).
- OpenF1 feeds occasionally include non-F1 cars (2023 Hungary FP1); they are excluded in silver.

## Bronze

| Source | Tables | Largest |
|---|---|---|
| Jolpica dump (snapshot) | `jolpica_dump_*` (18 tables) | `jolpica_dump_lap` 746,897 · `jolpica_dump_sessionentry` 52,256 |
| Jolpica API | `jolpica_api_pages`, `jolpica_schedule_snapshots` | rounds newer than the dump |
| FastF1 (2018–22) | `fastf1_*` (9 tables) | `fastf1_car_data` 242.5M · `fastf1_position` 234.3M · `fastf1_laps` 283,642 |
| OpenF1 (2023+) | `openf1_*` (13 tables) | `openf1_location` 203.9M · `openf1_car_data` 199.9M · `openf1_intervals` 2.4M |
| Open-Meteo | `openmeteo_archive` | backfilling |
| Manual seeds | `seed_calendar_2027` (24), `seed_circuits` (28) | |

Every bronze row carries `_source`, `_source_file` and `_ingested_at`.
