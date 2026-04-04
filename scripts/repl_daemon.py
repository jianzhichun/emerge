# backward-compat shim — use emerge_daemon.EmergeDaemon going forward
from scripts.emerge_daemon import EmergeDaemon as ReplDaemon  # noqa: F401
from scripts.emerge_daemon import run_stdio  # noqa: F401

if __name__ == "__main__":
    run_stdio()
