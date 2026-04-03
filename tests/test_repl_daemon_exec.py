from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.repl_daemon import ReplDaemon


def test_icc_exec_persists_variables_across_calls():
    daemon = ReplDaemon(root=ROOT)
    first = daemon.call_tool("icc_exec", {"code": "x = 41\nprint('set')"})
    assert first.get("isError") is not True
    second = daemon.call_tool("icc_exec", {"code": "print(x + 1)"})
    text = second["content"][0]["text"]
    assert "42" in text


def test_icc_exec_returns_explicit_error_payload():
    daemon = ReplDaemon(root=ROOT)
    result = daemon.call_tool("icc_exec", {"code": "raise ValueError('boom')"})
    assert result["isError"] is True
    assert "ValueError" in result["content"][0]["text"]
