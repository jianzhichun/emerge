from __future__ import annotations

import urllib.request


def test_no_proxy_urlopen_uses_shared_proxyless_opener(monkeypatch):
    from scripts import runner_http

    calls = []

    class _Opener:
        def open(self, req, timeout):
            calls.append((req, timeout))
            return "response"

    monkeypatch.setattr(runner_http, "_NO_PROXY_OPENER", _Opener())

    req = urllib.request.Request("http://runner.local/health")
    assert runner_http.no_proxy_urlopen(req, timeout=3.0) == "response"
    assert calls == [(req, 3.0)]
