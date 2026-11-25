# Backtest report (Phase 3)

How well the simulator predicts real races, and how that compares with simple rules anyone
could apply. Model version **`sim_v6`** (field model `field_rw_v2`). Numbers are stored in
`gold.backtest_results` (per-season averages), `gold.backtest_events` (per race),
`gold.backtest_calibration` and `gold.predictions`.

## Summary

- **Before the weekend**, the model's probabilities beat every simple baseline on the
  held-out 2026 season: winner log loss **1.65** vs 1.87 (championship order), win Brier
  0.771 vs 0.835, podium Brier 0.571 vs 0.635, and more podium finishers picked (54% vs 46%).
- **After qualifying**, it matches "the grid order" on picking the winner (71%) and gives
  better-calibrated chances: winner log loss **1.10** vs 1.27, podium Brier 0.473 vs 0.488,
  finishing-order correlation 0.69 vs 0.64.
- **Qualifying**: best finishing-order correlation (0.835 vs 0.824) and pole Brier (0.833 vs
  0.859). Pole log loss ties the championship order (2.04 each).
- **Weak spot:** the model's *single favourite* is right less often than the simplest rule.
  Before the weekend its favourite won 5 of 21 races, against 7 for the championship leader.
  Its favourite took pole 5 times, against 6. With 21 races this is a gap of 1–2 events.

## Method

**No look-ahead.** For each completed race, the cutoff is race day minus 3 days, before the
weekend starts. Every model input is fitted on data before the cutoff only: driver traits,
the joint car/driver/track model, circuit rates and tyre models. That means one fit per
cutoff, stored with its `data_cutoff`.

**Three predictions per race**, each from 10,000 simulations:

| Stage | Known | Predicts |
|---|---|---|
| `qualifying` | nothing from the weekend | pole, top 3, grid order |
| `pre_weekend` | nothing from the weekend; the grid is simulated | win, podium, points |
| `post_quali` | the actual starting grid and qualifying times | win, podium, points |

**Baselines** turn a simple ranking into probabilities, using how often each rank produced
the outcome in the four seasons before. For example, the championship leader won x% of
races in 2022–25.

- `championship`: standings before the race.
- `previous_race`: last race's finishing order.
- `grid`: the actual starting grid (post-qualifying only).
- `previous_quali`: last qualifying order (qualifying only).

**Metrics** (per race, averaged over the season):

- **Log loss of the winner:** −ln(probability given to the driver who won). Lower is
  better, and it punishes confident misses.
- **Brier score:** squared error of the win (or podium) probabilities over all drivers.
- **Hit rates:** did the favourite win? How many of the predicted top 3 were on the podium?
- **Spearman correlation** of expected and actual finishing order.

**Tuning and testing.** Simulator settings and blend weights were chosen on **2025** only
(`backtest/tune.py`), and 2026 was then run once as the test. One caveat: versions v1–v4
were developed while looking at 2026 results. 2026 is therefore not a perfectly untouched
season for the model's structure, only for the tuned numbers.

## Results: 2026 (21 races, held out for tuning)

| Stage | Metric | **Model** | Championship | Previous race | Grid / prev. quali |
|---|---|---|---|---|---|
| Pre-weekend | Winner log loss ↓ | **1.646** | 1.873 | 2.267 | – |
| | Win Brier ↓ | **0.771** | 0.835 | 0.813 | – |
| | Podium Brier ↓ | **0.571** | 0.635 | 0.619 | – |
| | Podium hits ↑ | **54%** | 46% | 52% | – |
| | Points hits ↑ | 73% | **76%** | 70% | – |
| | Spearman ↑ | **0.657** | 0.651 | 0.481 | – |
| | Winner hit ↑ | 24% | **33%** | **33%** | – |
| Post-qualifying | Winner log loss ↓ | **1.103** | 1.873 | 2.267 | 1.266 |
| | Win Brier ↓ | 0.497 | 0.835 | 0.813 | **0.493** |
| | Podium Brier ↓ | **0.473** | 0.635 | 0.619 | 0.488 |
| | Podium hits ↑ | **60%** | 46% | 52% | **60%** |
| | Spearman ↑ | **0.686** | 0.652 | 0.482 | 0.635 |
| | Winner hit ↑ | **71%** | 33% | 33% | **71%** |
| Qualifying | Pole log loss ↓ | 2.043 | **2.037** | – | 2.463 |
| | Pole Brier ↓ | **0.833** | 0.859 | – | 0.894 |
| | Spearman ↑ | **0.835** | 0.824 | – | 0.745 |
| | Pole hit ↑ | 24% | **29%** | – | **29%** |
| | Top-3 hits ↑ | 46% | **51%** | – | 37% |

## Results: 2025 (24 races, the tuning season)

