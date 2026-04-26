"""Mechanism helpers for writing pipeline artifacts safely."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


class IndentedSafeDumper:
    """YAML dumper that keeps nested lists indented in human-readable form."""

    @staticmethod
    def dump_yaml(payload: dict[str, Any]) -> str:
        import yaml  # type: ignore

        class _Dumper(yaml.SafeDumper):
            def increase_indent(self, flow=False, indentless=False):  # type: ignore[override]
                return super().increase_indent(flow, False)

        return yaml.dump(payload, Dumper=_Dumper, sort_keys=False, allow_unicode=True)


def assert_path_in_root(path: Path, root: Path, *, label: str) -> None:
    """Raise ValueError if ``path`` resolves outside ``root``."""
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"icc_crystallize: path escapes connector root ({label})"
        ) from exc


def atomic_write_text(dest: Path, content: str, *, prefix: str = ".tmp-") -> None:
    """Atomically write UTF-8 text to ``dest`` using a temp file beside it."""
    fd, tmp = tempfile.mkstemp(prefix=prefix, dir=str(dest.parent))
    tmp_path = tmp
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, dest)
        tmp_path = ""
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
