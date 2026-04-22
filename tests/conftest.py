"""
Shared pytest fixtures for the emerge test suite.
"""
import os
import json
from pathlib import Path

import pytest
from scripts.intent_registry import registry_path

_TESTS_DIR = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _mock_connector_root(monkeypatch, tmp_path):
    """Isolate all production state paths for every test.

    - EMERGE_CONNECTOR_ROOT → tests/connectors/ (mock connector, not shipped)
    - EMERGE_STATE_ROOT     → tmp_path/state   (prevent flywheel bridge from
      hitting stable production intents and executing live pipelines)
    - EMERGE_METRICS_SINK   → null             (no EventAppender thread; prevents
      accumulated fsync contention across tests on the shared metrics file)
    - EMERGE_HOOK_STATE_ROOT→ tmp_path/hook-state (prevent tests from writing
      deltas/span-WAL to the developer's production hook-state)
    """
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(_TESTS_DIR / "connectors"))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("EMERGE_METRICS_SINK", "null")
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir(parents=True, exist_ok=True)
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EMERGE_HOOK_STATE_ROOT", str(hook_state))


@pytest.fixture(autouse=True)
def isolate_runner_config(tmp_path):
    """Prevent tests from picking up the developer's persisted runner-map.json.

    RunnerRouter.from_env() reads ~/.emerge/runner-map.json by default.
    Without this fixture, tests run on a machine with a configured remote runner
    will try to connect to it, causing long timeouts and false failures.

    Tests that genuinely need a runner (e.g. test_remote_runner.py) create their
    own RunnerClient/RunnerExecutor directly and are not affected by this env var.
    """
    runner_cfg = tmp_path / "runner-map.json"
    runner_cfg.write_text('{"default_url": "", "map": {}, "pool": []}', encoding="utf-8")
    old = os.environ.get("EMERGE_RUNNER_CONFIG_PATH")
    os.environ["EMERGE_RUNNER_CONFIG_PATH"] = str(runner_cfg)
    yield
    if old is None:
        os.environ.pop("EMERGE_RUNNER_CONFIG_PATH", None)
    else:
        os.environ["EMERGE_RUNNER_CONFIG_PATH"] = old


@pytest.fixture
def isolate_hook_state(tmp_path, monkeypatch):
    """Give each test its own hook state dir (EMERGE_HOOK_STATE_ROOT).
    Also creates state.json so hooks don't crash on missing file.
    """
    hook_state = tmp_path / "hook-state"
    hook_state.mkdir()
    (hook_state / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EMERGE_HOOK_STATE_ROOT", str(hook_state))
    return hook_state


@pytest.fixture
def seed_intent_registry():
    """Write state/registry/intents.json and return its path."""

    def _seed(state_root: Path, entries: dict) -> Path:
        state_root.mkdir(parents=True, exist_ok=True)
        path = registry_path(state_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"intents": entries}, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    return _seed
