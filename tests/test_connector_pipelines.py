"""Unit tests for real connector pipeline verify logic.

Audit finding (2026-04-18): verify_read functions in hypermesh and zwcad
accepted mock-source fallback rows as valid, allowing the bridge to record
successes on fake data. These tests assert that mock-source output fails
verification, so bridge_silent_empty / verify_degraded demotions fire
correctly when the real remote host is unreachable.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONNECTORS = ROOT / "tests" / "connectors"


def _load(connector: str, mode: str, name: str):
    path = CONNECTORS / connector / "pipelines" / mode / f"{name}.py"
    spec = importlib.util.spec_from_file_location("_pipeline", path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ── hypermesh.read.state ──────────────────────────────────────────────────────

class TestHypermeshReadState:
    def _mod(self):
        return _load("hypermesh", "read", "state")

    def test_verify_rejects_mock_source(self):
        mod = self._mod()
        mock_rows = [{"model_name": "m", "node_count": 0, "element_count": 0,
                      "component_count": 0, "source": "mock"}]
        result = mod.verify_read({}, {}, mock_rows)
        assert result["ok"] is False, "mock-source rows must fail verification"
        assert result.get("why") == "mock_fallback"

    def test_verify_accepts_live_source(self):
        mod = self._mod()
        live_rows = [{"model_name": "m", "node_count": 100, "element_count": 200,
                      "component_count": 5, "source": "live"}]
        result = mod.verify_read({}, {}, live_rows)
        assert result["ok"] is True

    def test_verify_rejects_empty_rows(self):
        mod = self._mod()
        assert mod.verify_read({}, {}, [])["ok"] is False

    def test_run_read_fallback_marks_source_mock(self):
        """When TCP connection fails, run_read must mark rows source=mock."""
        mod = self._mod()
        rows = mod.run_read({}, {"hm_host": "127.0.0.1", "hm_port": 1,
                                  "hm_timeout": 0.05})
        assert all(r.get("source") == "mock" for r in rows), (
            "fallback rows must have source=mock so verify_read can detect them"
        )


# ── zwcad.read.state ──────────────────────────────────────────────────────────

class TestZwcadReadState:
    def _mod(self):
        return _load("zwcad", "read", "state")

    def test_verify_rejects_mock_source(self):
        mod = self._mod()
        mock_rows = [{"id": "L0", "name": "0", "document_id": "d", "on": True,
                      "source": "mock"}]
        result = mod.verify_read({}, {}, mock_rows)
        assert result["ok"] is False
        assert result.get("why") == "mock_fallback"

    def test_verify_accepts_live_source(self):
        mod = self._mod()
        live_rows = [{"id": "L0", "name": "Layer1", "document_id": "d",
                      "on": True, "source": "live"}]
        result = mod.verify_read({}, {}, live_rows)
        assert result["ok"] is True

    def test_verify_rejects_empty_rows(self):
        mod = self._mod()
        assert mod.verify_read({}, {}, [])["ok"] is False

    def test_run_read_fallback_marks_source_mock(self):
        """When win32com is unavailable, run_read must mark rows source=mock."""
        mod = self._mod()
        rows = mod.run_read({}, {"document_id": "test-doc"})
        assert all(r.get("source") == "mock" for r in rows), (
            "fallback rows must have source=mock"
        )
