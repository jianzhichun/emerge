<!-- emerge:connector:hypermesh — auto-generated at SessionStart -->
# Connector: hypermesh

# HyperMesh Connector — Operational Notes

## Environment

- HyperMesh 2025.0.0.24 on mycader-1 (Windows)
- Tcl socket server on port 9999 (loopback only — must run from runner, not Mac)
- Runner profile: `mycader-1`
- Protocol: send `<cmd>\n` → recv `SUCCESS: <result>\n` or `ERROR: <msg>\n`

## Critical: *automesh CRASHES HyperMesh 2025

`*automesh 1 5 2` causes a segmentation fault in HM 2025 wh
