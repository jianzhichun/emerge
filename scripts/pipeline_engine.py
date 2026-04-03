from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any


class PipelineEngine:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).resolve().parents[1]

    def run_read(self, args: dict[str, Any]) -> dict[str, Any]:
        connector = args.get("connector", "mock")
        pipeline = args.get("pipeline", "layers")
        metadata, module = self._load_pipeline(connector, "read", pipeline)
        rows = module.run_read(metadata=metadata, args=args)
        return {
            "pipeline_id": f"{connector}.read.{pipeline}",
            "intent_signature": metadata.get("intent_signature", ""),
            "rows": rows,
            "verification_state": "verified",
        }

    def run_write(self, args: dict[str, Any]) -> dict[str, Any]:
        connector = args.get("connector", "mock")
        pipeline = args.get("pipeline", "add-wall")
        metadata, module = self._load_pipeline(connector, "write", pipeline)
        action_result = module.run_write(metadata=metadata, args=args)
        verify_result = module.verify_write(
            metadata=metadata, args=args, action_result=action_result
        )
        verification_state = "verified" if verify_result.get("ok") else "degraded"
        return {
            "pipeline_id": f"{connector}.write.{pipeline}",
            "intent_signature": metadata.get("intent_signature", ""),
            "action_result": action_result,
            "verify_result": verify_result,
            "verification_state": verification_state,
            "rollback_or_stop_policy": metadata.get("rollback_or_stop_policy", "stop"),
        }

    def _load_pipeline(
        self, connector: str, mode: str, pipeline: str
    ) -> tuple[dict[str, Any], Any]:
        base = self.root / "connectors" / connector / "pipelines" / mode
        meta_path = base / f"{pipeline}.yaml"
        code_path = base / f"{pipeline}.py"
        if not meta_path.exists():
            raise FileNotFoundError(f"Missing pipeline metadata: {meta_path}")
        if not code_path.exists():
            raise FileNotFoundError(f"Missing pipeline action: {code_path}")

        metadata = self._load_metadata(meta_path)
        module = self._load_module(code_path, f"emerge_{connector}_{mode}_{pipeline}")
        return metadata, module

    @staticmethod
    def _load_metadata(path: Path) -> dict[str, Any]:
        text = path.read_text(encoding="utf-8")
        try:
            import yaml  # type: ignore

            loaded = yaml.safe_load(text)
            if not isinstance(loaded, dict):
                raise ValueError("metadata must be an object")
            return loaded
        except Exception:
            loaded = json.loads(text)
            if not isinstance(loaded, dict):
                raise ValueError("metadata must be a JSON object")
            return loaded

    @staticmethod
    def _load_module(path: Path, module_name: str) -> Any:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Failed to load module spec from {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
