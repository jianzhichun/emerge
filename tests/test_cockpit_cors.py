# tests/test_cockpit_cors.py
from __future__ import annotations

from scripts.admin.cockpit import resolve_cors_allow_origin


def test_cors_exact_host_match_lan():
    o = "http://192.168.1.10:8789"
    assert resolve_cors_allow_origin(o, "192.168.1.10:8789") == o


def test_cors_rejects_foreign_origin():
    assert resolve_cors_allow_origin("https://evil.example", "192.168.1.10:8789") == "null"


def test_cors_loopback_alias_same_port():
    o = "http://127.0.0.1:8789"
    assert resolve_cors_allow_origin(o, "localhost:8789") == o
    assert resolve_cors_allow_origin("http://localhost:8789", "127.0.0.1:8789") == "http://localhost:8789"


def test_cors_loopback_mismatched_port_rejected():
    assert resolve_cors_allow_origin("http://127.0.0.1:8789", "localhost:9999") == "null"


def test_cors_empty_origin():
    assert resolve_cors_allow_origin("", "localhost:8789") == "null"


def test_cors_localhost_matches_host():
    assert resolve_cors_allow_origin("http://localhost:8789", "localhost:8789") == "http://localhost:8789"
