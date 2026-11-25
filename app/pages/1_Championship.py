"""Championship odds."""

from __future__ import annotations

import streamlit as st
from lib import footer, frame, load, pct_column, setup

meta = setup("Championship")
season = load("season")

st.title(f"{season['season']} championship")
left = season.get("remaining_events", [])
st.markdown(
    f"From today's standings plus **{len(left)} remaining race weekends**, each simulated "
    "lap by lap (sprints included)."
    if left
    else "The season is complete."
)

for key, label, name_col in (
    ("drivers", "Drivers", "full_name"),
    ("constructors", "Constructors", "team_name"),
):
    df = frame(season[key])
    df["p_title"] = 100 * df["p_title"]
    df["p_top3"] = 100 * df["p_top3"]
    df["range"] = df.apply(lambda r: f"{r['points_p05']:.0f}–{r['points_p95']:.0f}", axis=1)
    st.subheader(label)
    st.dataframe(
        df[[name_col, "current_points", "expected_points", "range", "p_title", "p_top3"]],
        column_config={
            name_col: st.column_config.TextColumn("Driver" if key == "drivers" else "Team"),
            "current_points": st.column_config.NumberColumn("Points now", format="%.0f"),
            "expected_points": st.column_config.NumberColumn(
                "Expected final", help="Average final points over the simulations", format="%.0f"
            ),
            "range": st.column_config.TextColumn(
                "Likely range", help="90% of simulated seasons end inside this range"
            ),
            "p_title": pct_column("Title"),
            "p_top3": pct_column("Top 3"),
        },
        hide_index=True,
        width="stretch",
    )

st.caption(
    "Each simulated season draws one set of 'how wrong are our estimates' for every car and "
    "driver and keeps it for all remaining races, so a car that is secretly faster stays "
    "faster all season. That keeps the title odds from being overconfident."
)
footer(meta)
