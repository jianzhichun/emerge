from __future__ import annotations


def format_pending_actions(actions: list) -> str:
    """Format a list of cockpit pending actions into a human-readable string.

    Used by watch_emerge.py (Monitor tool path) and user_prompt_submit.py
    (UserPromptSubmit hook fallback path) to ensure consistent output.
    """
    lines = ["[Cockpit] The operator submitted the following actions — execute in order:"]
    for i, a in enumerate(actions, 1):
        t = a.get("type", "unknown")
        if t == "core.tool-call":
            call = a.get("call", {}) if isinstance(a.get("call"), dict) else {}
            tool = call.get("tool", "?")
            call_args = call.get("arguments", {})
            meta = a.get("meta", {}) if isinstance(a.get("meta"), dict) else {}
            scope = str(meta.get("scope", "")).strip()
            scope_suffix = f" scope={scope}" if scope else ""
            lines.append(f"{i}. Execute core.tool-call {tool} args={call_args}{scope_suffix}")
        elif t == "intent.set":
            lines.append(f"{i}. intent.set {a.get('key')} fields={a.get('fields', {})}")
        elif t == "intent.delete":
            lines.append(f"{i}. intent.delete {a.get('key')}")
        elif t == "notes.edit":
            lines.append(f"{i}. Update {a.get('connector')} NOTES.md (full replace)")
        elif t == "notes.comment":
            lines.append(
                f"{i}. Append comment to {a.get('connector')} NOTES.md: "
                f"{str(a.get('comment', ''))[:80]}"
            )
        elif t == "core.crystallize":
            lines.append(
                f"{i}. Crystallize component {a.get('filename')} -> "
                f"{a.get('connector')}/cockpit/"
            )
        else:
            lines.append(f"{i}. {t}: {a}")
    return "\n".join(lines)


def format_pattern_alert(data: dict) -> str:
    """Format a pattern-alerts.json payload into a human-readable Monitor line.

    Used by watch_emerge.py (operator-monitor alert path).
    """
    stage = data.get("stage", "?")
    sig = data.get("intent_signature", "?")
    message = data.get("message", "")
    meta = data.get("meta", {})
    lines = [f"[OperatorMonitor] Pattern alert (stage={stage}, intent={sig}):"]
    if message:
        lines.append(message)
    if meta:
        lines.append(
            f"  occurrences={meta.get('occurrences', '?')} "
            f"window={meta.get('window_minutes', '?')}min "
            f"machines={meta.get('machine_ids', [])}"
        )
    return "\n".join(lines)


def format_runner_discovered(data: dict) -> str:
    profile = data.get("runner_profile", "?")
    machine = data.get("machine_id", "?")
    ts = data.get("ts_ms", 0)
    return f"[RunnerDiscovered] runner={profile} machine={machine} ts={ts}"


def format_runner_online(data: dict) -> str:
    profile = data.get("runner_profile", "?")
    return f"[RunnerOnline] runner={profile} is ready"


def format_runner_event(data: dict) -> str:
    profile = data.get("runner_profile", "?")
    etype = data.get("type", "?")
    ts = data.get("ts_ms", 0)
    return f"[RunnerEvent] runner={profile} type={etype} ts={ts}"
