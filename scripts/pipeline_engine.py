from __future__ import annotations

import importlib.util
import json
import os
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

    def run_read(self, args: dict[str, Any]) -> dict[str, Any]:
        connector = args.get("connector", "").strip()
        pipeline = args.get("pipeline", "").strip()
        if not connector or not pipeline:
            raise ValueError(
                f"run_read: 'connector' and 'pipeline' are required (got connector={connector!r}, pipeline={pipeline!r})"
            )
        metadata, module = self._load_pipeline(connector, "read", pipeline)
        rows = module.run_read(metadata=metadata, args=args)
        verify_result = {"ok": True}
        verify_fn = getattr(module, "verify_read", None)
        if callable(verify_fn):
            verify_result = verify_fn(metadata=metadata, args=args, rows=rows)
            if not isinstance(verify_result, dict):
                raise ValueError("verify_read must return an object")
        verification_state = "verified" if bool(verify_result.get("ok", False)) else "degraded"
        return {
            "pipeline_id": f"{connector}.read.{pipeline}",
            "intent_signature": metadata.get("intent_signature", ""),
            "rows": rows,
            "verify_result": verify_result,
            "verification_state": verification_state,
        }

    def run_write(self, args: dict[str, Any]) -> dict[str, Any]:
        connector = args.get("connector", "").strip()
        pipeline = args.get("pipeline", "").strip()
        if not connector or not pipeline:
            raise ValueError(
                f"run_write: 'connector' and 'pipeline' are required (got connector={connector!r}, pipeline={pipeline!r})"
            )
        metadata, module = self._load_pipeline(connector, "write", pipeline)
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
        return {
            "pipeline_id": f"{connector}.write.{pipeline}",
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

    def _load_pipeline_source(
        self, connector: str, mode: str, pipeline: str
    ) -> tuple[dict[str, Any], str]:
        """Return (metadata, py_source_text) without importing the module.

        Used by the daemon to send pipeline code to a remote runner as inline
        icc_exec code, keeping connector assets local and the runner stateless.
        """
        for connector_root in self._connector_roots:
            base = connector_root / connector / "pipelines" / mode
            meta_path = base / f"{pipeline}.yaml"
            code_path = base / f"{pipeline}.py"
            if meta_path.exists() and code_path.exists():
                break
        else:
            searched = ", ".join(str(r / connector) for r in self._connector_roots)
            raise PipelineMissingError(
                connector=connector, mode=mode, pipeline=pipeline, searched=searched
            )
        metadata = self._load_metadata(meta_path)
        py_source = code_path.read_text(encoding="utf-8")
        return metadata, py_source

    def _load_pipeline(
        self, connector: str, mode: str, pipeline: str
    ) -> tuple[dict[str, Any], Any]:
        for connector_root in self._connector_roots:
            base = connector_root / connector / "pipelines" / mode
            meta_path = base / f"{pipeline}.yaml"
            code_path = base / f"{pipeline}.py"
            if meta_path.exists() and code_path.exists():
                break
        else:
            searched = ", ".join(str(r / connector) for r in self._connector_roots)
            raise PipelineMissingError(
                connector=connector, mode=mode, pipeline=pipeline, searched=searched
            )

        metadata = self._load_metadata(meta_path)
        module = self._load_module(code_path, f"emerge_{connector}_{mode}_{pipeline}")
        return metadata, module

    @staticmethod
    def _validate_metadata(path: Path, data: dict[str, Any]) -> None:
        errors: list[str] = []
        if not str(data.get("intent_signature", "")).strip():
            errors.append("intent_signature (required, non-empty string)")
        policy = str(data.get("rollback_or_stop_policy", ""))
        if policy not in ("stop", "rollback"):
            errors.append("rollback_or_stop_policy (must be 'stop' or 'rollback')")
        has_read = isinstance(data.get("read_steps"), list) and len(data["read_steps"]) > 0
        has_write = isinstance(data.get("write_steps"), list) and len(data["write_steps"]) > 0
        if not (has_read ^ has_write):
            errors.append("read_steps or write_steps (exactly one required, non-empty list)")
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
        loaded = None
        try:
            import yaml  # type: ignore
            loaded = yaml.safe_load(text)
            # yaml.YAMLError and other yaml-specific errors propagate from here
        except ImportError:
            pass  # yaml not installed — fall through to JSON
        if loaded is None:
            loaded = json.loads(text)
        if not isinstance(loaded, dict):
            raise ValueError(f"pipeline metadata must be a JSON/YAML object at {path}")
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
