import socket
import threading
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.remote_runner import RunnerExecutor, RunnerHTTPHandler, ThreadingHTTPServer
from scripts.runner_client import RunnerClient


class _RunnerServer:
    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def __enter__(self) -> "_RunnerServer":
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        host, port = sock.getsockname()
        sock.close()

        executor = RunnerExecutor(root=ROOT, state_root=self._state_root)
        handler_cls = type("TestRunnerHTTPHandler", (RunnerHTTPHandler,), {"executor": executor})
        self._server = ThreadingHTTPServer((host, port), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = f"http://{host}:{port}"
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        assert self._server is not None
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def test_runner_client_exec_persists_state(tmp_path: Path):
    with _RunnerServer(tmp_path / "runner-state") as server:
        client = RunnerClient(base_url=server.url, timeout_s=5.0)
        out1 = client.call_tool("icc_exec", {"code": "x = 41\nprint('set')"})
        assert out1.get("isError") is not True
        out2 = client.call_tool("icc_exec", {"code": "print(x + 1)"})
        assert "42" in out2["content"][0]["text"]


def test_runner_rejects_pipeline_tools(tmp_path: Path):
    """Runner is a pure executor — icc_read/icc_write must be rejected (daemon handles them)."""
    with _RunnerServer(tmp_path / "runner-state") as server:
        client = RunnerClient(base_url=server.url, timeout_s=5.0)
        read = client.call_tool("icc_read", {"connector": "mock", "pipeline": "layers"})
        assert read.get("isError") is True
        write = client.call_tool(
            "icc_write", {"connector": "mock", "pipeline": "add-wall", "length": 1200}
        )
        assert write.get("isError") is True


def test_get_session_is_thread_safe(tmp_path: Path):
    """Concurrent _get_session calls for the same profile must return the same ExecSession."""
    executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
    results: list[object] = []
    errors: list[Exception] = []

    def fetch() -> None:
        try:
            results.append(executor._get_session("default"))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=fetch) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Threads raised: {errors}"
    # All threads must have received the exact same ExecSession instance
    assert len(set(id(r) for r in results)) == 1, "Multiple ExecSession instances created (race condition)"


def test_forward_event_to_daemon_returns_false_on_failure(tmp_path):
    """_forward_event_to_daemon returns False when the daemon is unreachable."""
    executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
    executor._team_lead_url = "http://127.0.0.1:19999"  # nothing listening
    executor._runner_profile = "test-profile"
    result = executor._forward_event_to_daemon({"type": "test"})
    assert result is False


def test_post_operator_message_sends_correct_payload(tmp_path):
    """_post_operator_message calls _forward_event_to_daemon with required fields."""
    executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
    executor._team_lead_url = "http://localhost:9999"
    executor._runner_profile = "mycader-1"
    captured: list = []
    executor._forward_event_to_daemon = lambda event: (captured.append(event), True)[1]
    executor._post_operator_message("暂停 pipeline")
    assert len(captured) == 1
    ev = captured[0]
    assert ev["type"] == "operator_message"
    assert ev["text"] == "暂停 pipeline"
    assert ev["profile"] == "mycader-1"
    assert isinstance(ev["ts_ms"], int)
    assert "machine_id" in ev


def test_post_operator_message_shows_error_toast_on_failure(tmp_path, monkeypatch):
    """_post_operator_message shows error toast when daemon is unreachable."""
    import scripts.operator_popup as popup_mod
    toast_bodies: list = []
    monkeypatch.setattr(popup_mod, "_render_toast",
        lambda *, body, timeout_s: (toast_bodies.append(body), {"action": "dismissed", "value": ""})[1])
    executor = RunnerExecutor(root=ROOT, state_root=tmp_path / "state")
    executor._team_lead_url = "http://localhost:9999"
    executor._runner_profile = "mycader-1"
    executor._forward_event_to_daemon = lambda event: False
    executor._post_operator_message("test message")
    assert len(toast_bodies) == 1
    assert "失败" in toast_bodies[0]
