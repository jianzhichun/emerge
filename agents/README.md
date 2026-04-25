# Emerge Watcher Agents

`operator-watcher.md` is intentionally generic. Connector-specific behavior belongs in `watcher_profile.yaml` files, not in separate hardcoded agent definitions.

Watcher agents are runner-side sensors. They observe local operator activity, redact sensitive fields, and emit structured suggestions to the orchestrator. They never write pipelines, lifecycle policy, or Memory Hub commits.

Profile contract:

```yaml
connector: hypermesh
sources:
  - type: file
    path: ~/HW_TEMP/command.tcl
parser:
  type: regex
  pattern: automesh (?P<density>\w+)
intent_hints:
  - when:
      command: automesh
    intent: hypermesh.write.automesh
preference_hints:
  - name: density
    source: density
    ttl_days: 30
redaction:
  drop_fields:
    - license_key
```