| Stage | Metric | **Model** | Championship | Previous race | Grid / prev. quali |
|---|---|---|---|---|---|
| Pre-weekend | Winner log loss ↓ | **1.807** | 2.003 | 1.818 | – |
| | Win Brier ↓ | 0.798 | 0.922 | **0.785** | – |
| | Podium hits ↑ | 61% | **67%** | 56% | – |
| | Spearman ↑ | **0.573** | 0.539 | 0.437 | – |
| | Winner hit ↑ | **33%** | 21% | **33%** | – |
| Post-qualifying | Winner log loss ↓ | **1.106** | 2.003 | 1.818 | 1.116 |
| | Podium Brier ↓ | **0.386** | 0.519 | 0.592 | 0.388 |
| | Winner hit ↑ | **67%** | 21% | 33% | **67%** |
| Qualifying | Pole log loss ↓ | **1.891** | 2.205 | – | 1.993 |
| | Spearman ↑ | **0.678** | 0.603 | – | 0.603 |
| | Pole hit ↑ | **25%** | 21% | – | 21% |

2025 was harder for the model before the weekend. There, the "previous race" baseline is
about as good on win probabilities (log loss 1.818 vs 1.807, Brier slightly better).

## How the model got here (2026, model only)

| Version | Change | Pre-weekend winner log loss | Post-quali winner log loss | Post-quali winner hit |
|---|---|---|---|---|
| sim_v1 | first full simulator | 1.750 | 1.490 | 52% |
| sim_v2 | shared estimate draws, qualifying surprise carried into the race, strategy consensus, undercut cover | 1.808 | 1.441 | 52% |
| sim_v3 | joint field model: car per round (upgrades), driver × track | 1.701 | 1.359 | 52% |
| sim_v4 | lap-1 spread growing down the grid, retirements scaled to the field rate | 1.698 | 1.306 | 57% |
| sim_v5 | race-pace spread ×0.7, blend with grid-slot history (tuned on 2025) | 1.685 | 1.098 | 71% |
| sim_v6 | young drivers' faster development, pole blend (tuned on 2025) | **1.646** | 1.103 | 71% |

## Calibration (2026, before the weekend)

When the model gives a driver a given chance of winning, how often does he win?

| Predicted | Drivers | Mean predicted | Observed |
|---|---|---|---|
| 0–10% | 380 | 1.2% | 0.5% |
| 10–20% | 40 | 14.2% | 15.0% |
| 20–30% | 34 | 24.0% | 32.4% |
| 30–40% | 8 | 31.7% | 25.0% |

Win probabilities are well calibrated. Podium probabilities follow the same pattern, with a
slight under-confidence at 40–50% (45% predicted, 59% observed). The full tables, including
post-qualifying, are in `gold.backtest_calibration`.

## What we learned

- **The starting grid carries most of the information once it's known.** From pole, a
  driver won 55% of races in 2014–26. The lap-by-lap simulation made races too open: the
  pole sitter got 25–30%. Tuning the simulator (less spread in race pace, a 0.3 link
  between qualifying and race pace) closed only part of that gap. What worked was blending:
  after qualifying the final chance is **85% grid-slot history + 15% simulation**. The
  simulation still adds something: log loss 1.105 for the blend vs 1.116 for the grid alone
  on 2025, and 1.10 vs 1.27 on 2026.
- **Before the weekend the simulation does the work.** Once young drivers' development was
  modelled, mixing in grid-slot history no longer helped, so its weight is 0.
- **Young drivers change fast; veterans don't.** On the misses, the model often backed the
  right car but the wrong driver. Antonelli outqualified Russell early in 2026 after
  trailing him by ~0.4% in 2025. The field model now learns a separate year-to-year spread
  for a driver's 2nd and 3rd season: 0.22% vs 0.10% for veterans in qualifying.
- **Form within a season barely moves.** A per-round driver-form walk was tried first. It
  learned ~0.01% per round, made no difference, and was removed.
- **Pole chances improve with simple priors.** Blending the model with last qualifying order
  (40%) and championship order (25%) lowered pole log loss on 2025 from 1.97 to 1.89.

## Limitations and next steps

- **The single favourite is right less often than simple rules.** The model spreads chances
  across the front-runners rather than backing one driver. That is good for calibration but
  costs top-pick hits. The biggest expected gain is **practice-session pace** (a new
  "after practice" stage): it shows who is fast this weekend, at this track, with this car.
- **Small samples.** 21–24 races per season; differences of one or two races in hit rates
  are within luck.
- **Dry-weather pace only** in the field model; wet races rely on the wet-skill trait.
- **2026 is not perfectly clean for the model's structure** (see Method).

## Reproduce

```powershell
# backtest a season (fits any missing traits / field model per cutoff; ~1.5 min per race)
python -m f1_forecast.backtest.race_backtest --season 2026 --sims 10000

# tuning on one season
python -m f1_forecast.backtest.tune --season 2025            # simulator settings
python -m f1_forecast.backtest.tune --season 2025 --blend    # race blend weights
python -m f1_forecast.backtest.tune --season 2025 --quali sim_v6   # pole blend weights
```

The `Backtest` GitHub Actions workflow runs the first command on demand.
