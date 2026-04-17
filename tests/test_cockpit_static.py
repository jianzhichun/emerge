from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path


def _start_test_server(tmp_path: Path, monkeypatch) -> str:
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("EMERGE_CONNECTOR_ROOT", str(tmp_path / "connectors"))
    (tmp_path / "connectors").mkdir(exist_ok=True)
    from scripts.repl_admin import cmd_serve

    result = cmd_serve(port=0, open_browser=False)
    assert result["ok"]
    return result["url"]


def test_root_serves_dist_index_when_available(tmp_path: Path, monkeypatch):
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text(
        "<!doctype html><html><body>cockpit-svelte-index</body></html>",
        encoding="utf-8",
    )

    from scripts.admin import cockpit as cockpit_mod

    monkeypatch.setattr(cockpit_mod._CockpitHandler, "_dist_dir", dist_dir)
    monkeypatch.setattr(cockpit_mod._CockpitHandler, "_dist_index_path", dist_dir / "index.html")

    base = _start_test_server(tmp_path, monkeypatch)
    with urllib.request.urlopen(f"{base}/") as resp:
        body = resp.read().decode("utf-8")
    assert "cockpit-svelte-index" in body


def test_assets_served_with_content_type(tmp_path: Path, monkeypatch):
    dist_dir = tmp_path / "dist"
    assets_dir = dist_dir / "assets"
    assets_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")
    (assets_dir / "app.js").write_text("console.log('ok');", encoding="utf-8")

    from scripts.admin import cockpit as cockpit_mod

    monkeypatch.setattr(cockpit_mod._CockpitHandler, "_dist_dir", dist_dir)
    monkeypatch.setattr(cockpit_mod._CockpitHandler, "_dist_index_path", dist_dir / "index.html")

    base = _start_test_server(tmp_path, monkeypatch)
    with urllib.request.urlopen(f"{base}/assets/app.js") as resp:
        content_type = resp.headers.get("Content-Type", "")
        body = resp.read()
    assert content_type.startswith("application/javascript")
    assert b"console.log('ok')" in body


def test_assets_path_traversal_rejected(tmp_path: Path, monkeypatch):
    dist_dir = tmp_path / "dist"
    (dist_dir / "assets").mkdir(parents=True)
    (dist_dir / "index.html").write_text("<!doctype html><html></html>", encoding="utf-8")

    from scripts.admin import cockpit as cockpit_mod

    monkeypatch.setattr(cockpit_mod._CockpitHandler, "_dist_dir", dist_dir)
    monkeypatch.setattr(cockpit_mod._CockpitHandler, "_dist_index_path", dist_dir / "index.html")

    base = _start_test_server(tmp_path, monkeypatch)
    try:
        urllib.request.urlopen(f"{base}/assets/..%2Fsecret.txt")
        assert False, "Expected traversal request to return 404"
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_root_returns_fallback_html_when_dist_missing(tmp_path: Path, monkeypatch):
    dist_dir = tmp_path / "missing-dist"

    from scripts.admin import cockpit as cockpit_mod

    monkeypatch.setattr(cockpit_mod._CockpitHandler, "_dist_dir", dist_dir)
    monkeypatch.setattr(cockpit_mod._CockpitHandler, "_dist_index_path", dist_dir / "index.html")

    base = _start_test_server(tmp_path, monkeypatch)
    with urllib.request.urlopen(f"{base}/") as resp:
        body = resp.read().decode("utf-8")
    assert "scripts/admin/cockpit/dist/index.html" in body
    assert "npm run build" in body
