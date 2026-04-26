from __future__ import annotations


def test_read_json_payload_tolerates_empty_and_invalid_input(monkeypatch):
    from hooks import hook_io

    monkeypatch.setattr(hook_io.sys.stdin, "read", lambda: "")
    assert hook_io.read_json_payload() == {}

    monkeypatch.setattr(hook_io.sys.stdin, "read", lambda: "{bad json")
    assert hook_io.read_json_payload() == {}
