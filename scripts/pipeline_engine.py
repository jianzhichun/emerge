from __future__ import annotations

import importlib.util
import os
import re
import threading
from pathlib import Path
from typing import Any


class PipelineMissingError(FileNotFoundError):
    """Raised when a pipeline's .yaml + .py files cannot be found in any connector root.

    Subclasses FileNotFoundError so existing broad ``except Exception`` handlers
    still catch it, but ``call_tool`` can distinguish it with a specific ``except``.
    """
    def __init__(self, connector: str, mode: str, pipeline: str, searched: str) -> None:
        self.connector = connector
        self.mode = mode
        self.pipeline = pipeline
        self.searched = searched
        super().__init__(
            f"Pipeline '{connector}/{mode}/{pipeline}' not found in: {searched}"
        )


_USER_CONNECTOR_ROOT = Path("~/.emerge/connectors").expanduser()


class PipelineEngine:
    """Load and execute deterministic connector pipelines.

    The engine searches connector roots, validates pipeline metadata, runs
    Python or YAML pipelines, and normalizes read/write results for bridge and
    MCP callers. It does not perform policy decisions or LLM synthesis.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).resolve().parents[1]
        # Search order: env override (prepended) → user connector root → plugin connector root
        # EMERGE_CONNECTOR_ROOT adds an *extra* root at the front — it does not replace the
        # user connector root.  This lets tests inject fixtures (e.g. mock) while still
        # reaching user-space connectors (e.g. zwcad in ~/.emerge/connectors/).
        env_root = os.environ.get("EMERGE_CONNECTOR_ROOT", "").strip()
        if env_root:
            self._connector_roots = [Path(env_root).expanduser(), _USER_CONNECTOR_ROOT, self.root / "connectors"]
        else:
            self._connector_roots = [_USER_CONNECTOR_ROOT, self.root / "connectors"]
        self._cache_lock = threading.Lock()
        self._pipeline_cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    @staticmethod
    def _validate_path_segment(value: str, label: str) -> None:
        """Reject connector/pipeline values that could escape the connector root.

        Allowed pattern: lowercase letters, digits, hyphens, underscores, dots,
        and forward-slashes (for sub-pipeline names like "sub/pipeline").
        Rejected: path traversal sequences ('..'), leading slash, or uppercase letters.
        """
        _SAFE = re.compile(r"^[a-z][a-z0-9_./-]*$")
        if not value or ".." in value or value.startswith("/") or not _SAFE.match(value):
            raise ValueError(
                f"invalid {label} {value!r}: must start with lowercase letter, "
                "contain only [a-z0-9_.-/], no path traversal"
            )

    def run_read(self, args: dict[str, Any]) -> dict[str, Any]:
        """Execute a read pipeline and return rows plus verification metadata."""
        connector = args.get("connector", "").strip()
        pipeline = args.get("pipeline", "").strip()
        if not connector or not pipeline:
            raise ValueError(
                f"run_read: 'connector' and 'pipeline' are required (got connector={connector!r}, pipeline={pipeline!r})"
            )
        self._validate_path_segment(connector, "connector")
        self._validate_path_segment(pipeline, "pipeline")
        metadata, module = self._load_pipeline(connector, "read", pipeline)
        if module is None:
            # YAML scenario pipeline — delegate to YAMLScenarioEngine
            from scripts.pipeline_yaml_engine import YAMLScenarioEngine
            engine = YAMLScenarioEngine()
            result = engine.execute(metadata, args, pipeline_engine=self, mode="read")
            return self._build_read_result(
                connector=connector,
                pipeline=pipeline,
                metadata=metadata,
                rows=result.get("rows", []),
                verify_result=result.get("verify_result", {"ok": True}),
            )
        rows = module.run_read(metadata=metadata, args=args)
        verify_result = {"ok": True}
        verify_fn = getattr(module, "verify_read", None)
        if callable(verify_fn):
            verify_result = verify_fn(metadata=metadata, args=args, rows=rows)
            if not isinstance(verify_result, dict):
                raise ValueError("verify_read must return an object")
        return self._build_read_result(
            connector=connector,
            pipeline=pipeline,
            metadata=metadata,
            rows=rows,
            verify_result=verify_result,
        )

    def run_write(self, args: dict[str, Any]) -> dict[str, Any]:
        """Execute a write pipeline and enforce stop/rollback verification policy."""
        connector = args.get("connector", "").strip()
        pipeline = args.get("pipeline", "").strip()
        if not connector or not pipeline:
            raise ValueError(
                f"run_write: 'connector' and 'pipeline' are required (got connector={connector!r}, pipeline={pipeline!r})"
            )
        self._validate_path_segment(connector, "connector")
        self._validate_path_segment(pipeline, "pipeline")
        metadata, module = self._load_pipeline(connector, "write", pipeline)
        if module is None:
            # YAML scenario pipeline — delegate to YAMLScenarioEngine
            from scripts.pipeline_yaml_engine import YAMLScenarioEngine
            _eng = YAMLScenarioEngine()
            _res = _eng.execute(metadata, args, pipeline_engine=self, mode="write")
            _action = _res.get("action_result", {"ok": True})
            _vr = _res.get("verify_result", {"ok": True})
            _vs = "verified" if _vr.get("ok") else "degraded"
            _policy = str(metadata.get("rollback_or_stop_policy", "stop"))
            _rb_executed = False
            _rb_result: dict[str, Any] | None = None
            _stop = False
            if _vs == "degraded":
                if _policy == "rollback":
                    try:
                        _rb = _eng.execute_rollback(metadata, args, pipeline_engine=self)
                        _rb_executed = True
                        _rb_result = _rb
                    except Exception as _exc:
                        _rb_result = {"ok": False, "error": str(_exc)}
                        _rb_executed = True
                else:
                    _stop = True
            return self._build_write_result(
                connector=connector, pipeline=pipeline, metadata=metadata,
                action_result=_action, verify_result=_vr,
                stop_triggered=_stop, rollback_executed=_rb_executed, rollback_result=_rb_result,
            )
        action_result = module.run_write(metadata=metadata, args=args)
        verify_fn = getattr(module, "verify_write", None)
        if not callable(verify_fn):
            raise ValueError("verify_write is required for write pipelines")
        verify_result = verify_fn(metadata=metadata, args=args, action_result=action_result)
        if not isinstance(verify_result, dict):
            raise ValueError("verify_write must return an object")
        verification_state = "verified" if verify_result.get("ok") else "degraded"
        policy = str(metadata.get("rollback_or_stop_policy", "stop"))
        rollback_executed = False
        rollback_result: dict[str, Any] | None = None
        stop_triggered = False

        if verification_state == "degraded":
            if policy == "rollback":
                rollback_fn = getattr(module, "rollback_write", None)
                if callable(rollback_fn):
                    try:
                        rollback_payload = rollback_fn(
                            metadata=metadata, args=args, action_result=action_result
                        )
                        if isinstance(rollback_payload, dict):
                            rollback_result = rollback_payload
                        else:
                            rollback_result = {"ok": False, "error": "rollback_write must return object"}
                    except Exception as exc:
                        rollback_result = {"ok": False, "error": str(exc)}
                    rollback_executed = True
                else:
                    rollback_result = {"ok": False, "error": "rollback_write not implemented"}
                    stop_triggered = True
            else:
                stop_triggered = True
        return self._build_write_result(
            connector=connector,
            pipeline=pipeline,
            metadata=metadata,
            action_result=action_result,
            verify_result=verify_result,
            stop_triggered=stop_triggered,
            rollback_executed=rollback_executed,
            rollback_result=rollback_result,
        )

    def run_workflow(self, args: dict[str, Any]) -> dict[str, Any]:
        """Execute a workflow YAML pipeline and return its action/verification result."""
        connector = args.get("connector", "").strip()
        pipeline = args.get("pipeline", "").strip()
        if not connector or not pipeline:
            raise ValueError(
                f"run_workflow: 'connector' and 'pipeline' are required (got connector={connector!r}, pipeline={pipeline!r})"
            )
        self._validate_path_segment(connector, "connector")
        self._validate_path_segment(pipeline, "pipeline")
        metadata, module = self._load_pipeline(connector, "workflow", pipeline)
        has_write_steps = isinstance(metadata.get("write_steps"), list) and len(metadata["write_steps"]) > 0
        if has_write_steps:
            action_result = module.run_write(metadata=metadata, args=args)
            verify_fn = getattr(module, "verify_write", None)
            if not callable(verify_fn):
                raise ValueError("verify_write is required for workflow pipelines with write_steps")
            verify_result = verify_fn(metadata=metadata, args=args, action_result=action_result)
            if not isinstance(verify_result, dict):
                raise ValueError("verify_write must return an object")
            verification_state = "verified" if verify_result.get("ok") else "degraded"
            policy = str(metadata.get("rollback_or_stop_policy", "stop"))
            rollback_executed = False
            rollback_result: dict[str, Any] | None = None
            stop_triggered = False
            if verification_state == "degraded":
                if policy == "rollback":
                    rollback_fn = getattr(module, "rollback_write", None)
                    if callable(rollback_fn):
                        try:
                            rollback_payload = rollback_fn(metadata=metadata, args=args, action_result=action_result)
                            rollback_result = rollback_payload if isinstance(rollback_payload, dict) else {"ok": False, "error": "rollback_write must return object"}
                        except Exception as exc:
                            rollback_result = {"ok": False, "error": str(exc)}
                        rollback_executed = True
                    else:
                        rollback_result = {"ok": False, "error": "rollback_write not implemented"}
                        stop_triggered = True
                else:
                    stop_triggered = True
            return self._build_write_result(
                connector=connector, pipeline=pipeline, mode="workflow",
                metadata=metadata, action_result=action_result, verify_result=verify_result,
                stop_triggered=stop_triggered, rollback_executed=rollback_executed, rollback_result=rollback_result,
            )
        else:
            rows = module.run_read(metadata=metadata, args=args)
            verify_result_r = {"ok": True}
            verify_fn_r = getattr(module, "verify_read", None)
            if callable(verify_fn_r):
                verify_result_r = verify_fn_r(metadata=metadata, args=args, rows=rows)
                if not isinstance(verify_result_r, dict):
                    raise ValueError("verify_read must return an object")
            return self._build_read_result(
                connector=connector, pipeline=pipeline, mode="workflow",
                metadata=metadata, rows=rows, verify_result=verify_result_r,
            )

    @staticmethod
    def _build_read_result(
        *,
        connector: str,
        pipeline: str,
        metadata: dict[str, Any],
        rows: Any,
        verify_result: dict[str, Any],
        mode: str = "read",
    ) -> dict[str, Any]:
        verification_state = "verified" if bool(verify_result.get("ok", False)) else "degraded"
        return {
            "pipeline_id": f"{connector}.{mode}.{pipeline}",
            "intent_signature": metadata.get("intent_signature", ""),
            "rows": rows,
            "verify_result": verify_result,
            "verification_state": verification_state,
        }

    @staticmethod
    def _build_write_result(
        *,
        connector: str,
        pipeline: str,
        metadata: dict[str, Any],
        action_result: Any,
        verify_result: dict[str, Any],
        stop_triggered: bool,
        rollback_executed: bool,
        rollback_result: dict[str, Any] | None,
        mode: str = "write",
    ) -> dict[str, Any]:
        verification_state = "verified" if bool(verify_result.get("ok", False)) else "degraded"
        policy = str(metadata.get("rollback_or_stop_policy", "stop"))
        return {
            "pipeline_id": f"{connector}.{mode}.{pipeline}",
            "intent_signature": metadata.get("intent_signature", ""),
            "action_result": action_result,
            "verify_result": verify_result,
            "verification_state": verification_state,
            "rollback_or_stop_policy": policy,
            "policy_enforced": verification_state == "degraded",
            "stop_triggered": stop_triggered,
            "rollback_executed": rollback_executed,
            "rollback_result": rollback_result,
        }

    def has_persistent_hooks(self, intent_signature: str) -> bool:
        """Return True when a pipeline defines start()/stop() hooks."""
        connector, mode, pipeline = self._parse_intent_signature(intent_signature)
        _, module = self._load_pipeline(connector, mode, pipeline)
        return callable(getattr(module, "start", None)) or callable(
            getattr(module, "stop", None)
        )

    def start_pipeline(self, intent_signature: str, ctx: dict[str, Any] | None = None) -> bool:
        """Run optional start() hook for a pipeline; returns whether hook existed."""
        connector, mode, pipeline = self._parse_intent_signature(intent_signature)
        metadata, module = self._load_pipeline(connector, mode, pipeline)
        start_fn = getattr(module, "start", None)
        if not callable(start_fn):
            return False
        start_fn(
            ctx={
                **(ctx or {}),
                "metadata": metadata,
                "connector": connector,
                "mode": mode,
                "pipeline": pipeline,
            }
        )
        return True

    def stop_pipeline(self, intent_signature: str, ctx: dict[str, Any] | None = None) -> bool:
        """Run optional stop() hook for a pipeline; returns whether hook existed."""
        connector, mode, pipeline = self._parse_intent_signature(intent_signature)
        metadata, module = self._load_pipeline(connector, mode, pipeline)
        stop_fn = getattr(module, "stop", None)
        if not callable(stop_fn):
            return False
        stop_fn(
            ctx={
                **(ctx or {}),
                "metadata": metadata,
                "connector": connector,
                "mode": mode,
                "pipeline": pipeline,
            }
        )
        return True

    def _load_pipeline_source(
        self, connector: str, mode: str, pipeline: str
    ) -> tuple[dict[str, Any], str]:
        """Return (metadata, py_source_text) without importing the module.

        Used by the daemon to send pipeline code to a remote runner as inline
        icc_exec code, keeping connector assets local and the runner stateless.
        """
        metadata, _, py_source = self._load_pipeline_artifacts(
            connector=connector,
            mode=mode,
            pipeline=pipeline,
            need_module=False,
            need_source=True,
        )
        return metadata, py_source

    def _load_pipeline(
        self, connector: str, mode: str, pipeline: str
    ) -> tuple[dict[str, Any], Any]:
        metadata, module, _ = self._load_pipeline_artifacts(
            connector=connector,
            mode=mode,
            pipeline=pipeline,
            need_module=True,
            need_source=False,
        )
        return metadata, module

    def invalidate_cache(
        self,
        *,
        connector: str | None = None,
        mode: str | None = None,
        pipeline: str | None = None,
    ) -> None:
        with self._cache_lock:
            if connector is None and mode is None and pipeline is None:
                self._pipeline_cache.clear()
                return
            keep: dict[tuple[str, str, str], dict[str, Any]] = {}
            for key, value in self._pipeline_cache.items():
                k_connector, k_mode, k_pipeline = key
                if connector is not None and k_connector != connector:
                    keep[key] = value
                    continue
                if mode is not None and k_mode != mode:
                    keep[key] = value
                    continue
                if pipeline is not None and k_pipeline != pipeline:
                    keep[key] = value
                    continue
            self._pipeline_cache = keep

    def _resolve_pipeline_paths(
        self, connector: str, mode: str, pipeline: str
    ) -> tuple[Path, Path | None]:
        """Return (meta_path, code_path). code_path is None for YAML scenario pipelines."""
        for connector_root in self._connector_roots:
            base = connector_root / connector / "pipelines" / mode
            meta_path = base / f"{pipeline}.yaml"
            code_path = base / f"{pipeline}.py"
            if meta_path.exists() and code_path.exists():
                return meta_path, code_path
            if meta_path.exists() and not code_path.exists():
                try:
                    import yaml  # type: ignore
                    data = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and isinstance(data.get("steps"), list):
                        return meta_path, None
                except Exception:
                    pass
        searched = ", ".join(str(r / connector) for r in self._connector_roots)
        raise PipelineMissingError(
            connector=connector, mode=mode, pipeline=pipeline, searched=searched
        )

    def _load_pipeline_artifacts(
        self,
        *,
        connector: str,
        mode: str,
        pipeline: str,
        need_module: bool,
        need_source: bool,
    ) -> tuple[dict[str, Any], Any | None, str]:
        meta_path, code_path = self._resolve_pipeline_paths(connector, mode, pipeline)

        # YAML scenario: no Python module
        if code_path is None:
            metadata = self._load_metadata(meta_path)
            return metadata, None, ""

        key = (connector, mode, pipeline)
        meta_mtime_ns = meta_path.stat().st_mtime_ns
        code_mtime_ns = code_path.stat().st_mtime_ns
        metadata: dict[str, Any]
        module: Any | None = None
        source = ""
        with self._cache_lock:
            entry = self._pipeline_cache.get(key)
            valid = bool(
                entry
                and entry.get("meta_mtime_ns") == meta_mtime_ns
                and entry.get("code_mtime_ns") == code_mtime_ns
            )
            if valid:
                metadata = dict(entry["metadata"])
                module = entry.get("module")
                source = str(entry.get("source") or "")
                needs_module_load = need_module and module is None
                needs_source_load = need_source and not source
            else:
                needs_module_load = need_module
                needs_source_load = need_source
                metadata = self._load_metadata(meta_path)
        if not valid:
            if need_module:
                module = self._load_module(code_path, f"emerge_{connector}_{mode}_{pipeline}")
            if need_source:
                source = code_path.read_text(encoding="utf-8")
            with self._cache_lock:
                self._pipeline_cache[key] = {
                    "meta_mtime_ns": meta_mtime_ns,
                    "code_mtime_ns": code_mtime_ns,
                    "metadata": dict(metadata),
                    "module": module,
                    "source": source,
                }
        elif needs_module_load or needs_source_load:
            loaded_module = module
            loaded_source = source
            if needs_module_load:
                loaded_module = self._load_module(
                    code_path, f"emerge_{connector}_{mode}_{pipeline}"
                )
            if needs_source_load:
                loaded_source = code_path.read_text(encoding="utf-8")
            with self._cache_lock:
                entry = self._pipeline_cache.get(key)
                if entry and entry.get("meta_mtime_ns") == meta_mtime_ns and entry.get("code_mtime_ns") == code_mtime_ns:
                    if needs_module_load:
                        entry["module"] = loaded_module
                    if needs_source_load:
                        entry["source"] = loaded_source
                    module = entry.get("module")
                    source = str(entry.get("source") or "")
                else:
                    module = loaded_module
                    source = loaded_source
        return metadata, module, source

    @staticmethod
    def _parse_intent_signature(intent_signature: str) -> tuple[str, str, str]:
        parts = intent_signature.split(".", 2)
        if len(parts) != 3:
            raise ValueError(
                f"invalid intent_signature {intent_signature!r}: expected connector.mode.name"
            )
        connector, mode, pipeline = parts
        if mode not in ("read", "write", "workflow"):
            raise ValueError(
                f"invalid intent_signature mode {mode!r}: expected 'read', 'write', or 'workflow'"
            )
        PipelineEngine._validate_path_segment(connector, "connector")
        PipelineEngine._validate_path_segment(pipeline, "pipeline")
        return connector, mode, pipeline

    @staticmethod
    def _validate_metadata(path: Path, data: dict[str, Any]) -> None:
        errors: list[str] = []
        if not str(data.get("intent_signature", "")).strip():
            errors.append("intent_signature (required, non-empty string)")
        policy = str(data.get("rollback_or_stop_policy", ""))
        if policy not in ("stop", "rollback"):
            errors.append("rollback_or_stop_policy (must be 'stop' or 'rollback')")

        has_steps = isinstance(data.get("steps"), list) and len(data["steps"]) > 0
        has_read = isinstance(data.get("read_steps"), list) and len(data["read_steps"]) > 0
        has_write = isinstance(data.get("write_steps"), list) and len(data["write_steps"]) > 0

        if has_steps:
            pass  # YAML scenario — verify is inline, no function refs needed
        elif not (has_read ^ has_write):
            errors.append("read_steps or write_steps (exactly one required, non-empty list)")
        else:
            has_verify = isinstance(data.get("verify_steps"), list) and len(data["verify_steps"]) > 0
            if not has_verify:
                errors.append("verify_steps (required, non-empty list)")

        if errors:
            raise ValueError(
                f"pipeline metadata invalid at {path}: missing/invalid fields: {', '.join(errors)}"
            )

    @staticmethod
    def _load_metadata(path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        if text.lstrip().startswith(("{", "[")):
            raise ValueError(
                f"pipeline metadata must be YAML (JSON-style content is not allowed) at {path}"
            )
        try:
            import yaml  # type: ignore
        except ImportError:
            raise RuntimeError(
                "PyYAML is required to load pipeline metadata. Install with: pip install pyyaml"
            )
        loaded = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise ValueError(f"pipeline metadata must be a YAML object at {path}")
        PipelineEngine._validate_metadata(path, loaded)
        return loaded

    @staticmethod
    def _load_module(path: Path, module_name: str) -> Any:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Failed to load module spec from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
