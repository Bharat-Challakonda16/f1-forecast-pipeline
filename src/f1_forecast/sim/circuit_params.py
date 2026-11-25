"""Circuit parameters rebuilt from per-race tables using only races before a cutoff.

The gold tables ``track_event_rates``, ``circuit_measures`` and ``tyre_models`` use every
race, which is right for the app but would leak the future into a backtest (predicting 2026
Monaco with safety-car rates that already include 2026 Monaco). These functions apply the
same formulas to the per-race tables filtered to ``race_date < as_of``.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

RECENCY = 0.85  # weight per season back
EVENT_PSEUDO_RACES = 6
TYRE_TYPE_PSEUDO_STINTS = 15
OFFSET_PSEUDO_RACES = 5
COMPOUNDS = ["SOFT", "MEDIUM", "HARD"]


def event_rates(neutralisations: pd.DataFrame, circuit_id: str, ref_season: int) -> dict:
    """Probabilities per race of SC / VSC / red flag / lap-1 incident at a circuit,
    recency-weighted and shrunk toward the all-circuit rate (as gold.track_event_rates)."""
    n = neutralisations.copy()
    n["wt"] = RECENCY ** (ref_season - n["season"].astype(int))
    out = {}
    for name, col in [
        ("sc_prob", "had_sc"),
        ("vsc_prob", "had_vsc"),
        ("red_flag_prob", "had_red"),
        ("lap1_incident_prob", "had_lap1_incident"),
    ]:
        known = n[n[col].notna()]
        flag = known[col].astype(float)
        glob = (known["wt"] * flag).sum() / known["wt"].sum() if len(known) else 0.0
        here = known["circuit_id"] == circuit_id
        w_here, k_here = known.loc[here, "wt"].sum(), (known.loc[here, "wt"] * flag[here]).sum()
        out[name] = float((k_here + EVENT_PSEUDO_RACES * glob) / (w_here + EVENT_PSEUDO_RACES))
    out["races_at_circuit"] = int((n["circuit_id"] == circuit_id).sum())
    return out


def overtaking(race_overtakes: pd.DataFrame, circuit_id: str) -> tuple[float | None, int | None]:
    """(passes per green lap at the circuit, difficulty quintile 1-5 among circuits)."""
    per = race_overtakes.groupby("circuit_id")[["passes", "green_laps"]].sum()
    if per.empty or circuit_id not in per.index:
        return None, None
    rate = per["passes"] / per["green_laps"]
    quintile = pd.qcut(rate.rank(method="first"), 5, labels=False) + 1  # 1 = fewest passes
    return float(rate[circuit_id]), int(6 - quintile[circuit_id])


def pit_loss(green_pit_stops: pd.DataFrame, circuit_id: str, default: float = 22.0) -> float:
    s = green_pit_stops[(green_pit_stops["circuit_id"] == circuit_id)]["loss_s"].astype(float)
    s = s[(s >= 5) & (s <= 60)]
    return float(s.median()) if len(s) else default


def tyre_params(
    stints: pd.DataFrame,
    offsets: pd.DataFrame,
    circuit_id: str,
    speed_profile: str | None,
    ref_season: int,
) -> pd.DataFrame:
    """Per compound at a circuit, as gold.tyre_models: wear = circuit wear level x pooled
    compound wear (35 C reference), temperature sensitivity, fresh pace vs Medium, stint
    lengths, typical lap time."""
    s = stints[stints["wear_pct_per_lap"].between(-0.5, 1.0)].copy()
    s["track_temp_c"] = s["track_temp_c"].astype(float)
    sens = {}
    for c in COMPOUNDS:
        sc = s[(s["compound"] == c) & s["track_temp_c"].notna()]
        sens[c] = (
            10 * float(np.polyfit(sc["track_temp_c"], sc["wear_pct_per_lap"], 1)[0])
            if len(sc) > 20
            else 0.0
        )
    s["wear_at_35c"] = (
        s["wear_pct_per_lap"] - s["compound"].map(sens) * (s["track_temp_c"].fillna(35) - 35) / 10
    )
    s["wt"] = RECENCY ** (ref_season - s["season"].astype(int))
    s["w_wear"] = s["wt"] * s["wear_at_35c"]
    sums = s.groupby("compound")[["w_wear", "wt"]].sum()
    cw = sums["w_wear"] / sums["wt"]
    s["rel"] = s["wear_at_35c"] / s["compound"].map(cw)
    s["w_rel"] = s["wt"] * s["rel"]
    by_type = s.groupby(s["speed_profile"].fillna("?"))[["w_rel", "wt"]].sum()
    type_level = by_type["w_rel"] / by_type["wt"]
    here = s[s["circuit_id"] == circuit_id]
    prior = float(type_level.get(speed_profile or "?", (s["wt"] * s["rel"]).sum() / s["wt"].sum()))
    level = ((here["wt"] * here["rel"]).sum() + TYRE_TYPE_PSEUDO_STINTS * prior) / (
        here["wt"].sum() + TYRE_TYPE_PSEUDO_STINTS
    )

    g_off = offsets.groupby("compound")["fresh_vs_medium_pct"].median()
    o_here = offsets[offsets["circuit_id"] == circuit_id].groupby("compound")["fresh_vs_medium_pct"]
    rows = []
    for c in COMPOUNDS:
        hc = here[here["compound"] == c]
        allc = s[s["compound"] == c]
        if c == "MEDIUM":
            off = 0.0
        else:
            n_here = int(o_here.size().get(c, 0))
            med_here = float(o_here.median().get(c, 0.0)) if n_here else 0.0
            off = (n_here * med_here + OFFSET_PSEUDO_RACES * float(g_off.get(c, 0.0))) / (
                n_here + OFFSET_PSEUDO_RACES
            )
        latest = here[here["season"] == here["season"].max()] if len(here) else here
        lap_ms = (
            float(latest["median_lap_ms"].median())
            if len(latest)
            else float(s["median_lap_ms"].median())
        )
        rows.append(
            {
                "compound": c,
                "wear_pct_per_lap_35c": float(level * cw.get(c, 0.04)),
                "wear_ms_per_lap_35c": float(level * cw.get(c, 0.04)) / 100 * lap_ms,
                "wear_pct_per_lap_per_10c": sens[c],
                "fresh_pace_vs_medium_pct": off,
                "typical_stint_laps": float((hc if len(hc) >= 5 else allc)["stint_laps"].median()),
                "max_stint_laps": float((hc if len(hc) >= 5 else allc)["stint_laps"].quantile(0.9)),
                "typical_lap_ms": lap_ms,
                "circuit_wear_level": float(level),
                "stints_at_circuit": len(hc),
            }
        )
    return pd.DataFrame(rows)
