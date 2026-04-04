"""
Shared pytest fixtures for the emerge test suite.
"""
import os
import pytest


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
