"""Pipeline crystallization — WAL scan → code-gen → atomic file write.

PipelineCrystallizer is the single entry-point for three related operations:
  - crystallize: scan WAL for best exec, generate .py + .yaml pipeline files
  - auto_crystallize: best-effort wrapper (skips silently if pipeline exists)
  - generate_span_skeleton: write a _pending/<name>.py stub from span actions

All three produce pipeline artifacts under the active connector root
(EMERGE_CONNECTOR_ROOT env var or ~/.emerge/connectors).

IndentedSafeDumper is also exported for callers that need YAML generation
(e.g. icc_span_approve in emerge_daemon.py).
"""
from __future__ import annotations

import os
import tempfile
import textwrap
from pathlib import Path
from typing import Any


class IndentedSafeDumper:
    @staticmethod
    def dump_yaml(payload: dict[str, Any]) -> str:
        import yaml  # type: ignore

        class _Dumper(yaml.SafeDumper):
            def increase_indent(self, flow=False, indentless=False):  # type: ignore[override]
                return super().increase_indent(flow, False)

        return yaml.dump(payload, Dumper=_Dumper, sort_keys=False, allow_unicode=True)


class PipelineCrystallizer:
    """Handles WAL-to-pipeline code generation for a given state root."""

    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def crystallize(
        self,
        *,
        intent_signature: str,
        connector: str,
        pipeline_name: str,
        mode: str,
        target_profile: str = "default",
        persistent: bool = False,
    ) -> dict[str, Any]:
        """Scan the WAL for the most recent synthesizable exec, write .py + .yaml."""
        import json
        import time as _time
        from scripts.policy_config import derive_profile_token, resolve_connector_root, sessions_root
        from scripts.intent_registry import IntentRegistry

        # --- find synthesizable WAL entry ---
        normalized = (target_profile or "default").strip() or "default"
        profile_suffix = "" if normalized == "default" else f"__{derive_profile_token(normalized)}"

        best_code: str | None = None
        best_ts: int = 0
        sessions_dir = sessions_root(self._state_root)
        if sessions_dir.exists():
            for session_dir in sorted(sessions_dir.iterdir()):
                if not session_dir.is_dir():
                    continue
                dir_name = session_dir.name
                if profile_suffix:
                    if not dir_name.endswith(profile_suffix):
                        continue
                else:
                    if "__" in dir_name:
                        continue
                wal_path = session_dir / "wal.jsonl"
                if not wal_path.exists():
                    continue
                with wal_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (
                            entry.get("status") == "success"
                            and not entry.get("no_replay", False)
                            and entry.get("metadata", {}).get("intent_signature") == intent_signature
                        ):
                            ts = int(entry.get("finished_at_ms", 0))
                            if ts > best_ts:
                                best_ts = ts
                                best_code = str(entry.get("code", "")).strip()

        if not best_code:
            return {
                "isError": True,
                "content": [{"type": "text", "text": (
                    f"icc_crystallize: no synthesizable WAL entry found for "
                    f"intent_signature='{intent_signature}'. Run icc_exec with "
                    f"intent_signature='{intent_signature}' and no_replay=false first."
                )}],
            }

        # --- generate pipeline source ---
        ts = int(_time.time())
        indented = textwrap.indent(best_code, "    ")

        _registry = IntentRegistry.load(self._state_root)
        description = str(_registry["intents"].get(intent_signature, {}).get("description", "")).strip()

        py_src, yaml_data = self._build_pipeline_sources(
            intent_signature=intent_signature,
            mode=mode,
            indented_code=indented,
            ts=ts,
            description=description,
            persistent=persistent,
        )

        try:
            yaml_src = IndentedSafeDumper.dump_yaml(yaml_data)
        except ImportError as exc:
            raise RuntimeError(
                "PyYAML is required to crystallize pipeline metadata. Install with: pip install pyyaml"
            ) from exc

        # --- write files ---
        target_root = resolve_connector_root()
        pipeline_dir = target_root / connector / "pipelines" / mode
        self._check_path_in_root(pipeline_dir, target_root, label=f"connector={connector!r}, mode={mode!r}")
        pipeline_dir.mkdir(parents=True, exist_ok=True)

        py_path = pipeline_dir / f"{pipeline_name}.py"
        yaml_path = pipeline_dir / f"{pipeline_name}.yaml"
        for _check_path in (py_path, yaml_path):
            self._check_path_in_root(_check_path, target_root, label=f"pipeline_name={pipeline_name!r}")

        for dest_path, content in ((py_path, py_src), (yaml_path, yaml_src)):
            self._atomic_write_text(dest_path, content, prefix=".crystallize-")

        # Clear synthesis_ready flag
        if intent_signature in _registry["intents"]:
            _registry["intents"][intent_signature].pop("synthesis_ready", None)
            IntentRegistry.save(self._state_root, _registry)

        preview_lines = py_src.splitlines()[:20]
        intent_sig = f"{connector}.{mode}.{pipeline_name}"
        next_step = (
            f"Pipeline crystallized. Use icc_span_open to execute via the bridge:\n"
            f"  icc_span_open intent_signature={intent_sig!r}\n"
            "Do NOT call icc_exec for this intent again — the pipeline handles it."
        )
        payload = {
            "ok": True,
            "py_path": str(py_path),
            "yaml_path": str(yaml_path),
            "code_preview": "\n".join(preview_lines),
            "next_step": next_step,
        }
        return {
            "isError": False,
            "structuredContent": payload,
            "content": [{"type": "text", "text": json.dumps(payload)}],
        }

    def auto_crystallize(
        self,
        *,
        intent_signature: str,
        connector: str,
        pipeline_name: str,
        mode: str,
        target_profile: str = "default",
        persistent: bool = False,
    ) -> None:
        """Best-effort crystallize — skips silently if pipeline already exists."""
        from scripts.policy_config import resolve_connector_root
        try:
            target_root = resolve_connector_root()
            py_path = target_root / connector / "pipelines" / mode / f"{pipeline_name}.py"
            if py_path.exists():
                return
            self.crystallize(
                intent_signature=intent_signature,
                connector=connector,
                pipeline_name=pipeline_name,
                mode=mode,
                target_profile=target_profile,
                persistent=persistent,
            )
        except Exception:
            pass

    def generate_span_skeleton(
        self,
        *,
        intent_signature: str,
        span: dict,
        connector_root: "Path | None" = None,
    ) -> "Path | None":
        """Write a _pending/<name>.py stub from span actions. Returns path or None."""
        from scripts.policy_config import resolve_connector_root
        try:
            parts = intent_signature.split(".", 2)
            if len(parts) != 3:
                return None
            connector, mode, pipeline_name = parts
            target_root = connector_root or resolve_connector_root()
            pending_dir = target_root / connector / "pipelines" / mode / "_pending"
            pending_dir.mkdir(parents=True, exist_ok=True)
            skeleton_path = pending_dir / f"{pipeline_name}.py"
            if skeleton_path.exists():
                return skeleton_path

            actions = span.get("actions", [])
            is_read = mode == "read"
            call_lines = []
            for a in actions:
                tool = a.get("tool_name", "unknown_tool")
                call_lines.append(
                    f"    # seq={a.get('seq', '?')}: {tool} was called here\n"
                    f"    raise NotImplementedError('implement: {tool} equivalent')"
                )
            if not call_lines:
                call_lines = ["    raise NotImplementedError('implement pipeline body')"]
            body = "\n".join(call_lines)

            if is_read:
                skeleton = textwrap.dedent(f"""\
                    # auto-generated from span: {intent_signature}
                    # Review and implement before calling icc_span_approve.

                    def run_read(metadata, args):
                    {body}
                        return []  # return list of row dicts

                    def verify_read(metadata, args, rows):
                        return {{"ok": isinstance(rows, list)}}
                """)
            else:
                skeleton = textwrap.dedent(f"""\
                    # auto-generated from span: {intent_signature}
                    # Review and implement before calling icc_span_approve.
                    # verify_write is REQUIRED by PipelineEngine.

                    def run_write(metadata, args):
                    {body}
                        return {{"ok": True}}

                    def verify_write(metadata, args, action_result):
                        raise NotImplementedError('implement verify_write')

                    def rollback(metadata, args, action_result):
                        pass  # optional
                """)

            self._atomic_write_text(skeleton_path, skeleton, prefix=".skeleton-")
            return skeleton_path
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _check_path_in_root(path: Path, root: Path, *, label: str) -> None:
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError as e:
            raise ValueError(
                f"icc_crystallize: path escapes connector root ({label})"
            ) from e

    @staticmethod
    def _atomic_write_text(dest: Path, content: str, *, prefix: str = ".tmp-") -> None:
        fd, tmp = tempfile.mkstemp(prefix=prefix, dir=str(dest.parent))
        tmp_path = tmp
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, dest)
            tmp_path = ""
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    @staticmethod
    def _build_pipeline_sources(
        *,
        intent_signature: str,
        mode: str,
        indented_code: str,
        ts: int,
        description: str,
        persistent: bool = False,
    ) -> tuple[str, dict]:
        header = (
            f"# auto-generated by icc_crystallize — review before promoting\n"
            f"# intent_signature: {intent_signature}\n"
            f"# synthesized_at: {ts}\n\n"
        )
        if mode == "read":
            py_src = (
                header
                + f"def run_read(metadata, args):\n"
                f"    __args = args  # compat with exec __args scope\n"
                f"    # --- CRYSTALLIZED ---\n"
                f"{indented_code}\n"
                f"    # --- END ---\n"
                f"    return locals().get('__result', [])  # exec code sets __result; fallback avoids NameError on auto-activate\n\n\n"
                f"def verify_read(metadata, args, rows):\n"
                f"    return {{\"ok\": bool(rows)}}\n"
            )
            if persistent:
                py_src += (
                    "\n\ndef start(ctx):\n"
                    "    # Optional persistent hook; implement runner-side listener setup.\n"
                    "    return None\n\n"
                    "def stop(ctx):\n"
                    "    # Optional persistent hook; implement cleanup.\n"
                    "    return None\n"
                )
            yaml_data: dict[str, Any] = {
                "intent_signature": intent_signature,
                "rollback_or_stop_policy": "stop",
                "read_steps": ["run_read"],
                "verify_steps": ["verify_read"],
                "synthesized": True,
                "synthesized_at": ts,
                "persistent": bool(persistent),
            }
        else:
            py_src = (
                header
                + f"def run_write(metadata, args):\n"
                f"    __args = args  # compat with exec __args scope\n"
                f"    # --- CRYSTALLIZED ---\n"
                f"{indented_code}\n"
                f"    # --- END ---\n"
                f"    return locals().get('__action', {{\"ok\": True}})  # exec code sets __action; fallback avoids NameError on auto-activate\n\n\n"
                f"def verify_write(metadata, args, action_result):\n"
                f"    return {{\"ok\": bool(action_result.get(\"ok\"))}}\n"
            )
            if persistent:
                py_src += (
                    "\n\ndef start(ctx):\n"
                    "    # Optional persistent hook; implement runner-side listener setup.\n"
                    "    return None\n\n"
                    "def stop(ctx):\n"
                    "    # Optional persistent hook; implement cleanup.\n"
                    "    return None\n"
                )
            yaml_data = {
                "intent_signature": intent_signature,
                "rollback_or_stop_policy": "stop",
                "write_steps": ["run_write"],
                "verify_steps": ["verify_write"],
                "synthesized": True,
                "synthesized_at": ts,
                "persistent": bool(persistent),
            }
        if description:
            yaml_data["description"] = description
        return py_src, yaml_data
