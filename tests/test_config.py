"""tests/test_config.py — tests for config.py"""
import json
import pytest
from pathlib import Path
from unittest.mock import patch

from claudebud.config import load_config, save_config, get_config_path, DEFAULTS


@pytest.fixture()
def tmp_config(tmp_path, monkeypatch):
    """Redirect config path to a temp directory."""
    config_file = tmp_path / ".claudebud" / "config.json"
    monkeypatch.setattr(
        "claudebud.config.get_config_path", lambda: config_file
    )
    return config_file


def test_load_creates_defaults_when_missing(tmp_config):
    assert not tmp_config.exists()
    cfg = load_config()
    assert tmp_config.exists()
    assert cfg["port"] == DEFAULTS["port"]
    assert cfg["ntfy_topic"] == ""
    assert isinstance(cfg["prompt_patterns"], list)


def test_load_returns_saved_values(tmp_config):
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    tmp_config.write_text(json.dumps({"port": 9999, **{k: v for k, v in DEFAULTS.items() if k != "port"}}))
    cfg = load_config()
    assert cfg["port"] == 9999


def test_load_fills_missing_keys(tmp_config):
    tmp_config.parent.mkdir(parents=True, exist_ok=True)
    # Write config with only 'port' key
    tmp_config.write_text(json.dumps({"port": 4242}))
    cfg = load_config()
    assert cfg["port"] == 4242
    assert "ntfy_topic" in cfg
    assert "prompt_patterns" in cfg


def test_save_and_reload_roundtrip(tmp_config):
    original = dict(DEFAULTS)
    original["ntfy_topic"] = "my-topic"
    original["port"] = 5555
    save_config(original)
    assert tmp_config.exists()
    cfg = load_config()
    assert cfg["ntfy_topic"] == "my-topic"
    assert cfg["port"] == 5555


def test_save_creates_parent_directory(tmp_path, monkeypatch):
    deep_path = tmp_path / "a" / "b" / "config.json"
    monkeypatch.setattr("claudebud.config.get_config_path", lambda: deep_path)
    save_config(DEFAULTS)
    assert deep_path.exists()
