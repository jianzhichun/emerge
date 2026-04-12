from __future__ import annotations


def format_pending_actions(actions: list) -> str:
    """Format a list of cockpit pending actions into a human-readable string.

    Used by both watch_pending.py (Monitor tool path) and user_prompt_submit.py
    (UserPromptSubmit hook fallback path) to ensure consistent output.
    """
    lines = ["[Cockpit] The operator submitted the following actions — execute in order:"]
    for i, a in enumerate(actions, 1):
        t = a.get("type", "unknown")
        if t == "tool-call":
            call = a.get("call", {}) if isinstance(a.get("call"), dict) else {}
            tool = call.get("tool", "?")
            call_args = call.get("arguments", {})
            meta = a.get("meta", {}) if isinstance(a.get("meta"), dict) else {}
            scope = str(meta.get("scope", "")).strip()
            scope_suffix = f" scope={scope}" if scope else ""
            lines.append(f"{i}. Execute tool-call {tool} args={call_args}{scope_suffix}")
        elif t == "pipeline-set":
            lines.append(f"{i}. pipeline-set {a.get('key')} fields={a.get('fields', {})}")
        elif t == "pipeline-delete":
            lines.append(f"{i}. pipeline-delete {a.get('key')}")
        elif t == "notes-edit":
            lines.append(f"{i}. Update {a.get('connector')} NOTES.md (full replace)")
        elif t == "notes-comment":
            lines.append(
                f"{i}. Append comment to {a.get('connector')} NOTES.md: "
                f"{str(a.get('comment', ''))[:80]}"
            )
        elif t == "crystallize-component":
            lines.append(
                f"{i}. Crystallize component {a.get('filename')} -> "
                f"{a.get('connector')}/cockpit/"
            )
        else:
            lines.append(f"{i}. {t}: {a}")
    return "\n".join(lines)
