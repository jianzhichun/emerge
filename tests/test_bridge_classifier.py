"""Unit tests for bridge failure classifier pure functions.

These are extracted from _try_flywheel_bridge so each failure mode can be
tested independently of the daemon / PolicyEngine / IntentRegistry stack.
"""

from scripts.emerge_daemon import EmergeDaemon

_classify_bridge_failure = EmergeDaemon._classify_bridge_failure
_classify_bridge_success_non_empty = EmergeDaemon._classify_bridge_success_non_empty


class TestClassifyBridgeFailure:
    """_classify_bridge_failure(result, mode, has_non_empty_baseline) → dict | None."""

    def test_verify_degraded(self):
        result = {
            "rows": [{"id": 1}],
            "verify_result": {"ok": False, "why": "schema mismatch"},
            "verification_state": "degraded",
        }
        failure = _classify_bridge_failure(result, "read", False)
        assert failure is not None
        assert failure["demotion_reason"] == "bridge_broken"
        assert "verify_degraded" in failure["reason"]

    def test_read_empty_with_baseline(self):
        result = {
            "rows": [],
            "verify_result": {"ok": True},
            "verification_state": "verified",
        }
        failure = _classify_bridge_failure(result, "read", True)
        assert failure is not None
        assert failure["demotion_reason"] == "bridge_silent_empty"
        assert "empty" in failure["reason"]

    def test_read_empty_no_baseline(self):
        result = {
            "rows": [],
            "verify_result": {"ok": True},
            "verification_state": "verified",
        }
        failure = _classify_bridge_failure(result, "read", False)
        assert failure is None

    def test_write_action_not_ok(self):
        result = {
            "action_result": {"ok": False, "error": "quota exceeded"},
            "verify_result": {"ok": True},
            "verification_state": "verified",
        }
        failure = _classify_bridge_failure(result, "write", False)
        assert failure is not None
        assert failure["demotion_reason"] == "bridge_broken"
        assert "action_not_ok" in failure["reason"]

    def test_write_action_ok(self):
        result = {
            "action_result": {"ok": True},
            "verify_result": {"ok": True},
            "verification_state": "verified",
        }
        failure = _classify_bridge_failure(result, "write", False)
        assert failure is None

    def test_read_non_empty_success(self):
        result = {
            "rows": [{"id": 1}],
            "verify_result": {"ok": True},
            "verification_state": "verified",
        }
        failure = _classify_bridge_failure(result, "read", True)
        assert failure is None

    def test_none_rows_is_empty_with_baseline(self):
        result = {
            "rows": None,
            "verify_result": {"ok": True},
            "verification_state": "verified",
        }
        failure = _classify_bridge_failure(result, "read", True)
        assert failure is not None
        assert failure["demotion_reason"] == "bridge_silent_empty"

    def test_non_dict_result_is_not_failure(self):
        failure = _classify_bridge_failure("string_result", "read", True)
        assert failure is None

    def test_empty_string_rows_with_baseline(self):
        result = {
            "rows": "",
            "verify_result": {"ok": True},
            "verification_state": "verified",
        }
        failure = _classify_bridge_failure(result, "read", True)
        assert failure is not None
        assert failure["demotion_reason"] == "bridge_silent_empty"


class TestClassifyBridgeSuccessNonEmpty:
    """_classify_bridge_success_non_empty(result, mode) → bool | None."""

    def test_read_non_empty_list(self):
        result = {"rows": [{"id": 1}]}
        assert _classify_bridge_success_non_empty(result, "read") is True

    def test_read_empty_list(self):
        result = {"rows": []}
        assert _classify_bridge_success_non_empty(result, "read") is None

    def test_read_none_rows(self):
        result = {"rows": None}
        assert _classify_bridge_success_non_empty(result, "read") is None

    def test_read_missing_rows_key(self):
        result = {}
        assert _classify_bridge_success_non_empty(result, "read") is None

    def test_write_returns_none(self):
        result = {"action_result": {"ok": True}}
        assert _classify_bridge_success_non_empty(result, "write") is None

    def test_non_dict_result(self):
        assert _classify_bridge_success_non_empty("not a dict", "read") is None

    def test_read_non_empty_dict(self):
        result = {"rows": {"key": "val"}}
        assert _classify_bridge_success_non_empty(result, "read") is True

    def test_read_non_empty_string(self):
        result = {"rows": "data"}
        assert _classify_bridge_success_non_empty(result, "read") is True


class TestExtractRowKeysSample:
    """EmergeDaemon._extract_row_keys_sample(result, mode) → frozenset[str] | None."""

    def test_read_list_of_dicts(self):
        result = {"rows": [{"id": 1, "name": "foo"}, {"id": 2, "name": "bar"}]}
        keys = EmergeDaemon._extract_row_keys_sample(result, "read")
        assert keys == frozenset({"id", "name"})

    def test_read_empty_list(self):
        result = {"rows": []}
        assert EmergeDaemon._extract_row_keys_sample(result, "read") is None

    def test_read_non_dict_rows(self):
        result = {"rows": [1, 2, 3]}
        assert EmergeDaemon._extract_row_keys_sample(result, "read") is None

    def test_write_mode(self):
        result = {"rows": [{"id": 1}]}
        assert EmergeDaemon._extract_row_keys_sample(result, "write") is None

    def test_non_dict_result(self):
        assert EmergeDaemon._extract_row_keys_sample("string", "read") is None


class TestClassifyBridgeFailureSchemaDrift:
    """_classify_bridge_failure schema-drift path."""

    def test_schema_drift_removed_key(self):
        result = {"rows": [{"id": 1}], "verification_state": "verified"}
        sample = frozenset({"id", "name"})  # baseline had 'name'
        failure = _classify_bridge_failure(result, "read", True, sample)
        assert failure is not None
        assert failure["demotion_reason"] == "bridge_schema_drift"
        assert "removed" in failure["reason"]
        assert "name" in failure["reason"]

    def test_schema_drift_added_key(self):
        result = {"rows": [{"id": 1, "name": "foo", "extra": "x"}], "verification_state": "verified"}
        sample = frozenset({"id", "name"})
        failure = _classify_bridge_failure(result, "read", True, sample)
        assert failure is not None
        assert failure["demotion_reason"] == "bridge_schema_drift"
        assert "added" in failure["reason"]

    def test_no_drift_same_keys(self):
        result = {"rows": [{"id": 1, "name": "foo"}], "verification_state": "verified"}
        sample = frozenset({"id", "name"})
        failure = _classify_bridge_failure(result, "read", True, sample)
        assert failure is None

    def test_no_sample_no_drift(self):
        result = {"rows": [{"id": 1}], "verification_state": "verified"}
        failure = _classify_bridge_failure(result, "read", True, None)
        assert failure is None

    def test_drift_with_non_empty_non_dict_rows_ignored(self):
        # rows is a non-empty string — schema-drift check skips non-list rows
        result = {"rows": "raw_data", "verification_state": "verified"}
        sample = frozenset({"id"})
        failure = _classify_bridge_failure(result, "read", True, sample)
        assert failure is None
