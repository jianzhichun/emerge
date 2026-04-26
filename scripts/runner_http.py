"""Shared HTTP primitives for direct runner communication."""
from __future__ import annotations

import urllib.request
from typing import Any

_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def no_proxy_urlopen(req: urllib.request.Request, *, timeout: float) -> Any:
    """Open a runner HTTP request without consulting system proxy settings."""
    return _NO_PROXY_OPENER.open(req, timeout=timeout)
