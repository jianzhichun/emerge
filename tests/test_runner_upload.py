from __future__ import annotations
import json, threading, time, urllib.request, urllib.error
from pathlib import Path
import pytest


def _make_server(tmp_path):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.daemon_http import DaemonHTTPServer

    class _StubDaemon:
        def handle_jsonrpc(self, req):
            return {"jsonrpc": "2.0", "id": req.get("id"), "result": {}}

    srv = DaemonHTTPServer(
        daemon=_StubDaemon(), port=0,
        pid_path=tmp_path / "d.pid",
        event_root=tmp_path / "operator-events",
        state_root=tmp_path / "repl",
    )
    srv.start()
    time.sleep(0.1)
    return srv


def _post_multipart(port, path, filename, file_bytes, mime="image/png", runner_profile=""):
    boundary = "boundary123"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + file_bytes + (
        f"\r\n--{boundary}\r\n"
        f'Content-Disposition: form-data; name="runner_profile"\r\n\r\n'
        f"{runner_profile}"
        f"\r\n--{boundary}--\r\n"
    ).encode()
    req = urllib.request.Request(
        f"http://localhost:{port}{path}",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _build_multipart_body(filename, file_bytes, mime="application/octet-stream"):
    boundary = "bnd"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode() + file_bytes + f"\r\n--{boundary}--\r\n".encode()
    return {
        "body": body,
        "headers": {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    }


def test_upload_stores_file_and_returns_path(tmp_path):
    srv = _make_server(tmp_path)
    try:
        resp = _post_multipart(srv.port, "/runner/upload", "error.png", b"PNGDATA")
        assert "file_id" in resp
        assert "path" in resp
        assert "mime" in resp
        assert Path(resp["path"]).exists()
        assert Path(resp["path"]).read_bytes() == b"PNGDATA"
        assert resp["mime"] == "image/png"
    finally:
        srv.stop()


def test_upload_sanitizes_filename(tmp_path):
    srv = _make_server(tmp_path)
    try:
        resp = _post_multipart(srv.port, "/runner/upload", "../../etc/passwd", b"DATA")
        stored = Path(resp["path"])
        assert ".." not in resp["path"]
        assert stored.exists()
    finally:
        srv.stop()


def test_upload_missing_file_returns_400(tmp_path):
    srv = _make_server(tmp_path)
    try:
        boundary = "boundary123"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="other"\r\n\r\nvalue\r\n'
            f"--{boundary}--\r\n"
        ).encode()
        req = urllib.request.Request(
            f"http://localhost:{srv.port}/runner/upload",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected HTTP error"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        srv.stop()


def test_upload_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setenv("EMERGE_UPLOAD_MAX_BYTES", "10")
    srv = _make_server(tmp_path)
    try:
        req_data = _build_multipart_body("big.bin", b"X" * 11)
        req = urllib.request.Request(
            f"http://localhost:{srv.port}/runner/upload",
            data=req_data["body"],
            headers=req_data["headers"],
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False, "expected HTTP error"
        except urllib.error.HTTPError as e:
            assert e.code == 413
    finally:
        srv.stop()
