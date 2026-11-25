# Methodology: features (Phase 2)

How every feature in the `gold` layer is measured, how it was validated, and what it can't do.
Tables and row counts are in [data_dictionary.md](data_dictionary.md); the code is in
`databricks/sql/gold/` (SQL features) and `src/f1_forecast/features/` (models).

## Principles

- **No look-ahead.** Car features are stored "as of" every round, using only rounds up to
  then. Driver traits are stored per `data_cutoff` and fitted only on races before it. A
  backtest at any past date sees exactly what was knowable then.
- **Every estimate carries its uncertainty.** Traits have a standard error and a 90%
  interval; rates are shrunk toward a sensible average when data is thin. The simulator
  samples from these ranges rather than using single numbers.
- **Measure, then check against what F1 people know.** 13 "known fact" checks (Monaco is
  the hardest place to overtake, Soft wears faster than Hard, Alonso is good in the wet, ...)
  run after every build and fail it if a code change breaks one.
- **Inferences are validated where the truth is known.** Several inputs are missing for
  older seasons. Each inferred value is tested on the seasons where the real value exists,
  and the scores are reported below.

## Driver traits

### Teammate comparisons

Teammates drive the same car, so the gap between them is mostly the driver. Chaining
teammates across seasons (A vs B, B vs C, ...) puts every driver since 2001 on one scale.
Only race entries count as teammates, not reserve drivers who ran a practice session.

| Trait | Observation per session (teammates *a*, *b*) | Years |
|---|---|---|
| Qualifying pace | 100 × ln(time_a / time_b) in the deepest part both reached (Q3 > Q2 > Q1) | 2001+ (sparse 2001–02) |
| Race pace | median of 100 × ln(t_a / t_b) over laps **both** ran clean | 2001+ |
| Consistency | 100 × ln(spread_a / spread_b); spread = median absolute deviation from the 5-lap local mean | 2001+ |
| Tyre management | difference in fuel-corrected wear slope on the same compound | 2018+ |
| Starts | lap-1 places lost vs the norm for that grid slot and era, a minus b | 2001+ |
| Wet skill | wet gap minus the gap the dry traits predict | 2001+ (see wet detection) |

A **clean race lap** is not lap 1, not under a safety car / VSC / red flag, not a pit in- or
out-lap, at least 1.5 s behind the car ahead, and within 107% of the driver's typical lap.
61% of race laps qualify.

### Model (PyMC, Bayesian hierarchical)

For each observation *i* of teammates *a*, *b* in season *s*:

```
gap_i        ~ StudentT(nu, skill[a, s] - skill[b, s], sigma_obs)
skill[d, s]  = skill[d, previous season] + sigma_walk * sqrt(years apart) * z
skill[d, first season] ~ Normal(0, sigma_driver)
```

- The **random walk** lets form change over a career (Alonso's skill rises to a 2010 peak and
  declines gently with age) and makes recent seasons count most for current estimates,
  while older seasons still stabilise them.
- **Rookies** start from the population average with wide uncertainty and move as data
  arrives (hierarchical shrinkage).
- The **Student-t** likelihood means a crash or a failure in one session can't drag an
  estimate.
- Implementation: non-centred parameterisation, career walk as one segmented cumulative sum;
  sampled with nutpie (4 chains). Fits take 15–30 s; all fits have 0 divergences and
  r-hat ≤ 1.013.

