"""YAMLScenarioEngine — declarative YAML pipeline execution.

Executes step sequences defined in YAML scenario dicts.  A shared ``context``
dict flows through every step; template substitution (``{{ var }}``,
``{{ var | filter }}``) is resolved against it before each step runs.

Two execution modes:
  - ``"write"`` — returns ``{"action_result": {...}, "verify_result": {...}}``
  - ``"read"``  — returns ``{"rows": [...], "verify_result": {...}}``

A ``verify`` section in the scenario runs after ``steps``; any
``YAMLStepError`` raised there sets ``verify_result["ok"]`` to ``False``
without propagating.
"""

from __future__ import annotations

import ast
import re
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any


class YAMLStepError(Exception):
    """Raised when a pipeline step fails."""


class YAMLScenarioEngine:
    """Execute declarative YAML scenario dicts."""

    # ── public API ────────────────────────────────────────────────────────────

    def execute(
        self,
        scenario: dict,
        args: dict,
        *,
        pipeline_engine=None,
        mode: str = "write",
    ) -> dict:
        """Run scenario steps then verify section.

        Parameters
        ----------
        scenario:
            Dict with ``steps`` list and optional ``verify`` list.
        args:
            Initial context values (caller-supplied).
        pipeline_engine:
            Optional ``PipelineEngine`` instance forwarded to
            ``connector_call`` steps.
        mode:
            ``"write"`` or ``"read"``.
        """
        context: dict[str, Any] = dict(args)

        self._run_steps(scenario.get("steps", []), context, pipeline_engine)

        verify_ok = True
        try:
            self._run_steps(scenario.get("verify", []), context, pipeline_engine)
        except Exception:
            verify_ok = False

        verify_result = {"ok": verify_ok}

        if mode == "read":
            rows = context.get("__rows", [])
            if not isinstance(rows, list):
                rows = []
            return {"rows": rows, "verify_result": verify_result}

        # write mode
        action_result = {k: v for k, v in context.items() if not k.startswith("__")}
        action_result.setdefault("ok", True)
        return {"action_result": action_result, "verify_result": verify_result}

    def execute_rollback(
        self,
        scenario: dict,
        args: dict,
        *,
        pipeline_engine=None,
    ) -> dict:
        """Run the ``rollback`` section of *scenario* if present."""
        context: dict[str, Any] = dict(args)
        try:
            self._run_steps(scenario.get("rollback", []), context, pipeline_engine)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        action_result = {k: v for k, v in context.items() if not k.startswith("__")}
        action_result.setdefault("ok", True)
        return {"action_result": action_result, "verify_result": {"ok": True}}

    # ── internal step runner ──────────────────────────────────────────────────

    def _run_steps(
        self,
        steps: list,
        context: dict,
        pipeline_engine,
    ) -> None:
        for step in steps:
            step_type: str = step.get("type", "")
            method_name = "_step_" + step_type.replace("-", "_")
            handler = getattr(self, method_name, None)
            if handler is None:
                raise YAMLStepError(f"Unknown step type: {step_type!r}")
            handler(step, context, pipeline_engine)

    # ── template resolution ───────────────────────────────────────────────────

    _FILTER_RE = re.compile(r"^\s*([a-zA-Z_]\w*)\s*\|\s*(\w+)\s*$")
    _VAR_RE = re.compile(r"^\s*([a-zA-Z_]\w*)\s*$")

    # ── condition evaluation ──────────────────────────────────────────────────

    _IDENT_FILTER_RE = re.compile(r"\b([a-zA-Z_]\w*)\s*(?:\|\s*(\w+))?")

    def _resolve(self, template: Any, context: dict) -> str:
        if not isinstance(template, str):
            return str(template)

        def _replace(m: re.Match) -> str:
            expr = m.group(1).strip()
            filter_match = self._FILTER_RE.match(expr)
            if filter_match:
                var, filt = filter_match.group(1), filter_match.group(2)
                val = context.get(var, "")
                if filt == "int":
                    try:
                        return str(int(float(str(val))))
                    except (ValueError, TypeError):
                        return "0"
                if filt == "float":
                    try:
                        return str(float(str(val)))
                    except (ValueError, TypeError):
                        return "0.0"
                if filt == "lower":
                    return str(val).lower()
                if filt == "upper":
                    return str(val).upper()
                return str(val)
            var_match = self._VAR_RE.match(expr)
            if var_match:
                return str(context.get(var_match.group(1), ""))
            return m.group(0)  # leave unknown expressions untouched

        return re.sub(r"\{\{\s*(.*?)\s*\}\}", _replace, template)

    # ── step implementations ──────────────────────────────────────────────────

    def _step_derive(self, step: dict, context: dict, _pe) -> None:
        for key, value in step.get("set", {}).items():
            context[key] = self._resolve(value, context)

    def _step_transform(self, step: dict, context: dict, _pe) -> None:
        for key, value in step.get("mapping", {}).items():
            context[key] = self._resolve(value, context)

    def _step_branch(self, step: dict, context: dict, pipeline_engine) -> None:
        condition = step.get("condition", "")
        result = self._eval_condition(condition, context)
        if result:
            self._run_steps(step.get("when", []), context, pipeline_engine)
        else:
            self._run_steps(step.get("otherwise", []), context, pipeline_engine)

    def _eval_condition(self, condition: str, context: dict) -> bool:
        """Evaluate a ``{{ expr }}`` comparison condition against *context*."""
        # Strip outer {{ }}
        inner_match = re.fullmatch(r"\{\{\s*(.*?)\s*\}\}", condition.strip(), re.DOTALL)
        if inner_match:
            expr = inner_match.group(1).strip()
        else:
            expr = condition.strip()

        # Replace identifiers (with optional | filter) with their values.
        # Pattern: word boundary, identifier, optional | filter
        # We must NOT replace numeric literals.
        def _subst(m: re.Match) -> str:
            ident = m.group(1)
            filt = m.group(2)
            # Skip Python keywords/builtins that are not context variables
            _SKIP = {"True", "False", "None", "and", "or", "not", "in",
                     "is", "if", "else", "lambda"}
            if ident in _SKIP:
                return m.group(0)
            val = context.get(ident)
            if val is None:
                # Not in context — leave as-is (may be a literal or keyword)
                return m.group(0)
            if filt == "int":
                try:
                    return str(int(float(str(val))))
                except (ValueError, TypeError):
                    return "0"
            if filt == "float":
                try:
                    return str(float(str(val)))
                except (ValueError, TypeError):
                    return "0.0"
            if filt:
                return str(val)
            # No filter: try int, then float, then repr
            try:
                return str(int(float(str(val))))
            except (ValueError, TypeError):
                pass
            try:
                float(str(val))
                return str(val)
            except (ValueError, TypeError):
                pass
            return repr(str(val))

        resolved = self._IDENT_FILTER_RE.sub(_subst, expr)

        try:
            tree = ast.parse(resolved, mode="eval")
        except SyntaxError as exc:
            raise YAMLStepError(
                f"Branch condition parse error: {exc}"
            ) from exc

        body = tree.body
        if not isinstance(body, ast.Compare):
            raise YAMLStepError(
                f"Branch condition must be a comparison expression "
                f"(e.g. '{{{{ count == 0 }}}}'); "
                f"got type={type(body).__name__!r} from {condition!r} "
                f"(resolved: {resolved!r})"
            )
        _allowed_ops = (ast.Gt, ast.Lt, ast.GtE, ast.LtE, ast.Eq, ast.NotEq)
        for op in body.ops:
            if not isinstance(op, _allowed_ops):
                raise YAMLStepError(
                    f"Branch condition uses disallowed operator in: {condition!r}"
                )

        return bool(eval(  # noqa: S307
            compile(tree, "<branch-condition>", "eval"),
            {"__builtins__": {}},
            {},
        ))

    # ── cli steps ─────────────────────────────────────────────────────────────

    def _step_cli(self, step: dict, context: dict, _pe) -> None:
        cmd = self._resolve(step["run"], context)
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise YAMLStepError(
                f"CLI command failed (exit {proc.returncode}): {cmd}\n"
                f"stderr: {proc.stderr.strip()}"
            )
        if step.get("extract_stdout"):
            context[step["extract_stdout"]] = proc.stdout.strip()

    def _step_cli_poll(self, step: dict, context: dict, _pe) -> None:
        cmd = self._resolve(step["run"], context)
        until = self._resolve(step.get("until", ""), context)
        timeout_s = float(step.get("timeout_s", 30))
        deadline = time.monotonic() + timeout_s
        while True:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True, text=True
            )
            if until in proc.stdout:
                if step.get("extract_stdout"):
                    context[step["extract_stdout"]] = proc.stdout.strip()
                return
            if time.monotonic() >= deadline:
                raise YAMLStepError(
                    f"cli_poll timed out after {timeout_s}s waiting for "
                    f"{until!r} in: {cmd}"
                )
            time.sleep(0.5)

    # ── connector_call ────────────────────────────────────────────────────────

    def _step_connector_call(self, step: dict, context: dict, pipeline_engine) -> None:
        if pipeline_engine is None:
            raise YAMLStepError(
                "connector_call step requires pipeline_engine but none was provided"
            )
        call_args = {k: self._resolve(v, context) for k, v in step.get("args", {}).items()}
        call_mode = step.get("mode", "read")
        if call_mode == "read":
            result = pipeline_engine.run_read(call_args)
        else:
            result = pipeline_engine.run_write(call_args)
        for dest_key, src_path in step.get("extract", {}).items():
            # src_path may be a dotted path into result (supports dict keys and list indices)
            val = result
            for part in src_path.split("."):
                if isinstance(val, dict):
                    val = val.get(part)
                elif isinstance(val, list):
                    # Handle list indexing (e.g., "rows.0.field")
                    try:
                        idx = int(part)
                        val = val[idx] if 0 <= idx < len(val) else None
                    except (ValueError, IndexError):
                        val = None
                else:
                    val = None
                    break
            # None propagates to context; downstream {{ var }} renders as empty string
            context[dest_key] = val

    # ── http steps ────────────────────────────────────────────────────────────

    def _http_request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict | None = None,
        expected_status: int | None = None,
    ) -> tuple[int, bytes]:
        req = urllib.request.Request(url, data=body, method=method)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            if expected_status is not None and exc.code == expected_status:
                return exc.code, exc.read()
            raise YAMLStepError(
                f"HTTP {method} {url} returned {exc.code}: {exc.reason}"
            ) from exc

    def _step_http_get(self, step: dict, context: dict, _pe) -> None:
        url = self._resolve(step["url"], context)
        expected = step.get("expect_status", 200)
        status, body = self._http_request("GET", url, expected_status=expected)
        if status != expected:
            raise YAMLStepError(
                f"http_get expected status {expected}, got {status} for {url}"
            )
        if step.get("extract_json"):
            import json
            data = json.loads(body)
            for dest, src in step["extract_json"].items():
                val = data
                for part in src.split("."):
                    val = val.get(part) if isinstance(val, dict) else None
                context[dest] = val

    def _step_http_post(self, step: dict, context: dict, _pe) -> None:
        import json as _json
        url = self._resolve(step["url"], context)
        expected = step.get("expect_status", 200)
        payload = step.get("body", {})
        if isinstance(payload, dict):
            payload = {k: self._resolve(v, context) for k, v in payload.items()}
            body = _json.dumps(payload).encode()
        else:
            body = self._resolve(str(payload), context).encode()
        headers = {"Content-Type": "application/json"}
        status, resp_body = self._http_request("POST", url, body=body, headers=headers, expected_status=expected)
        if status != expected:
            raise YAMLStepError(
                f"http_post expected status {expected}, got {status} for {url}"
            )
        if step.get("extract_json"):
            data = _json.loads(resp_body)
            for dest, src in step["extract_json"].items():
                val = data
                for part in src.split("."):
                    val = val.get(part) if isinstance(val, dict) else None
                context[dest] = val

    def _step_http_delete(self, step: dict, context: dict, _pe) -> None:
        url = self._resolve(step["url"], context)
        expected = step.get("expect_status", 200)
        status, _ = self._http_request("DELETE", url, expected_status=expected)
        if status != expected:
            raise YAMLStepError(
                f"http_delete expected status {expected}, got {status} for {url}"
            )

    def _step_http_poll(self, step: dict, context: dict, _pe) -> None:
        import json as _json
        url = self._resolve(step["url"], context)
        until_key = step.get("until_key")
        until_value = self._resolve(str(step.get("until_value", "")), context)
        timeout_s = float(step.get("timeout_s", 30))
        deadline = time.monotonic() + timeout_s
        last_error: str | None = None
        while True:
            try:
                status, body = self._http_request("GET", url)
                data = _json.loads(body)
                if until_key:
                    val = data
                    for part in until_key.split("."):
                        val = val.get(part) if isinstance(val, dict) else None
                    if str(val) == until_value:
                        if step.get("extract_json"):
                            for dest, src in step["extract_json"].items():
                                v = data
                                for part in src.split("."):
                                    v = v.get(part) if isinstance(v, dict) else None
                                context[dest] = v
                        return
                    last_error = f"key {until_key}={val!r} != {until_value!r}"
            except YAMLStepError as exc:
                last_error = str(exc)
            if time.monotonic() >= deadline:
                detail = f"; last error: {last_error}" if last_error else ""
                raise YAMLStepError(
                    f"http_poll timed out after {timeout_s}s waiting for "
                    f"{until_key}={until_value!r} at {url}{detail}"
                )
            time.sleep(0.5)
