"""How accurate is it? Backtest results."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from lib import footer, load, setup

meta = setup("How accurate")
acc = pd.DataFrame(load("accuracy") or [])

st.title("How accurate is it?")
st.markdown("""
Every race of the last two seasons was predicted again **using only data from before that
race weekend**, then compared with what happened, and with simple rules anyone could use:

- **Championship order**: the championship leader is the favourite, then second, ...
- **Previous race**: whoever won last time is the favourite.
- **Grid order** (after qualifying): the driver on pole is the favourite.

The model's settings were tuned on 2025 only. **2026 is the fair test.**
""")

STAGES = {
    "pre_weekend": "Before the weekend",
    "post_practice": "After practice",
    "post_quali": "After qualifying",
    "qualifying": "Qualifying, before the weekend",
    "qualifying_post_practice": "Qualifying, after practice",
}
METRICS = {
    "log_loss_win": ("Winner surprise (lower is better)", False),
    "brier_win": ("Win-chance error (lower is better)", False),
    "winner_hit": ("Favourite won", True),
    "podium_hits": ("Podium finishers picked", True),
    "spearman": ("Order match (1 = perfect)", False),
    "log_loss_pole": ("Pole surprise (lower is better)", False),
    "pole_hit": ("Favourite took pole", True),
    "top3_hits": ("Top 3 in qualifying picked", True),
}
METHODS = {
    "model": "Model",
    "championship": "Championship order",
    "previous_race": "Previous race",
    "grid": "Grid order",
    "previous_quali": "Previous qualifying",
}

if acc.empty:
    st.info("No backtest results published.")
    footer(meta)
    st.stop()

season = st.radio("Season", sorted(acc["season"].unique(), reverse=True), horizontal=True)
a = acc[(acc["season"] == season) & acc["metric"].isin(METRICS)]
for stage, label in STAGES.items():
    s = a[a["stage"] == stage]
    if s.empty:
        continue
    t = s.pivot(index="metric", columns="method", values="value")
    t = t[[m for m in METHODS if m in t.columns]].rename(columns=METHODS)
    t = t.loc[[m for m in METRICS if m in t.index]].astype(float)

    def fmt(v: float, is_pct: bool) -> str:
        if pd.isna(v):
            return "–"
        return f"{100 * v:.0f}%" if is_pct else f"{v:.2f}"

    shown = pd.DataFrame(
        [[fmt(v, METRICS[m][1]) for v in t.loc[m]] for m in t.index],
        index=[METRICS[m][0] for m in t.index],
        columns=t.columns,
    )
    n = int(s["n_events"].max())
    st.subheader(f"{label} ({n} races)")
    st.dataframe(shown, width="stretch")

st.markdown("""
**What the measures mean**

- *Winner surprise* (log loss): how surprised the forecast was by the actual winner. If it
  gave the winner 50%, the surprise is 0.69; at 10% it is 2.3. Lower is better.
- *Win-chance error* (Brier score): how far all the win chances were from what happened.
- *Favourite won*: how often the driver with the highest chance won. With ~20 races a
  season, this moves a lot by luck.
""")
footer(meta)
