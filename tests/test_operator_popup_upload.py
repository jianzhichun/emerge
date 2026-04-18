from __future__ import annotations
import json, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import pytest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _make_mock_server(tmp_path, *, fail=False, too_large=False):
    results = []

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            if too_large:
                body = json.dumps({"error": "file too large"}).encode()
                self.send_response(413)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if fail:
                body = json.dumps({"error": "no file"}).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            fake_path = str(tmp_path / "uploads" / "abc" / "test.png")
            resp = {"file_id": "abc", "path": fake_path, "mime": "image/png"}
            body = json.dumps(resp).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            results.append(resp)

    srv = HTTPServer(("localhost", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, results


def test_upload_file_success(tmp_path):
    from scripts.operator_popup import _upload_file
    src = tmp_path / "test.png"
    src.write_bytes(b"PNGDATA")
    mock_srv, _ = _make_mock_server(tmp_path)
    url = f"http://localhost:{mock_srv.server_address[1]}/runner/upload"
    att = _upload_file(url, src)
    mock_srv.shutdown()
    assert att["name"] == "test.png"
    assert att["mime"] == "image/png"
    assert "path" in att


def test_upload_file_http_error_raises(tmp_path):
    from scripts.operator_popup import _upload_file
    src = tmp_path / "test.png"
    src.write_bytes(b"DATA")
    mock_srv, _ = _make_mock_server(tmp_path, fail=True)
    url = f"http://localhost:{mock_srv.server_address[1]}/runner/upload"
    with pytest.raises(RuntimeError, match="upload failed"):
        _upload_file(url, src)
    mock_srv.shutdown()


def test_upload_file_413_raises(tmp_path):
    from scripts.operator_popup import _upload_file
    src = tmp_path / "big.bin"
    src.write_bytes(b"X" * 100)
    mock_srv, _ = _make_mock_server(tmp_path, too_large=True)
    url = f"http://localhost:{mock_srv.server_address[1]}/runner/upload"
    with pytest.raises(RuntimeError, match="file too large"):
        _upload_file(url, src)
    mock_srv.shutdown()
