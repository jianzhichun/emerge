from __future__ import annotations

import json
import os
import random
import time as _time
import urllib.error
import urllib.request
from hashlib import sha1
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Opener that bypasses system proxy (runner endpoints are always direct/LAN)
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

from scripts.policy_config import default_emerge_home


@dataclass
class RetryConfig:
    max_attempts: int = 3
    base_delay_s: float = 0.5
    max_delay_s: float = 10.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"RetryConfig.max_attempts must be >= 1, got {self.max_attempts}")
        if self.base_delay_s < 0:
            raise ValueError(f"RetryConfig.base_delay_s must be >= 0, got {self.base_delay_s}")
        if self.max_delay_s < self.base_delay_s:
            raise ValueError(f"RetryConfig.max_delay_s must be >= base_delay_s")


@dataclass
class RunnerClient:
    base_url: str
    timeout_s: float = 30.0
    retry: "RetryConfig | None" = None

    @classmethod
    def from_env(cls) -> "RunnerClient | None":
        raw = str(os.environ.get("EMERGE_RUNNER_URL", "")).strip()
        if not raw:
            return None
        timeout_raw = str(os.environ.get("EMERGE_RUNNER_TIMEOUT_S", "30")).strip()
        try:
            timeout_s = float(timeout_raw)
        except Exception:
            timeout_s = 30.0
        return cls(base_url=raw.rstrip("/"), timeout_s=max(1.0, timeout_s))

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        retry = self.retry or RetryConfig(max_attempts=1)
        last_exc: Exception | None = None
        for attempt in range(max(1, retry.max_attempts)):
            if attempt > 0:
                delay = min(retry.base_delay_s * (2 ** (attempt - 1)), retry.max_delay_s)
                _time.sleep(delay * random.random())
            try:
                return self._call_tool_once(tool_name, arguments)
            except RuntimeError as exc:
                msg = str(exc)
                # retry on connection errors and 5xx; not on 4xx
                if "runner http 4" in msg:
                    raise
                last_exc = exc
        assert last_exc is not None
        raise last_exc

    def _call_tool_once(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {"tool_name": tool_name, "arguments": arguments}
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.base_url}/run",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _NO_PROXY_OPENER.open(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"runner http {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"runner unreachable: {exc}") from exc
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise RuntimeError("runner response must be an object")
        if not bool(data.get("ok", False)):
            err = str(data.get("error", "unknown runner error"))
            raise RuntimeError(err)
        result = data.get("result", {})
        if not isinstance(result, dict):
            raise RuntimeError("runner result must be an object")
        return result

    def notify(
        self,
        stage: str,
        message: str,
        intent_draft: str = "",
        timeout_s: int = 0,
    ) -> dict[str, Any]:
        """Send a notification request to the runner's /notify endpoint.

        Blocks until the operator responds or timeout_s elapses.
        Returns {action: str, intent: str}.
        Raises RuntimeError on HTTP error or connection failure.
        """
        payload = {
            "stage": stage,
            "message": message,
            "intent_draft": intent_draft,
            "timeout_s": timeout_s,
        }
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        # Use a longer timeout than the dialog so the HTTP connection stays open
        # while the user is deciding.
        http_timeout = max(self.timeout_s, float(timeout_s) + 10.0)
        req = urllib.request.Request(
            url=f"{self.base_url}/notify",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with _NO_PROXY_OPENER.open(req, timeout=http_timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"runner notify http {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"runner notify unreachable: {exc}") from exc
        data = json.loads(raw)
        if not isinstance(data, dict) or not bool(data.get("ok", False)):
            raise RuntimeError(str(data.get("error", "notify failed")))
        result = data.get("result", {})
        if not isinstance(result, dict):
            raise RuntimeError("runner notify result must be an object")
        return result

    def health(self) -> dict[str, Any]:
        req = urllib.request.Request(
            url=f"{self.base_url}/health",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with _NO_PROXY_OPENER.open(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"runner http {exc.code}: {detail}") from exc
        except Exception as exc:
            raise RuntimeError(f"runner unreachable: {exc}") from exc
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise RuntimeError("runner health response must be an object")
        return data


@dataclass
class RunnerRouter:
    default_client: RunnerClient | None
    mapped_clients: dict[str, RunnerClient]
    pooled_clients: list[RunnerClient]

    @classmethod
    def from_env(cls) -> "RunnerRouter | None":
        persisted = cls._load_persisted_config()
        default_url = str(persisted.get("default_url", "") or "").strip()
        mapped_urls = dict(persisted.get("map", {}))
        pooled_urls = list(persisted.get("pool", []))

        env_default_url = str(os.environ.get("EMERGE_RUNNER_URL", "")).strip()
        if env_default_url:
            default_url = env_default_url

        timeout_raw = str(os.environ.get("EMERGE_RUNNER_TIMEOUT_S", "30")).strip()
        try:
            timeout_s = max(1.0, float(timeout_raw))
        except Exception:
            timeout_s = 30.0

        from scripts.policy_config import load_settings
        try:
            _s = load_settings()
            _r = _s.get("runner", {})
            retry_cfg = RetryConfig(
                max_attempts=int(_r.get("retry_max_attempts", 3)),
                base_delay_s=float(_r.get("retry_base_delay_s", 0.5)),
                max_delay_s=float(_r.get("retry_max_delay_s", 10.0)),
            )
        except Exception:
            retry_cfg = RetryConfig()

        raw_map = str(os.environ.get("EMERGE_RUNNER_MAP", "")).strip()
        if raw_map:
            parsed = json.loads(raw_map)
            if not isinstance(parsed, dict):
                raise RuntimeError("EMERGE_RUNNER_MAP must be a JSON object")
            for key, value in parsed.items():
                key_text = str(key).strip()
                url_text = str(value).strip()
                if key_text:
                    if url_text:
                        mapped_urls[key_text] = url_text
                    elif key_text in mapped_urls:
                        del mapped_urls[key_text]

        raw_urls = str(os.environ.get("EMERGE_RUNNER_URLS", "")).strip()
        if raw_urls:
            pooled_urls = []
            for item in raw_urls.split(","):
                url_text = item.strip()
                if url_text:
                    pooled_urls.append(url_text)

        default_client = (
            RunnerClient(base_url=default_url.rstrip("/"), timeout_s=timeout_s, retry=retry_cfg) if default_url else None
        )
        mapped_clients: dict[str, RunnerClient] = {}
        for key, url in mapped_urls.items():
            key_text = str(key).strip()
            url_text = str(url).strip()
            if key_text and url_text:
                mapped_clients[key_text] = RunnerClient(
                    base_url=url_text.rstrip("/"), timeout_s=timeout_s, retry=retry_cfg
                )
        pooled_clients: list[RunnerClient] = []
        for url in pooled_urls:
            url_text = str(url).strip()
            if url_text:
                pooled_clients.append(RunnerClient(base_url=url_text.rstrip("/"), timeout_s=timeout_s, retry=retry_cfg))

        if default_client is None and not mapped_clients and not pooled_clients:
            return None
        return cls(
            default_client=default_client,
            mapped_clients=mapped_clients,
            pooled_clients=pooled_clients,
        )

    @staticmethod
    def persisted_config_path() -> Path:
        raw = str(os.environ.get("EMERGE_RUNNER_CONFIG_PATH", "")).strip()
        if raw:
            return Path(raw).expanduser().resolve()
        return (default_emerge_home() / "runner-map.json").resolve()

    @classmethod
    def _load_persisted_config(cls) -> dict[str, Any]:
        path = cls.persisted_config_path()
        if not path.exists():
            return {"default_url": "", "map": {}, "pool": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise RuntimeError("runner config must be a JSON object")
        raw_map = data.get("map", {})
        if not isinstance(raw_map, dict):
            raw_map = {}
        raw_pool = data.get("pool", [])
        if not isinstance(raw_pool, list):
            raw_pool = []
        return {
            "default_url": str(data.get("default_url", "") or ""),
            "map": {str(k): str(v) for k, v in raw_map.items()},
            "pool": [str(item) for item in raw_pool],
        }

    def find_client(self, arguments: dict[str, Any]) -> "RunnerClient | None":
        """Return the best matching client for these arguments, or None if no match.

        Unlike _select_client, this never raises — callers can fall back to local
        execution when None is returned (e.g., no target_profile matches any mapped
        client and no default client is configured).
        """
        try:
            return self._select_client(arguments)
        except RuntimeError:
            return None

    def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        client = self._select_client(arguments)
        return client.call_tool(tool_name, arguments)

    def health_summary(self) -> dict[str, Any]:
        endpoints: list[dict[str, Any]] = []
        for key, client in self.mapped_clients.items():
            endpoints.append(self._probe(name=f"map:{key}", client=client))
        for i, client in enumerate(self.pooled_clients):
            endpoints.append(self._probe(name=f"pool:{i}", client=client))
        if self.default_client is not None:
            endpoints.append(self._probe(name="default", client=self.default_client))
        any_reachable = any(bool(e.get("reachable", False)) for e in endpoints)
        return {
            "configured": True,
            "any_reachable": any_reachable,
            "endpoint_count": len(endpoints),
            "endpoints": endpoints,
        }

    def _select_client(self, arguments: dict[str, Any]) -> RunnerClient:
        runner_id = str(arguments.get("runner_id", "")).strip()
        if runner_id and runner_id in self.mapped_clients:
            return self.mapped_clients[runner_id]

        target_profile = str(arguments.get("target_profile", "")).strip()
        if target_profile and target_profile in self.mapped_clients:
            return self.mapped_clients[target_profile]

        if self.pooled_clients:
            selector = runner_id or target_profile or str(arguments.get("connector", "")).strip() or "default"
            idx = int(sha1(selector.encode("utf-8")).hexdigest(), 16) % len(self.pooled_clients)
            return self.pooled_clients[idx]

        if self.default_client is not None:
            return self.default_client
        raise RuntimeError("no runner configured")

    @staticmethod
    def _probe(name: str, client: RunnerClient) -> dict[str, Any]:
        try:
            health = client.health()
            return {
                "name": name,
                "url": client.base_url,
                "reachable": True,
                "health": health,
                "error": "",
            }
        except Exception as exc:
            return {
                "name": name,
                "url": client.base_url,
                "reachable": False,
                "health": {},
                "error": str(exc),
            }
