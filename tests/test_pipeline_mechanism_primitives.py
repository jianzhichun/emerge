from __future__ import annotations

from pathlib import Path

import pytest


def test_code_assigns_name_detects_supported_assignment_shapes():
    from scripts.pipeline_code_checks import code_assigns_name

    assert code_assigns_name("__result = []", "__result")
    assert code_assigns_name("__action += [{'ok': True}]", "__action")
    assert code_assigns_name("globals()['__result'] = [{'x': 1}]", "__result")
    assert not code_assigns_name("value = []", "__result")
    assert not code_assigns_name("def broken(:", "__result")


def test_pipeline_artifact_helpers_write_atomically_and_reject_escape(tmp_path):
    from scripts.pipeline_artifacts import (
        IndentedSafeDumper,
        assert_path_in_root,
        atomic_write_text,
    )

    root = tmp_path / "connectors"
    dest = root / "mock" / "pipelines" / "read" / "state.yaml"
    dest.parent.mkdir(parents=True)

    atomic_write_text(dest, "ok: true\n", prefix=".test-")
    assert dest.read_text(encoding="utf-8") == "ok: true\n"
    assert_path_in_root(dest, root, label="dest")

    with pytest.raises(ValueError, match="path escapes connector root"):
        assert_path_in_root(tmp_path / "outside.yaml", root, label="outside")

    yaml_src = IndentedSafeDumper.dump_yaml({"steps": [{"name": "one"}]})
    assert "steps:" in yaml_src
    assert "- name: one" in yaml_src


def test_span_pipeline_skeleton_writer_creates_pending_yaml(tmp_path):
    from scripts.span_pipeline_skeleton import SpanPipelineSkeletonWriter

    writer = SpanPipelineSkeletonWriter()
    path = writer.generate_span_skeleton(
        intent_signature="mock.write.multi_step",
        span={
            "actions": [
                {
                    "seq": 1,
                    "tool_name": "tool_a",
                    "has_side_effects": True,
                    "args_snapshot": {"intent_signature": "mock.write.a"},
                },
                {
                    "seq": 2,
                    "tool_name": "tool_b",
                    "has_side_effects": False,
                    "args_snapshot": {"intent_signature": "mock.read.b"},
                },
            ]
        },
        connector_root=tmp_path,
    )

    assert path is not None
    assert path.suffix == ".yaml"
    assert path.parent == tmp_path / "mock" / "pipelines" / "write" / "_pending"
