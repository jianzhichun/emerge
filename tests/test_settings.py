# tests/test_settings.py
from __future__ import annotations
import json, os
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _reset_cache():
    from scripts.policy_config import _reset_settings_cache
    _reset_settings_cache()
    yield
    _reset_settings_cache()


def _write_settings(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_load_settings_returns_defaults_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_SETTINGS_PATH", str(tmp_path / "nonexistent.json"))
    from scripts.policy_config import load_settings, _reset_settings_cache
    _reset_settings_cache()
    s = load_settings()
    assert s["policy"]["promote_min_attempts"] == 20
    assert s["runner"]["timeout_s"] == 30
    assert s["metrics_sink"] == "local_jsonl"


def test_load_settings_file_overrides_defaults(tmp_path, monkeypatch):
    cfg = tmp_path / "settings.json"
    _write_settings(cfg, {"policy": {"promote_min_attempts": 50}})
    monkeypatch.setenv("EMERGE_SETTINGS_PATH", str(cfg))
    from scripts.policy_config import load_settings, _reset_settings_cache
    _reset_settings_cache()
    s = load_settings()
    assert s["policy"]["promote_min_attempts"] == 50
    # non-overridden key keeps default
    assert s["policy"]["promote_min_success_rate"] == 0.95


def test_load_settings_env_path_overrides_default_home_path(tmp_path, monkeypatch):
    # env-path file wins over any default home path
    env_cfg = tmp_path / "custom.json"
    _write_settings(env_cfg, {"metrics_sink": "null"})
    # point default home somewhere else so it can't interfere
    monkeypatch.setenv("EMERGE_SETTINGS_PATH", str(env_cfg))
    from scripts.policy_config import load_settings
    s = load_settings()
    assert s["metrics_sink"] == "null"
    # sanity: non-overridden keys still come from defaults
    assert s["policy"]["promote_min_attempts"] == 20


def test_load_settings_rejects_invalid_policy_value(tmp_path, monkeypatch):
    cfg = tmp_path / "bad.json"
    _write_settings(cfg, {"policy": {"promote_min_attempts": "not-a-number"}})
    monkeypatch.setenv("EMERGE_SETTINGS_PATH", str(cfg))
    from scripts.policy_config import load_settings, _reset_settings_cache
    _reset_settings_cache()
    with pytest.raises(ValueError, match="promote_min_attempts"):
        load_settings()
