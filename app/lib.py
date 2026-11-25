"""Shared helpers for the Streamlit app: load the published JSON, common page furniture."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

DATA_DIR = Path(__file__).resolve().parent.parent / "site_data"

DISCLAIMER = (
    "Unofficial fan project, not affiliated with Formula 1, the FIA or any team. "
    "Probabilities from a statistical model: for fun, not for betting."
)


@st.cache_data(ttl=600)
def load(name: str):
    f = DATA_DIR / f"{name}.json"
    if not f.exists():
        return None
    return json.loads(f.read_text(encoding="utf-8"))


def setup(title: str) -> dict | None:
    st.set_page_config(page_title=f"{title} · Grid Oracle", layout="wide")
    meta = load("meta")
    if meta is None:
        st.error("No forecast published yet. Run `python -m f1_forecast.predict.live`.")
        st.stop()
    return meta


def footer(meta: dict) -> None:
    st.divider()
    st.caption(
        f"Updated {meta['generated_at'].replace('T', ' ').rstrip('Z')} UTC · model "
        f"{meta['model_version']} · {meta['sims']:,} simulations per stage. {DISCLAIMER}"
    )


def pct_column(label: str, help_text: str | None = None):
    return st.column_config.ProgressColumn(
        label, help=help_text, format="%.0f%%", min_value=0, max_value=100
    )


def frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)
