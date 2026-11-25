"""About: how it works, data sources, disclaimer."""

from __future__ import annotations

import streamlit as st
from lib import DISCLAIMER, footer, setup

meta = setup("About")
st.title("About Grid Oracle")
st.markdown(f"""
**{DISCLAIMER}** No team logos or driver photos are used.

### How it works

1. **Data.** Results, qualifying and every lap since 2001, detailed timing and tyres since
   2018, and hourly weather at every circuit, stored in a Databricks lakehouse.
2. **Cars and drivers.** A Bayesian model splits each driver's pace in every session into
   how fast the car was *at that point of the season* (so upgrades show up), how good the
   driver is, and what that driver does at particular kinds of track and at each circuit.
   Young drivers are allowed to improve faster than veterans.
3. **Simulation.** Each race is simulated lap by lap 10,000 times: tyre wear and
   strategies, pit stops, starts, overtaking calibrated for each circuit, safety cars,
   crashes and failures.
4. **During the weekend.** After practice, the forecast is shifted by how much faster or
   slower each car and driver was than expected. After qualifying, the actual grid is
   used, blended with how often each grid slot has won in the past.
5. **Checked honestly.** Every past race is predicted again using only what was known
   before it, and compared with simple rules (see *How accurate*).

### Data sources

- [Jolpica-F1](https://github.com/jolpica/jolpica-f1): results since 2001
- [OpenF1](https://openf1.org/): timing data 2023+ (CC BY-NC-SA 4.0)
- [FastF1](https://docs.fastf1.dev/): timing data 2018–2022
- [Open-Meteo](https://open-meteo.com/): weather (CC BY 4.0)

Raw timing data is never republished; this site shows only model results.
""")
footer(meta)
