"""Grid Oracle: the next race."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st
from lib import footer, frame, load, pct_column, setup

meta = setup("Next race")
race = load("next_race")
ev = race["event"]

st.title(ev["event_name"])
st.markdown(
    f"**{ev['season']} round {ev['round']}** · {ev.get('circuit_name') or ev['circuit_id']}, "
    f"{ev.get('country') or ''} · race day **{ev['race_date']}**"
    + (" · sprint weekend" if ev.get("is_sprint") else "")
)

stages = {s["label"]: s["stage"] for s in race["stages"]}
labels = list(stages)
choice = st.radio(
    "Forecast made",
    labels,
    index=len(labels) - 1,
    horizontal=True,
    help="Each stage uses what was known at that point of the weekend: the model alone, "
    "then practice lap times, then the qualifying result.",
)
stage = stages[choice]
df = frame(race["forecasts"][stage])
for c in ("p_win", "p_podium", "p_points", "p_dnf", "p_pole", "p_top3_grid"):
    if c in df:
        df[c] = 100 * df[c]

# ------------------------------------------------------------------ headline
top = df.sort_values("p_win", ascending=False).head(3)
cols = st.columns(3)
for col, (_, r) in zip(cols, top.iterrows(), strict=False):
    col.metric(f"{r['full_name']} ({r['team_name']})", f"{r['p_win']:.0f}% to win")

# ------------------------------------------------------------------ win chart
chart_df = df.sort_values("p_win", ascending=False).head(12)
colors = chart_df.drop_duplicates("team_name")
chart = (
    alt.Chart(chart_df)
    .mark_bar()
    .encode(
        x=alt.X("p_win:Q", title="Chance to win (%)"),
        y=alt.Y("full_name:N", sort="-x", title=None),
        color=alt.Color(
            "team_name:N",
            scale=alt.Scale(
                domain=colors["team_name"].tolist(),
                range=["#" + str(c).lstrip("#") if c else "#888888" for c in colors["team_color"]],
            ),
            legend=alt.Legend(title="Team", orient="bottom"),
        ),
        tooltip=[
            alt.Tooltip("full_name:N", title="Driver"),
            alt.Tooltip("team_name:N", title="Team"),
            alt.Tooltip("p_win:Q", title="Win %", format=".1f"),
            alt.Tooltip("p_podium:Q", title="Podium %", format=".1f"),
        ],
    )
    .properties(height=360)
)
st.altair_chart(chart, width="stretch")

# ------------------------------------------------------------------ table
strategies = race.get("strategies", {})
latest = race["latest_stage"]
if stage == latest and strategies:
    df["strategy"] = df["driver_id"].map(
        lambda d: ", ".join(
            f"{s['strategy']} ({100 * s['share']:.0f}%)" for s in strategies.get(d, [])[:2]
        )
    )

show = ["full_name", "team_name"]
config = {
    "full_name": st.column_config.TextColumn("Driver"),
    "team_name": st.column_config.TextColumn("Team"),
}
if "p_pole" in df and df["p_pole"].notna().any():
    show += ["p_pole"]
    config["p_pole"] = pct_column("Pole", "Chance of qualifying fastest")
show += ["p_win", "p_podium", "p_points", "p_dnf"]
config.update(
    {
        "p_win": pct_column("Win"),
        "p_podium": pct_column("Podium", "Chance of finishing in the top 3"),
        "p_points": pct_column("Points", "Chance of finishing in the top 10"),
        "p_dnf": pct_column("Retire", "Chance of not finishing"),
    }
)
if "expected_position" in df:
    show += ["expected_position"]
    config["expected_position"] = st.column_config.NumberColumn(
        "Avg. finish",
        help="Average finishing position in the simulations where the driver finishes",
        format="%.1f",
    )
if "strategy" in df:
    show += ["strategy"]
    config["strategy"] = st.column_config.TextColumn(
        "Likely tyres", help="Most likely compound sequences (S = soft, M = medium, H = hard)"
    )

st.subheader("Every driver")
st.dataframe(
    df.sort_values("p_win", ascending=False)[show],
    column_config=config,
    hide_index=True,
    width="stretch",
)

# ------------------------------------------------------------------ how it changed
if len(labels) > 1:
    st.subheader("How the picture changed over the weekend")
    rows = []
    for lab, st_key in stages.items():
        for r in race["forecasts"][st_key]:
            rows.append({"stage": lab, "driver": r["full_name"], "win": 100 * r["p_win"]})
    ch = pd.DataFrame(rows)
    leaders = ch.groupby("driver")["win"].max().nlargest(8).index
    pivot = ch[ch["driver"].isin(leaders)].pivot(index="driver", columns="stage", values="win")
    pivot = pivot[labels].sort_values(labels[-1], ascending=False)
    st.dataframe(pivot.round(0), width="stretch")

st.caption(
    "How to read this: 25% means that in 10,000 simulated races this driver won about 2,500. "
    "Even a big favourite loses often."
)
footer(meta)
