"""Remote pipeline execution for EmergeDaemon.

run_pipeline_remotely() packages a local pipeline (YAML + .py) into a
self-contained icc_exec payload and dispatches it to a remote runner.
The runner stays a pure Python executor — it never needs connector files.
"""
from __future__ import annotations

import json
from typing import Any

from scripts.pipeline_engine import PipelineEngine


def run_pipeline_remotely(
    pipeline_engine: PipelineEngine,
    client: Any,
    mode: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Execute a pipeline on a remote runner as inline icc_exec code.

    Raises RuntimeError on remote execution failure.
    Raises PipelineMissingError if the pipeline is not found locally.
    """
    connector = str(arguments.get("connector", "")).strip()
    pipeline_name = str(arguments.get("pipeline", "")).strip()
    target_profile = str(arguments.get("target_profile", "default")).strip()

    # Raises PipelineMissingError if not found locally — propagates to structured hint.
    metadata, py_source = pipeline_engine._load_pipeline_source(connector, mode, pipeline_name)

    # Strip `from __future__` lines — they are only valid as the first statement of a
    # module, and exec() raises SyntaxError when they appear mid-string.
    py_source = "\n".join(
        line for line in py_source.splitlines()
        if not line.strip().startswith("from __future__")
    )

    meta_repr = repr(json.dumps(metadata, ensure_ascii=True))
    args_repr = repr(json.dumps(arguments, ensure_ascii=True))

    # workflow pipelines dispatch as read or write depending on their YAML steps.
    if mode == "workflow":
        has_write_steps = isinstance(metadata.get("write_steps"), list) and len(metadata["write_steps"]) > 0
        effective_mode = "write" if has_write_steps else "read"
    else:
        effective_mode = mode

    if effective_mode == "read":
        dispatch = (
            "_rows = run_read(metadata=_m, args=_a)\n"
            "_vfn = globals().get('verify_read')\n"
            "_v = _vfn(metadata=_m, args=_a, rows=_rows) if callable(_vfn) else {'ok': True}\n"
            "_out = {'rows': _rows, 'verify': _v}\n"
        )
    else:
        dispatch = (
            "_act = run_write(metadata=_m, args=_a)\n"
            "_vfn = globals().get('verify_write')\n"
            "if not callable(_vfn): raise ValueError('verify_write is required')\n"
            "_v = _vfn(metadata=_m, args=_a, action_result=_act)\n"
            "_ok = bool(_v.get('ok', False))\n"
            "_pol = _m.get('rollback_or_stop_policy', 'stop')\n"
            "_rb, _rr, _st = False, None, False\n"
            "if not _ok:\n"
            "    if _pol == 'rollback':\n"
            "        _rfn = globals().get('rollback_write')\n"
            "        if callable(_rfn):\n"
            "            try:\n"
            "                _rr = _rfn(metadata=_m, args=_a, action_result=_act)\n"
            "                if not isinstance(_rr, dict): _rr = {'ok': False, 'error': 'must return object'}\n"
            "            except Exception as _re: _rr = {'ok': False, 'error': str(_re)}\n"
            "            _rb = True\n"
            "        else:\n"
            "            _rr = {'ok': False, 'error': 'rollback_write not implemented'}; _st = True\n"
            "    else:\n"
            "        _st = True\n"
            "_out = {'action_result': _act, 'verify': _v, 'rollback_executed': _rb, 'rollback_result': _rr, 'stop_triggered': _st}\n"
        )

    result_var = "__emerge_pipeline_out"
    exec_code = (
        "import json as _j\n"
        f"_m = _j.loads({meta_repr})\n"
        f"_a = _j.loads({args_repr})\n"
        f"{py_source}\n"
        f"{dispatch}"
        f"{result_var} = _out\n"
    )

    exec_result = client.call_tool("icc_exec", {
        "code": exec_code,
        "no_replay": True,
        "target_profile": target_profile,
        "result_var": result_var,
    })

    if exec_result.get("isError"):
        text = str(exec_result.get("content", [{}])[0].get("text", "remote pipeline exec failed"))
        raise RuntimeError(text)

    output = exec_result.get("result_var_value")
    if not isinstance(output, dict):
        result_err = str(exec_result.get("result_var_error", "")).strip()
        fallback_text = str(exec_result.get("content", [{}])[0].get("text", "")).strip()
        detail = result_err or fallback_text or "missing result_var_value"
        raise RuntimeError(f"remote pipeline exec missing structured output: {detail}")

    if effective_mode == "read":
        rows = output.get("rows", [])
        verify = output.get("verify", {"ok": True})
        return PipelineEngine._build_read_result(
            connector=connector,
            pipeline=pipeline_name,
            metadata=metadata,
            rows=rows,
            verify_result=verify,
            mode=mode,
        )
    else:
        act = output.get("action_result", {})
        verify = output.get("verify", {})
        rb = bool(output.get("rollback_executed", False))
        st = bool(output.get("stop_triggered", False))
        rr = output.get("rollback_result")
        return PipelineEngine._build_write_result(
            connector=connector,
            pipeline=pipeline_name,
            metadata=metadata,
            action_result=act,
            verify_result=verify,
            stop_triggered=st,
            rollback_executed=rb,
            rollback_result=rr,
            mode=mode,
        )
