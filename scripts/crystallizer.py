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

import ast
import json
import os
import tempfile
import textwrap
from pathlib import Path
from typing import Any


def _code_assigns_name(code: str, name: str) -> bool:
    """Return True when `name` appears as an assignment target in `code`.

    Covers plain ``name = ...``, augmented ``name += ...``, and
    ``globals()[name] = ...`` style writes. Parse errors default to False
    so we refuse to crystallize WAL that wouldn't even compile."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.found = False

        def _check_target(self, node: ast.AST) -> None:
            if isinstance(node, ast.Name) and node.id == name:
                self.found = True
            elif isinstance(node, (ast.Tuple, ast.List)):
                for elt in node.elts:
                    self._check_target(elt)

        def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
            for tgt in node.targets:
                self._check_target(tgt)
            self.generic_visit(node)

        def visit_AugAssign(self, node: ast.AugAssign) -> None:  # noqa: N802
            self._check_target(node.target)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
            self._check_target(node.target)
            self.generic_visit(node)

        def visit_Subscript(self, node: ast.Subscript) -> None:  # noqa: N802
            # globals()["__action"] = ... or __builtins__["__action"] = ...
            if (
                isinstance(node.slice, ast.Constant)
                and node.slice.value == name
            ):
                self.found = True
            self.generic_visit(node)

    v = _Visitor()
    v.visit(tree)
    return v.found


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

        _registry = IntentRegistry.load(self._state_root)

        # Precondition: WAL code must set the return variable this mode expects
        # (__result for read, __action for write). Otherwise the crystallized
        # pipeline would either crash or return a zombie "ok" — both of which
        # strand the intent at stable while LLM pays full cost on every call.
        required_var = "__result" if mode == "read" else "__action"
        if not _code_assigns_name(best_code, required_var):
            reason = (
                f"WAL code for {intent_signature!r} never assigns {required_var}; "
                f"crystallization refused. Re-run icc_exec with a code body that "
                f"sets {required_var} before icc_crystallize promotes the pipeline."
            )
            try:
                if intent_signature in _registry["intents"]:
                    _registry["intents"][intent_signature]["synthesis_skipped_reason"] = (
                        f"missing_{required_var}_assignment"
                    )
                    _registry["intents"][intent_signature].pop("synthesis_ready", None)
                    IntentRegistry.save(self._state_root, _registry)
            except Exception:
                pass
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"icc_crystallize: {reason}"}],
            }

        # --- generate pipeline source ---
        ts = int(_time.time())
        indented = textwrap.indent(best_code, "    ")

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
        """Best-effort enqueue for Claude Code lead-agent synthesis.

        The old auto path wrote verbatim WAL code with textwrap.indent. New
        artifacts must be distilled by the lead agent and submitted back for
        smoke testing, so this method only emits a pending job.
        """
        from scripts.policy_config import events_root, resolve_connector_root
        from scripts.synthesis_coordinator import SynthesisCoordinator
        try:
            target_root = resolve_connector_root()
            py_path = target_root / connector / "pipelines" / mode / f"{pipeline_name}.py"
            if py_path.exists():
                return
            event_path = events_root(self._state_root) / "events.jsonl"
            self._append_event(
                event_path,
                {
                    "type": "crystallizer_deprecated",
                    "intent_signature": intent_signature,
                    "connector": connector,
                    "mode": mode,
                    "pipeline_name": pipeline_name,
                    "message": "auto_crystallize no longer writes verbatim pipelines; forwarding to Claude Code synthesis",
                },
            )
            SynthesisCoordinator(
                state_root=self._state_root,
                connector_root=target_root,
                exec_tool=lambda _args: {"isError": True, "error": "auto_crystallize enqueue only"},
            ).enqueue_forward_synthesis(
                intent_signature=intent_signature,
                connector=connector,
                pipeline_name=pipeline_name,
                mode=mode,
                target_profile=target_profile,
                event_path=event_path,
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
        """Write a _pending stub from span actions. Returns path or None.

        Routing:
        - Multi-tool spans (len(actions) > 1) → generate_yaml_span_skeleton → .yaml
        - Single-tool spans                   → .py skeleton (existing path)
        """
        from scripts.policy_config import resolve_connector_root
        try:
            parts = intent_signature.split(".", 2)
            if len(parts) != 3:
                return None
            connector, mode, pipeline_name = parts
            target_root = connector_root or resolve_connector_root()

            actions = span.get("actions", [])

            # Multi-tool spans get a YAML pipeline skeleton.
            if len(actions) > 1:
                return self.generate_yaml_span_skeleton(
                    intent_signature=intent_signature,
                    span=span,
                    connector_root=target_root,
                )

            # Single-tool span: keep existing .py path.
            pending_dir = target_root / connector / "pipelines" / mode / "_pending"
            pending_dir.mkdir(parents=True, exist_ok=True)
            skeleton_path = pending_dir / f"{pipeline_name}.py"
            if skeleton_path.exists():
                return skeleton_path

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

    def generate_yaml_span_skeleton(
        self,
        *,
        intent_signature: str,
        span: dict,
        connector_root: "Path | None" = None,
    ) -> "Path | None":
        """Write a _pending/<name>.yaml pipeline skeleton from multi-tool span actions.

        Each action becomes one placeholder ``connector_call`` step annotated with
        hints from ``args_snapshot`` and ``result_summary``.  The file is never
        overwritten if it already exists.  Returns the path or None on error.
        """
        from scripts.policy_config import resolve_connector_root
        try:
            import yaml  # type: ignore

            parts = intent_signature.split(".", 2)
            if len(parts) != 3:
                return None
            connector, mode, pipeline_name = parts
            target_root = connector_root or resolve_connector_root()

            pending_dir = target_root / connector / "pipelines" / mode / "_pending"
            pending_dir.mkdir(parents=True, exist_ok=True)
            skeleton_path = pending_dir / f"{pipeline_name}.yaml"
            if skeleton_path.exists():
                return skeleton_path

            actions = span.get("actions", [])

            # Build one step per action.
            steps = []
            for a in actions:
                hint_sig = (a.get("args_snapshot") or {}).get("intent_signature", "")
                result_hint = a.get("result_summary") or {}
                step: dict[str, Any] = {
                    "type": "connector_call",
                    # Hint at which child intent this step should invoke; operator
                    # should replace with the real intent after reviewing.
                    "intent": hint_sig or f"TODO.{mode}.step{a.get('seq', '?')}",
                    # Preserve seq / side-effect metadata as comments via a
                    # human-readable annotation field (not executed by PipelineEngine).
                    "_annotation": (
                        f"seq={a.get('seq', '?')} tool={a.get('tool_name', '?')} "
                        f"side_effects={a.get('has_side_effects', '?')} "
                        + (f"result={json.dumps(result_hint, ensure_ascii=False)}" if result_hint else "")
                    ).strip(),
                }
                steps.append(step)

            if not steps:
                steps = [{"type": "connector_call", "intent": f"TODO.{mode}.step0"}]

            yaml_data: dict[str, Any] = {
                "intent_signature": intent_signature,
                "rollback_or_stop_policy": "stop",
                "steps": steps,
                "verify": [{"type": "derive", "from": "steps"}],
                "rollback": [{"type": "derive", "from": "steps"}],
            }

            yaml_src = IndentedSafeDumper.dump_yaml(yaml_data)
            # Prepend a comment header so the operator knows this is a skeleton.
            header = (
                f"# auto-generated YAML skeleton from span: {intent_signature}\n"
                f"# Review each step's intent_signature and remove _annotation fields\n"
                f"# before calling icc_span_approve.\n"
            )
            self._atomic_write_text(skeleton_path, header + yaml_src, prefix=".skeleton-")
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
    def _append_event(path: Path, event: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

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
                f"    return __result  # exec code must set __result = [{{...}}]; crystallizer enforces this precondition\n\n\n"
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
                f"    return __action  # exec code must set __action = {{\"ok\": True, ...}}; crystallizer enforces this precondition\n\n\n"
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
