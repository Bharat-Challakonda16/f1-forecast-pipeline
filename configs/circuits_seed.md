# Circuit facts (manual seed)

`circuits_seed.csv` holds facts that no API provides, for every circuit on the 2026/2027
calendars. Location, altitude and names come from Jolpica and are **not** repeated here.

| Column | Meaning |
|---|---|
| `circuit_ref` | Jolpica circuit reference id |
| `circuit_type` | `permanent`, `street` (city streets), `temporary` (public roads/park used for the event) |
| `speed_profile` | dominant corner type: `low`, `medium`, `high`, `mixed` |
| `length_km`, `turns`, `race_laps` | current layout |
| `overtaking_difficulty` | 1 = easy … 5 = nearly impossible (judgement; replaced by measured overtake rates in Phase 2) |
| `pit_lane_loss_s_est` | rough time lost by a pit stop vs. staying out, seconds |
| `confidence` | how sure we are of the hand-entered values: `high`, `medium`, `low` |

**Status: hand-curated, needs owner review.** These are best-known values, not measurements.
Phase 2 replaces the judgement columns with numbers computed from the data where possible:
pit-lane loss from OpenF1/FastF1 pit timings, overtaking difficulty from OpenF1 overtakes
and position changes. Blank cells (e.g. Madring) are filled from data.
