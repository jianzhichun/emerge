"""
Shared pytest fixtures for the emerge test suite.
"""
import os
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent


@pytest.fixture(autouse=True)
def _mock_connector_root(monkeypatch):
    """Point EMERGE_CONNECTOR_ROOT at tests/connectors so PipelineEngine finds
    the mock connector during testing.  The mock directory lives here rather than
    in the plugin root so it is not shipped when Claude Code installs the plugin.
    """
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(_TESTS_DIR / "connectors"))


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
