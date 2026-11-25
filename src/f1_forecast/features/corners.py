"""Corner speeds from telemetry: how fast each car is through low/medium/high-speed corners.

For one qualifying session, given every driver's fastest-lap speed trace:

1. Distance along the lap is integrated from speed (telemetry has no distance channel) and
   expressed as a fraction of the lap, so laps of different length line up.
2. A corner apex is a local speed minimum that is at least ``min_drop_kph`` below the
   fastest point in the preceding ``lookback_s`` seconds (filters out lifts and noise).
3. Apexes from all drivers are clustered by lap position; a cluster found by at least
   ``min_share`` of drivers is a corner of this track.
4. Each corner is classed by the field's median apex speed (low < 130, medium < 210 kph,
   else high), and each team's best apex speed is compared with the field median.

Output: per constructor, the mean kph above/below the field in each corner class.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

LOW_MAX_KPH = 130.0
MEDIUM_MAX_KPH = 210.0


def apexes(
    t_s: np.ndarray,
    speed: np.ndarray,
    *,
    min_drop_kph: float = 15.0,
    lookback_s: float = 4.0,
    window: int = 3,
) -> pd.DataFrame:
    """Local speed minima of one lap -> DataFrame(frac, speed_kph)."""
    order = np.argsort(t_s)
    t = np.asarray(t_s, float)[order]
    v = (
        pd.Series(np.asarray(speed, float)[order])
        .rolling(3, center=True, min_periods=1)
        .median()
        .to_numpy()
    )
    if len(t) < 20:
        return pd.DataFrame(columns=["frac", "speed_kph"])
    dist = np.concatenate([[0.0], np.cumsum(np.diff(t) * (v[1:] + v[:-1]) / 2 / 3.6)])
    if dist[-1] <= 0:  # broken trace (no movement): nothing to measure
        return pd.DataFrame(columns=["frac", "speed_kph"])
    frac = dist / dist[-1]
    rows = []
    for i in range(window, len(v) - window):
        seg = v[i - window : i + window + 1]
        if v[i] > seg.min() or v[i] == v[i - 1]:
            continue  # not the (first) lowest point of its neighbourhood
        before = v[(t >= t[i] - lookback_s) & (t < t[i])]
        if len(before) and before.max() - v[i] >= min_drop_kph:
            rows.append((frac[i], v[i]))
    return pd.DataFrame(rows, columns=["frac", "speed_kph"])


def track_corners(
    apex_by_driver: dict[str, pd.DataFrame], *, max_gap: float = 0.015, min_share: float = 0.5
) -> np.ndarray:
    """Cluster all drivers' apex positions; return the lap fraction of each real corner."""
    pos = np.sort(np.concatenate([a["frac"].to_numpy() for a in apex_by_driver.values()] or [[]]))
    if len(pos) == 0:
        return np.array([])
    clusters, current = [], [pos[0]]
    for p in pos[1:]:
        if p - current[-1] <= max_gap:
            current.append(p)
        else:
            clusters.append(current)
            current = [p]
    clusters.append(current)
    need = max(2, int(np.ceil(min_share * len(apex_by_driver))))
    return np.array([np.median(c) for c in clusters if len(c) >= need])


def corner_class(speed_kph: float) -> str:
    if speed_kph < LOW_MAX_KPH:
        return "low"
    return "medium" if speed_kph < MEDIUM_MAX_KPH else "high"


def session_corner_speeds(laps: pd.DataFrame, *, tolerance: float = 0.012) -> pd.DataFrame:
    """``laps``: driver_id, constructor_id, t_s, speed_kph for one session.

    Returns one row per constructor and corner class with the mean apex-speed difference
    to the field median (kph, positive = faster) and the number of corners used.
    """
    team_of = laps.groupby("driver_id")["constructor_id"].first()
    apex = {
        d: apexes(g["t_s"].to_numpy(), g["speed_kph"].to_numpy())
        for d, g in laps.groupby("driver_id")
    }
    corners = track_corners(apex)
    rows = []
    for k, c in enumerate(corners):
        for d, a in apex.items():
            near = a[(a["frac"] - c).abs() <= tolerance]
            if len(near):
                rows.append((k, d, team_of[d], near["speed_kph"].min()))
    per = pd.DataFrame(rows, columns=["corner", "driver_id", "constructor_id", "apex_kph"])
    if per.empty:
        return pd.DataFrame(columns=["constructor_id", "corner_class", "kph_vs_field", "corners"])
    field = per.groupby("corner")["apex_kph"].median().rename("field_kph")
    team = (
        per.groupby(["corner", "constructor_id"])["apex_kph"].max().rename("team_kph").reset_index()
    )
    team = team.join(field, on="corner")
    team["corner_class"] = team["field_kph"].map(corner_class)
    team["diff"] = team["team_kph"] - team["field_kph"]
    out = (
        team.groupby(["constructor_id", "corner_class"])
        .agg(kph_vs_field=("diff", "mean"), corners=("corner", "nunique"))
        .reset_index()
    )
    return out
