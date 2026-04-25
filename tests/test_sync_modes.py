from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_read_only_sync_pulls_without_push_or_export(monkeypatch, tmp_path):
    from scripts.sync import sync_flow

    calls: list[str] = []

    monkeypatch.setenv("EMERGE_SYNC_MODE", "read-only")
    monkeypatch.setattr(sync_flow, "load_hub_config", lambda: {"selected_verticals": ["mock"], "branch": "hub"})
    monkeypatch.setattr(sync_flow, "hub_worktree_path", lambda: tmp_path / "hub")
    monkeypatch.setattr(sync_flow, "connectors_root", lambda: tmp_path / "connectors")
    monkeypatch.setattr(sync_flow, "git_has_remote_changes", lambda *_args, **_kw: True)
    monkeypatch.setattr(sync_flow, "git_merge_remote", lambda *_args, **_kw: {"ok": True})
    monkeypatch.setattr(sync_flow, "import_vertical", lambda *_args, **_kw: calls.append("import"))
    monkeypatch.setattr(sync_flow, "export_vertical", lambda *_args, **_kw: calls.append("export"))
    monkeypatch.setattr(sync_flow, "git_push", lambda *_args, **_kw: calls.append("push") or {"ok": True})

    result = sync_flow.sync_connector("mock")

    assert result["mode"] == "read-only"
    assert result["pull"]["action"] == "imported"
    assert calls == ["import"]


def test_read_write_sync_preserves_existing_push_then_pull(monkeypatch, tmp_path):
    from scripts.sync import sync_flow

    calls: list[str] = []

    monkeypatch.setenv("EMERGE_SYNC_MODE", "read-write")
    monkeypatch.setattr(sync_flow, "load_hub_config", lambda: {"selected_verticals": ["mock"], "branch": "hub"})
    monkeypatch.setattr(sync_flow, "hub_worktree_path", lambda: tmp_path / "hub")
    monkeypatch.setattr(sync_flow, "connectors_root", lambda: tmp_path / "connectors")
    monkeypatch.setattr(sync_flow, "git_merge_remote", lambda *_args, **_kw: {"ok": True})
    monkeypatch.setattr(sync_flow, "export_vertical", lambda *_args, **_kw: calls.append("export"))
    monkeypatch.setattr(sync_flow, "git_push", lambda *_args, **_kw: calls.append("push") or {"ok": True})
    monkeypatch.setattr(sync_flow, "git_has_remote_changes", lambda *_args, **_kw: False)

    result = sync_flow.sync_connector("mock")

    assert result["mode"] == "read-write"
    assert result["push"]["ok"] is True
    assert calls == ["export", "push"]
