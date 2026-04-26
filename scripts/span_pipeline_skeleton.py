"""Mechanism for writing pending pipeline skeletons from recorded spans."""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

from scripts.pipeline_artifacts import IndentedSafeDumper, atomic_write_text


class SpanPipelineSkeletonWriter:
    """Writes conservative `_pending` skeletons from span action facts."""

    def generate_span_skeleton(
        self,
        *,
        intent_signature: str,
        span: dict[str, Any],
        connector_root: Path,
    ) -> Path | None:
        """Write a pending skeleton and return its path, or None for invalid input."""
        parts = intent_signature.split(".", 2)
        if len(parts) != 3:
            return None
        connector, mode, pipeline_name = parts
        actions = span.get("actions", [])

        if len(actions) > 1:
            return self.generate_yaml_span_skeleton(
                intent_signature=intent_signature,
                span=span,
                connector_root=connector_root,
            )

        pending_dir = connector_root / connector / "pipelines" / mode / "_pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        skeleton_path = pending_dir / f"{pipeline_name}.py"
        if skeleton_path.exists():
            return skeleton_path

        body = self._single_tool_body(actions)
        if mode == "read":
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

        atomic_write_text(skeleton_path, skeleton, prefix=".skeleton-")
        return skeleton_path

    def generate_yaml_span_skeleton(
        self,
        *,
        intent_signature: str,
        span: dict[str, Any],
        connector_root: Path,
    ) -> Path | None:
        """Write a pending YAML skeleton from a multi-tool span."""
        parts = intent_signature.split(".", 2)
        if len(parts) != 3:
            return None
        connector, mode, pipeline_name = parts

        pending_dir = connector_root / connector / "pipelines" / mode / "_pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        skeleton_path = pending_dir / f"{pipeline_name}.yaml"
        if skeleton_path.exists():
            return skeleton_path

        steps = []
        for action in span.get("actions", []):
            hint_sig = (action.get("args_snapshot") or {}).get("intent_signature", "")
            result_hint = action.get("result_summary") or {}
            steps.append(
                {
                    "type": "connector_call",
                    "intent": hint_sig or f"TODO.{mode}.step{action.get('seq', '?')}",
                    "_annotation": (
                        f"seq={action.get('seq', '?')} tool={action.get('tool_name', '?')} "
                        f"side_effects={action.get('has_side_effects', '?')} "
                        + (f"result={json.dumps(result_hint, ensure_ascii=False)}" if result_hint else "")
                    ).strip(),
                }
            )

        if not steps:
            steps = [{"type": "connector_call", "intent": f"TODO.{mode}.step0"}]

        yaml_data: dict[str, Any] = {
            "intent_signature": intent_signature,
            "rollback_or_stop_policy": "stop",
            "steps": steps,
            "verify": [{"type": "derive", "from": "steps"}],
            "rollback": [{"type": "derive", "from": "steps"}],
        }
        header = (
            f"# auto-generated YAML skeleton from span: {intent_signature}\n"
            f"# Review each step's intent_signature and remove _annotation fields\n"
            f"# before calling icc_span_approve.\n"
        )
        atomic_write_text(
            skeleton_path,
            header + IndentedSafeDumper.dump_yaml(yaml_data),
            prefix=".skeleton-",
        )
        return skeleton_path

    @staticmethod
    def _single_tool_body(actions: Any) -> str:
        call_lines = []
        for action in actions:
            tool = action.get("tool_name", "unknown_tool")
            call_lines.append(
                f"    # seq={action.get('seq', '?')}: {tool} was called here\n"
                f"    raise NotImplementedError('implement: {tool} equivalent')"
            )
        if not call_lines:
            call_lines = ["    raise NotImplementedError('implement pipeline body')"]
        return "\n".join(call_lines)
