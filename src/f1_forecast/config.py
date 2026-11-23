"""Load project configuration from the repo's ``configs/`` folder."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "configs"


def load_yaml(name: str, config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    """Read ``<config_dir>/<name>`` and return its top-level mapping."""
    path = config_dir / name
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return data


def load_sources(config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    """Return the ``sources`` mapping from ``sources.yaml``."""
    return load_yaml("sources.yaml", config_dir)["sources"]


def load_source_defaults(config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    """Return the shared ``defaults`` (user agent, timeout, retries) from ``sources.yaml``."""
    return load_yaml("sources.yaml", config_dir).get("defaults", {})


def unverified_sources(config_dir: Path = CONFIG_DIR) -> list[str]:
    """Names of sources whose endpoints or limits still need checking against live docs."""
    return [name for name, cfg in load_sources(config_dir).items() if cfg.get("verify")]