**Error rate** uses a count version of the same idea: `errors ~ Poisson(starts × rate)`,
`log rate = season level + career random walk`. The season level absorbs what changes for
everyone (safer cars and circuits, how completely a season's causes are recorded).

**Why PyMC rather than ridge regression?** Both put drivers on one scale. The Bayesian model
gives proper per-driver uncertainty, which the simulator needs, handles drivers with little
data through the prior instead of ad-hoc penalties, and models change over a career
directly. The cost is more complex code and slower fitting, but at 15–30 s per fit that cost
is small.

### Validation

- **Synthetic recovery:** on simulated teammate data with known true skills, the model ranks
  drivers with correlation 0.99; the rate model separates careful / average / crash-prone
  drivers and keeps a 3-race rookie near the field average (unit tests).
- **Known cases** (automated checks): Verstappen has a top-3 race pace in 2023; Alonso,
  Hamilton and Verstappen come out better than average in the wet; Alonso makes fewer
  errors than the median driver; rookies are more uncertain than veterans.
- **Face validity:** Pérez is stronger in races than in qualifying; Alonso, Albon and
  Hülkenberg are the gentlest on tyres; Magnussen, Leclerc and Alonso are the best
  starters, Bottas and Norris the weakest. Qualifying and race pace correlate at 0.79:
  related, but distinct skills.

## Inferences validated against known truth

| What is missing | Inference | Validation (seasons with the truth) |
|---|---|---|
| Retirement causes 2023+ (source records only "Retired") | Driver error if a race-control incident message names the car within -1..+3 laps, it retired on lap 1, or a safety car / red flag followed within 2 laps | 2018–22: precision **0.79**, recall **0.76** |
| Safety cars / red flags before 2018 | Runs of laps where the whole field is >10% slow; a lap >2.5× normal = red flag | 2018+: any neutralisation **96%** agreement, starting on lap 1–2 **95%**; red flags 17/20 |
| Wet races before 2018 | Broad: race laps ≥14% slower than pole **or** ≥0.8 mm rain in the first two hours (F1 0.55). Strict (for wet skill): laps ≥14% slower than pole | 2018+ (truth = wet tyres on ≥10% of laps): strict rule 6/6 correct, 0 false alarms; hourly rain alone F1 0.52 |

## Circuit and car features

- **Event rates** (safety car, VSC, red flag, lap-1 incident): per circuit, recency-weighted
  (×0.85 per year back) and shrunk toward the all-circuit rate with 6 pseudo-races; beta
  posterior s.d. reported.
- **Pit-stop loss:** in-lap + out-lap minus two of the driver's typical laps, green-flag stops
  2018+, median per circuit.
- **Overtaking difficulty:** on-track passes per green lap, net of places handed over by cars
  that pitted or retired (which would otherwise look like overtakes), as quintiles 1–5.
- **Car profiles** (as of every round): qualifying "supertime" and race-pace gap to the best
  team with a 5-race trend; strength at high/medium/low-speed tracks relative to the team's
  own average; speed-trap p90 vs field; tyre wear vs field; mechanical-failure rate shrunk
  toward the season's field rate with 20 pseudo-starts; pit stationary time (2023+).
- **Corner speeds** (telemetry, 2018+): on each driver's fastest qualifying lap, distance is
  integrated from speed; apexes are speed minima ≥15 km/h below the preceding 4 s; corners
  are apex clusters found by at least half the field; corners are classed low (<130 km/h),
  medium (<210) or high by the field's median apex speed; each team's best apex speed is
  compared with the field.

## Joint field model: car form vs driver, and driver ability at each track

A result at a track only tells you about the driver once you know how good the car was **at
that point of the season**. Cars change through a year: in 2026 the Red Bull started slow
and was upgraded, so early-season results say more about the car than about Verstappen. The
teammate models above cancel the car out but use only two drivers per comparison and ignore
how fast the car itself was. The joint field model (`features/field_model.py`, 2010+, fitted
separately for qualifying and for race pace) uses the whole field every weekend.

One observation is one driver's pace in one dry session, relative to the session median
(qualifying: best lap; race: median clean lap, ≥10 laps), table `gold.driver_session_pace`.
It is split into:

| Part | Meaning | Prior / structure |
|---|---|---|
| session | how the field ran that day | Normal(0, 0.5) |
| **car[team, round]** | the car's pace *at that round* | random walk through each season, so upgrades show up as steps; the spread per round is learned (qualifying ≈0.17%, race ≈0.10%) |
| car × track type | this car on high/medium/low/mixed-speed tracks, per season | Normal(0, learned σ) |
| skill[driver, season] | the driver | career random walk |
| driver × track type | e.g. a driver who is strong on street-like slow tracks | Student-t(3), learned σ |
| **driver × circuit** | e.g. Pérez in Baku, Alonso in Singapore | Student-t(3), learned σ |

Teammates share the car terms in every session, so a driver's numbers are always net of the
car he had that weekend. Drivers who change teams tie the car scales together.

**Shrinkage.** Every bonus starts at zero and has a learned spread. A track bonus grows only
when a driver beats what his car *and* his general level predict at that circuit again and
again, over seasons and against different teammates. The heavy-tailed prior keeps most
bonuses near zero without flattening a genuine specialist. On synthetic data (a known 1.0%
mid-season upgrade, one driver 0.4% faster at one circuit) the model recovers both.
Number of visits is stored with every track bonus (`n_samples`).

**What came out (cutoff 2026-11-19):** Verstappen is the best qualifier and racer net of the
car; Mercedes was the fastest 2026 car, Red Bull improved from −0.26% (round 1) to −0.76%
(round 8) in qualifying. Largest qualifying track bonuses: Pérez/Baku, Bottas/Hungary,
Albon/Monaco, Alonso/Singapore; race: Hamilton/Barcelona, Vettel/Singapore,
Räikkönen/Hungary.

**In the simulator** (`sim/field_inputs.py`): car pace = the car at its latest round (+ one
round of upgrade uncertainty; a new season starts from last year's final car ± 0.6%), plus
its car × track-type strength; driver pace = skill + track-type bonus + this-circuit bonus.
Weekend form left over on top: 0.28% (qualifying) / 0.22% (race), from the shared deviation
of teammates (0.34% / 0.28% before removing upgrades and track type).

Stored per cutoff in `gold.field_effects` (one row per effect, `effect = hyper:<name>` rows
hold the learned spreads). A fit takes ~30 s per kind.

## Tyre models (2019+)

- Per stint (≥8 clean laps), lap time is fuel-corrected (`100·ln t + 0.035·lap`) and
  regressed on tyre age; the slope is the wear rate.
- **Temperature:** pooled over all stints, Medium and Hard wear ~0.005% more per lap for
  every +10 °C of track temperature.
- **Per circuit:** a circuit's wear level is measured from all compounds pooled, then
  multiplied by the global compound wear (Soft 0.061, Medium 0.045, Hard 0.037 %/lap at
  35 °C). Per-compound measurements at a single circuit were too noisy: at 10 of 27
  circuits Medium seemed to wear more slowly than Hard. They are kept as
  `raw_wear_pct_per_lap_35c` for reference.
- **Fresh-tyre pace** between compounds comes from teammates on the same lap on different
  compounds, adjusted for tyre age. Comparing stint intercepts instead made fresh Hards look
  faster, because Hards usually run late, on a faster, rubbered-in track.
- **Stint lengths:** typical (median) and p90 (a proxy for the "cliff": teams pit before it).

## Known limitations

- **Fuel effect is assumed** at 0.035% per lap. Within a stint, tyre age and fuel burn both
  increase by one per lap, so the data alone can't separate them; wear values inherit this
  assumption.
- **Lusail** comes out as the lowest-wear circuit, which is almost certainly wrong. Its 2023
  race had a mandatory 18-lap stint limit, and those forced short stints probably distort the
  measurement.
- **Wet skill is weakly identified:** about 300 wet comparisons in total, so most drivers'
  uncertainty (±0.2%) is as large as their estimate. Races where qualifying was also wet
  (e.g. Monza 2008) can't be confidently detected before 2018.
- **Qualifying 2001–2002** is mostly missing at the source.
- **Corner detection** finds braking corners only; flat-out kinks have no speed minimum.
  Fast sweepers are covered by the straight-line and high-speed-track measures instead.
- **Track status per lap** for 2023+ is reconstructed from race-control messages, not
  recorded directly.
