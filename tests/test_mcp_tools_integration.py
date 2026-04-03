import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.repl_daemon import ReplDaemon


def test_tools_call_routes_exec_read_write_in_same_runtime():
    daemon = ReplDaemon(root=ROOT)

    r1 = daemon.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "icc_exec", "arguments": {"code": "n = 10\nprint(n)"}},
        }
    )
    assert "10" in r1["result"]["content"][0]["text"]

    r2 = daemon.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "icc_exec", "arguments": {"code": "print(n + 1)"}},
        }
    )
    assert "11" in r2["result"]["content"][0]["text"]

    read = daemon.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "icc_read", "arguments": {"connector": "mock", "pipeline": "layers"}},
        }
    )
    read_obj = json.loads(read["result"]["content"][0]["text"])
    assert read_obj["pipeline_id"] == "mock.read.layers"

    write = daemon.handle_jsonrpc(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "icc_write",
                "arguments": {"connector": "mock", "pipeline": "add-wall", "length": 1200},
            },
        }
    )
    write_obj = json.loads(write["result"]["content"][0]["text"])
    assert write_obj["verification_state"] == "verified"


def test_tools_list_does_not_expose_admin_state_operations():
    daemon = ReplDaemon(root=ROOT)
    listed = daemon.handle_jsonrpc({"jsonrpc": "2.0", "id": 99, "method": "tools/list", "params": {}})
    names = [t["name"] for t in listed["result"]["tools"]]
    assert "icc_state_status" not in names
    assert "icc_state_clear" not in names
