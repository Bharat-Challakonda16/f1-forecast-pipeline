from pathlib import Path

import pytest

import f1_forecast
from f1_forecast.config import load_sources, load_yaml, unverified_sources

EXPECTED_SOURCES = {"jolpica", "fastf1", "openf1", "open_meteo", "gdelt", "rss", "manual"}


def test_package_imports():
    assert f1_forecast.__version__
    assert "Not affiliated with Formula 1" in f1_forecast.DISCLAIMER


def test_sources_config_lists_all_brief_sources():
    assert set(load_sources()) == EXPECTED_SOURCES


def test_http_sources_have_https_urls():
    for name, cfg in load_sources().items():
        urls = [cfg["base_url"]] if "base_url" in cfg else list(cfg.get("endpoints", {}).values())
        for url in urls:
            assert url.startswith("https://"), f"{name}: {url}"


def test_unverified_sources_flagged():
    # Phase 1 must clear these flags after checking live docs.
    assert "jolpica" in unverified_sources()


def test_load_yaml_rejects_non_mapping(tmp_path: Path):
    (tmp_path / "bad.yaml").write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_yaml("bad.yaml", tmp_path)
