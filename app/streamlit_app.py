"""Grid Oracle: entry point. Names the pages in the sidebar and runs the chosen one."""

from __future__ import annotations

import streamlit as st

pages = [
    st.Page("views/next_race.py", title="Next race", default=True),
    st.Page("views/championship.py", title="Championship"),
    st.Page("views/accuracy.py", title="How accurate"),
    st.Page("views/about.py", title="About"),
]
st.navigation(pages).run()
