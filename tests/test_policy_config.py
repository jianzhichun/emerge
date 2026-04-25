def test_truncate_jsonl_no_stray_tmp_file(tmp_path):
    """After truncation, no fixed-name .tmp file must remain."""
    import json
    from scripts.policy_config import truncate_jsonl_if_needed

    path = tmp_path / "events.jsonl"
    lines = [json.dumps({"n": i}) for i in range(20000)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    truncate_jsonl_if_needed(path, max_lines=10000)

    # No fixed-name stray .tmp files
    stray = list(tmp_path.glob("events.tmp"))
    assert stray == [], f"stray fixed-name .tmp files: {stray}"

    # File was truncated to 10000 lines
    remaining = [l for l in path.read_text().splitlines() if l.strip()]
    assert len(remaining) == 10000, f"Expected 10000 lines, got {len(remaining)}"

    # Correct lines retained (last max_lines)
    last = json.loads(remaining[-1])
    assert last["n"] == 19999
    first = json.loads(remaining[0])
    assert first["n"] == 10000


def test_truncate_jsonl_below_threshold_no_change(tmp_path):
    """File below threshold must not be modified at all."""
    import json
    from scripts.policy_config import truncate_jsonl_if_needed

    path = tmp_path / "small.jsonl"
    lines = [json.dumps({"n": i}) for i in range(100)]
    original = "\n".join(lines) + "\n"
    path.write_text(original, encoding="utf-8")

    import stat
    mtime_before = path.stat().st_mtime_ns  # nanosecond precision
    truncate_jsonl_if_needed(path, max_lines=10000)
    mtime_after = path.stat().st_mtime_ns

    assert mtime_before == mtime_after, "File must not be touched when below threshold"


def test_exec_default_timeout_is_cae_friendly(monkeypatch):
    from scripts.policy_config import exec_limits

    monkeypatch.delenv("EMERGE_EXEC_TIMEOUT_S", raising=False)

    assert exec_limits()["timeout_s"] == 600
