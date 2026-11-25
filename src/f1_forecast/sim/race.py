"""Vectorised Monte Carlo race simulator: every simulated race advances one lap at a time,
all simulations at once (arrays of shape ``(sims, drivers)``).

Per lap, for every car still running::

    lap_ms = base_lap_ms * (1 + pace% / 100)
    pace%  = car race pace + team form on the day + driver race skill        (per race)
           + tyre: fresh offset of the compound + (wear + driver wear) * age  (per lap)
           + noise ~ N(0, LAP_NOISE * driver consistency factor)              (per lap)
           + pit-stop loss on in-laps, safety-car / VSC slow laps

Racing: cars are taken in running order. A car that would finish the lap less than
``MIN_GAP_S`` behind the car ahead must pass it, with probability
``1 - exp(-k * (advantage_s - PASS_THRESHOLD_S))``, where ``k`` grows with how easy
overtaking is at the circuit (measured passes per green lap); if it fails, the car is held
behind. Following within 1 s costs time (dirty air). Cars that pit don't take part: a car
losing 20 s in the pit lane isn't "overtaken".

Events: lap-1 start (grid order shaken by each driver's start skill), first-lap incidents,
safety cars (field bunched, cheap pit stops), VSCs (slow laps, cheaper stops), red flags
(bunched, free tyre change), mechanical failures (team reliability) and driver-error crashes
(driver error rate). All rates come from the gold layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from f1_forecast.sim.quali import track_type_adjustment
from f1_forecast.sim.strategy import COMPOUNDS, candidate_plans, plan_probabilities

POINTS = np.array([25, 18, 15, 12, 10, 8, 6, 4, 2, 1], float)
LAP_NOISE_PCT = 0.30  # clean-lap variation for an average-consistency driver
MIN_GAP_S = 0.25  # closest a car can follow at the line without passing
START_NOISE_PLACES = 1.6  # spread of lap-1 place changes
SC_LAP_FACTOR = 1.40
VSC_LAP_FACTOR = 1.30
SC_PIT_FACTOR = 0.45  # a stop under a safety car costs ~45% of a green stop
VSC_PIT_FACTOR = 0.65
# Pass-probability slope k = PASS_EASE * (measured passes per green lap) ** PASS_POWER,
# calibrated so simulated passes match the measured rate at 7 circuits (2026 cars):
# Monaco 0.06/0.05, Silverstone 0.58/0.56, Lusail 0.53/0.48, Hungary 0.51/0.40,
# Monza 0.59/0.71; Spa (1.16/0.82) and Las Vegas (0.90/1.50) are off - slipstream on
# very long straights is not modelled.
PASS_EASE = 3.0
PASS_POWER = 1.5
PASS_THRESHOLD_S = 0.20  # pace advantage (s/lap) needed before a pass is possible at all
DIRTY_AIR_PCT = 0.25  # time lost per lap when following within DIRTY_AIR_GAP_S
DIRTY_AIR_GAP_S = 1.0


@dataclass
class RaceResult:
    driver_ids: list[str]
    team_ids: list[str]
    position: np.ndarray  # (sims, drivers) finishing position; DNF ranked after finishers
    dnf: np.ndarray  # (sims, drivers) bool
    dnf_cause: np.ndarray  # (sims, drivers) 0 none, 1 mechanical, 2 driver error
    stops: np.ndarray  # (sims, drivers)
    strategy: np.ndarray  # (sims, drivers) plan label strings
    sc_laps: np.ndarray  # (sims,) number of safety-car laps
    passes_per_green_lap: float = 0.0  # simulated on-track passes, for calibration
    replays: list[dict] = field(default_factory=list)

    def summary(self) -> pd.DataFrame:
        pos, dnf = self.position, self.dnf
        pts = np.where(~dnf & (pos <= 10), POINTS[np.clip(pos - 1, 0, 9)], 0.0)
        out = pd.DataFrame(
            {
                "driver_id": self.driver_ids,
                "constructor_id": self.team_ids,
                "p_win": ((pos == 1) & ~dnf).mean(axis=0),
                "p_podium": ((pos <= 3) & ~dnf).mean(axis=0),
                "p_points": ((pos <= 10) & ~dnf).mean(axis=0),
                "p_dnf": dnf.mean(axis=0),
                "expected_points": pts.mean(axis=0),
            }
        )
        out["expected_position"] = np.nanmean(np.where(dnf, np.nan, pos.astype(float)), axis=0)
        return out.sort_values("expected_points", ascending=False).reset_index(drop=True)

    def strategy_summary(self) -> pd.DataFrame:
        rows = []
        for j, (d, t) in enumerate(zip(self.driver_ids, self.team_ids, strict=True)):
            labels, counts = np.unique(self.strategy[:, j], return_counts=True)
            for lab, c in zip(labels, counts, strict=True):
                rows.append((t, d, lab, c / len(self.strategy)))
        return pd.DataFrame(rows, columns=["constructor_id", "driver_id", "strategy", "share"])


def simulate_race(
    drivers: pd.DataFrame,
    teams: pd.DataFrame,
    tyres: pd.DataFrame,
    circuit: dict,
    grid: np.ndarray,
    *,
    seed: int = 2027,
    n_replays: int = 3,
    pass_ease: float = PASS_EASE,
    pass_power: float = PASS_POWER,
    pass_k: float | None = None,
    pass_threshold_s: float = PASS_THRESHOLD_S,
    dirty_air_pct: float = DIRTY_AIR_PCT,
    car_z: np.ndarray | None = None,
    skill_z: np.ndarray | None = None,
    sprint: bool = False,
) -> RaceResult:
    """``grid``: (sims, drivers) starting positions, e.g. ``QualiResult.grid``.

    ``car_z`` / ``skill_z``: optional shared standard-normal draws for estimate uncertainty
    (see :func:`simulate_qualifying`). ``sprint``: short race, no compulsory stop."""
    rng = np.random.default_rng(seed)
    sims, n = grid.shape
    laps = int(circuit["laps"])
    base_ms = float(circuit["base_lap_ms"])
    pit_loss_ms = float(circuit["pit_loss_s"]) * 1000

    t = teams.reset_index(drop=True)
    team_idx = {c: i for i, c in enumerate(t["constructor_id"])}
    k = drivers["constructor_id"].map(team_idx).to_numpy().astype(int)

    # ------------------------------------------------------------------ per-race draws
    adj = track_type_adjustment(t, circuit.get("speed_profile")).to_numpy()
    if car_z is None:
        car_z = rng.standard_normal((sims, len(t)))
    if skill_z is None:
        skill_z = rng.standard_normal((sims, n))
    car = t["car_race"].to_numpy(float) + t["car_race_se"].to_numpy(float) * car_z
    form = rng.normal(0.0, t["form_sd"].to_numpy(float), (sims, len(t)))
    skill = drivers["race_pace"].to_numpy(float) + drivers["race_pace_se"].to_numpy(float) * skill_z
    pace = (car + form + adj)[:, k] + skill  # % lap, lower = faster
    noise_sd = LAP_NOISE_PCT * np.exp(drivers["consistency"].fillna(0).to_numpy(float) / 100)
    driver_wear = drivers["tyre_wear"].fillna(0).to_numpy(float) / 10  # % per lap of age

    # ------------------------------------------------------------------ strategies
    plans = candidate_plans(
        tyres,
        laps=laps,
        base_lap_ms=base_ms,
        pit_loss_s=circuit["pit_loss_s"],
        track_temp_c=circuit.get("track_temp_c") or 35.0,
        sprint=sprint,
    )
    probs = plan_probabilities(plans)
    choice = rng.choice(len(plans), size=(sims, n), p=probs)
    max_stops = max(p.stops for p in plans)
    pit_lap = np.full((sims, n, max(max_stops, 1)), laps + 99)  # keep a slot even if no stops
    seq = np.zeros((sims, n, max_stops + 1), int)
    for i, p in enumerate(plans):
        m = choice == i
        for s_i, lap_i in enumerate(p.pit_laps):
            jitter = rng.integers(-3, 4, size=m.sum())
            pit_lap[m, s_i] = np.clip(lap_i + jitter, 2, laps - 2)
        for c_i, comp in enumerate(p.compounds):
            seq[m, c_i] = comp
    pit_lap.sort(axis=2)
    labels = np.array([p.label() for p in plans])[choice]
    tt = tyres.set_index("compound").reindex(list(COMPOUNDS))
    offset = tt["fresh_pace_vs_medium_pct"].fillna(0).to_numpy(float)
    wear = (
        tt["wear_pct_per_lap_35c"].to_numpy(float)
        + tt["wear_pct_per_lap_per_10c"].fillna(0).to_numpy(float)
        * ((circuit.get("track_temp_c") or 35.0) - 35.0)
        / 10
    ).clip(min=0.002)

    # ------------------------------------------------------------------ events
    def event_laps(prob: float | None, min_len: int, max_len: int) -> np.ndarray:
        """(sims, laps+1) bool: laps under this neutralisation."""
        on = np.zeros((sims, laps + 1), bool)
        happens = rng.random(sims) < (prob or 0.0)
        start = rng.integers(2, max(3, laps - 2), sims)
        length = rng.integers(min_len, max_len + 1, sims)
        for s in np.flatnonzero(happens):
            on[s, start[s] : min(laps, start[s] + length[s])] = True
        return on

    sc = event_laps(circuit.get("sc_prob"), 3, 6)
    vsc = event_laps(circuit.get("vsc_prob"), 1, 3) & ~sc
    red = np.zeros((sims, laps + 1), bool)
    red_happens = rng.random(sims) < (circuit.get("red_flag_prob") or 0.0)
    red_lap = rng.integers(2, max(3, laps - 3), sims)
    red[np.flatnonzero(red_happens), red_lap[red_happens]] = True

    # retirements: lap of failure, if any (uniform over the race)
    rel = t["reliability_dnf_rate"].to_numpy(float)[k]
    err = drivers["error_rate"].fillna(0.07).to_numpy(float)
    mech_lap = np.where(
        rng.random((sims, n)) < rel, rng.integers(1, laps + 1, (sims, n)), laps + 99
    )
    crash_lap = np.where(
        rng.random((sims, n)) < err, rng.integers(1, laps + 1, (sims, n)), laps + 99
    )
    # first-lap incident: one or two cars, more likely from the midfield back
    lap1 = rng.random(sims) < (circuit.get("lap1_incident_prob") or 0.0)
    weight = grid / grid.sum(axis=1, keepdims=True)
    for s in np.flatnonzero(lap1):
        victims = rng.choice(n, size=rng.integers(1, 3), replace=False, p=weight[s])
        out = victims[rng.random(len(victims)) < 0.4]  # some retire, others just lose time
        crash_lap[s, out] = np.minimum(crash_lap[s, out], 1)
    dnf_lap = np.minimum(mech_lap, crash_lap)
    dnf_cause = np.where(dnf_lap > laps, 0, np.where(mech_lap <= crash_lap, 1, 2))

    # ------------------------------------------------------------------ lap 1
    start_skill = drivers["starts"].fillna(0).to_numpy(float)  # places lost on lap 1
    order_score = grid + start_skill + rng.normal(0, START_NOISE_PLACES, (sims, n))
    order = order_score.argsort(axis=1)
    cum = np.empty((sims, n))
    rows = np.arange(sims)[:, None]
    cum[rows, order] = base_ms * 1.05 + np.arange(n) * 450.0  # lap 1: field ~0.45 s apart
    lost_time = lap1[:, None] & (crash_lap > 1) & (rng.random((sims, n)) < 0.08)
    cum += lost_time * rng.uniform(5_000, 25_000, (sims, n))

    alive = dnf_lap > 1
    stint = np.zeros((sims, n), int)
    age = np.ones((sims, n))
    stops = np.zeros((sims, n), int)
    replays = [{"positions": [], "sc": []} for _ in range(min(n_replays, sims))]

    k_pass = (
        pass_k
        if pass_k is not None
        else pass_ease * float(circuit.get("passes_per_green_lap") or 0.5) ** pass_power
    )
    n_passes, n_green = 0, 0

    # ------------------------------------------------------------------ laps 2..N
    for lap in range(2, laps + 1):
        alive &= dnf_lap > lap
        compound = np.take_along_axis(seq, stint[:, :, None], axis=2)[:, :, 0]
        tyre = offset[compound] + (wear[compound] + driver_wear) * age
        lap_pct = pace + tyre + rng.normal(0, 1, (sims, n)) * noise_sd
        lap_ms = base_ms * (1 + lap_pct / 100)

        sc_now, vsc_now, red_now = sc[:, lap], vsc[:, lap], red[:, lap]
        # pit stops: planned this lap, or early under SC/VSC/red if a stop is due soon
        due = np.take_along_axis(
            pit_lap, np.minimum(stint, pit_lap.shape[2] - 1)[:, :, None], axis=2
        )[:, :, 0]
        has_stop = stint < max_stops
        planned = has_stop & (due == lap)
        opportunistic = (
            has_stop & (due - lap <= 8) & (due > lap) & (sc_now | vsc_now | red_now)[:, None]
        )
        pitting = alive & (planned | opportunistic)
        loss = np.where(
            red_now[:, None],
            0.0,
            np.where(
                sc_now[:, None], SC_PIT_FACTOR, np.where(vsc_now[:, None], VSC_PIT_FACTOR, 1.0)
            ),
        )
        lap_ms += pitting * (pit_loss_ms * loss + rng.normal(0, 600, (sims, n)))
        stint = np.where(pitting, np.minimum(stint + 1, max_stops), stint)
        age = np.where(pitting, 0, age) + 1
        stops += pitting

        neutral = sc_now | vsc_now
        lap_ms = np.where(
            sc_now[:, None], base_ms * SC_LAP_FACTOR + (lap_ms - base_ms) * 0.2, lap_ms
        )
        lap_ms = np.where(vsc_now[:, None], lap_ms * VSC_LAP_FACTOR, lap_ms)

        # dirty air: following closely at the start of the lap costs time
        start_order = np.where(alive, cum, np.inf).argsort(axis=1)
        cum_o0 = np.take_along_axis(cum, start_order, axis=1)
        gap_o = np.diff(cum_o0, axis=1, prepend=-np.inf)
        gap_start = np.empty_like(cum)
        np.put_along_axis(gap_start, start_order, gap_o, axis=1)
        following = (gap_start < DIRTY_AIR_GAP_S * 1000) & ~pitting
        lap_ms = lap_ms + following * base_ms * dirty_air_pct / 100 * ~(sc_now | vsc_now)[:, None]

        # racing in running order (held up unless a pass works)
        proposed = cum + lap_ms
        run_order = np.where(alive, cum, np.inf).argsort(axis=1)
        prop_o = np.take_along_axis(proposed, run_order, axis=1)
        lap_o = np.take_along_axis(lap_ms, run_order, axis=1)
        alive_o = np.take_along_axis(alive, run_order, axis=1)
        pit_o = np.take_along_axis(pitting, run_order, axis=1)
        green = ~neutral
        n_green += int(green.sum())
        for p in range(1, n):
            ahead, me = prop_o[:, p - 1], prop_o[:, p]
            close = (
                alive_o[:, p]
                & alive_o[:, p - 1]
                & green
                & ~pit_o[:, p]
                & ~pit_o[:, p - 1]
                & (me < ahead + MIN_GAP_S * 1000)
            )
            if not close.any():
                continue
            advantage_s = np.clip(
                (lap_o[:, p - 1] - lap_o[:, p]) / 1000 - pass_threshold_s, 0, None
            )
            passes = rng.random(sims) < 1 - np.exp(-k_pass * advantage_s)
            held = close & ~passes
            n_passes += int((close & passes).sum())
            prop_o[held, p] = ahead[held] + MIN_GAP_S * 1000
            # after a pass the overtaken car is behind: keep order consistent
            swap = close & passes & (prop_o[:, p] > prop_o[:, p - 1])
            prop_o[swap, p] = prop_o[swap, p - 1] - 50
        new_cum = np.empty_like(cum)
        np.put_along_axis(new_cum, run_order, prop_o, axis=1)
        cum = np.where(alive, new_cum, cum)

        # safety car / red flag: bunch the field behind the leader
        bunch = sc_now & ~sc[:, lap - 1] | red_now
        if bunch.any():
            b = np.flatnonzero(bunch)
            o = np.where(alive[b], cum[b], np.inf).argsort(axis=1)
            leader = np.take_along_axis(cum[b], o[:, :1], axis=1)
            spaced = leader + np.arange(n)[None, :] * 800.0
            bunched = np.empty((len(b), n))
            np.put_along_axis(bunched, o, spaced, axis=1)
            cum[b] = np.where(alive[b], bunched, cum[b])

        for r, rep in enumerate(replays):
            rep["positions"].append(np.where(alive[r], cum[r], np.inf).argsort().argsort().tolist())
            rep["sc"].append(bool(sc_now[r] or vsc_now[r]))

    # ------------------------------------------------------------------ classification
    finished = dnf_lap > laps
    # finishers by race time, then retirements by how far they got
    key = np.where(finished, cum, 1e12 + (laps - dnf_lap) * 1e6)
    position = key.argsort(axis=1).argsort(axis=1) + 1
    return RaceResult(
        driver_ids=drivers["driver_id"].tolist(),
        team_ids=drivers["constructor_id"].tolist(),
        position=position,
        dnf=~finished,
        dnf_cause=dnf_cause,
        stops=stops,
        strategy=labels,
        sc_laps=sc.sum(axis=1),
        passes_per_green_lap=n_passes / max(n_green, 1),
        replays=[{**r, "driver_ids": drivers["driver_id"].tolist()} for r in replays],
    )


@dataclass(frozen=True)
class PassCalibration:
    k: float  # pass-probability slope
    slipstream: float  # 0 = normal track; 1 = strongest tow (see calibrate_pass_k)
    threshold_s: float  # pace advantage needed to pass (negative: the tow lets slower cars by)
    dirty_air_pct: float  # time lost following closely (negative: the tow pulls cars closer)
    achieved: float  # simulated passes per green lap
    target: float  # measured passes per green lap

    def race_kwargs(self) -> dict:
        return {
            "pass_k": self.k,
            "pass_threshold_s": self.threshold_s,
            "dirty_air_pct": self.dirty_air_pct,
        }


def slipstream_settings(strength: float) -> tuple[float, float]:
    """Slipstream strength 0..1 -> (pass threshold s/lap, dirty-air % lap)."""
    return PASS_THRESHOLD_S - 0.8 * strength, DIRTY_AIR_PCT - 0.6 * strength


def calibrate_pass_k(
    drivers: pd.DataFrame,
    teams: pd.DataFrame,
    tyres: pd.DataFrame,
    circuit: dict,
    grid: np.ndarray,
    *,
    target: float | None = None,
    sims: int = 1000,
    iterations: int = 8,
    seed: int = 11,
) -> PassCalibration:
    """Calibrate overtaking for this circuit so the simulator reproduces the measured
    on-track passes per green lap.

    1. Bisect the pass-probability slope ``k`` (log scale, fixed seed).
    2. If even near-certain passes fall short, the track's slipstream must be doing more
       than our dirty-air model allows (e.g. the Las Vegas Strip): keep ``k`` high and
       raise a single slipstream strength that lowers the pass threshold (slower cars can
       be passed... by faster-on-the-straight cars) and turns dirty-air loss into a tow.

    Everything that makes a track special is already in the measured rate, so matching it
    per circuit captures it without modelling each effect separately.
    """
    target = float(target if target is not None else circuit.get("passes_per_green_lap") or 0.5)
    g = grid[:sims]

    def rate(k: float, strength: float = 0.0) -> float:
        threshold, dirty = slipstream_settings(strength)
        r = simulate_race(
            drivers,
            teams,
            tyres,
            circuit,
            g,
            seed=seed,
            n_replays=0,
            pass_k=k,
            pass_threshold_s=threshold,
            dirty_air_pct=dirty,
        )
        return r.passes_per_green_lap

    k_max = 80.0
    lo, hi = np.log(0.02), np.log(k_max)
    for _ in range(iterations):
        mid = (lo + hi) / 2
        if rate(float(np.exp(mid))) < target:
            lo = mid
        else:
            hi = mid
    k = float(np.exp((lo + hi) / 2))
    strength = 0.0
    achieved = rate(k)
    if achieved < 0.95 * target:  # slope saturated: the slipstream has to do the rest
        k = k_max
        s_lo, s_hi = 0.0, 1.0
        for _ in range(iterations):
            mid = (s_lo + s_hi) / 2
            if rate(k, mid) < target:
                s_lo = mid
            else:
                s_hi = mid
        strength = (s_lo + s_hi) / 2
        achieved = rate(k, strength)
    threshold, dirty = slipstream_settings(strength)
    return PassCalibration(k, strength, threshold, dirty, achieved, target)
