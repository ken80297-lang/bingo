from desktop.core.readonly_guard import READ_ONLY_REASON, install_readonly_guard


def test_readonly_guard_sets_desktop_flags(monkeypatch):
    monkeypatch.setenv("DESKTOP_READ_ONLY", "false")
    guard = install_readonly_guard()

    assert guard.read_only is True
    assert guard.database_write_allowed() is False
    assert guard.collector_allowed() is False
    assert guard.learning_write_allowed() is False
    assert guard.prediction_write_allowed() is False


def test_readonly_guard_blocks_write():
    guard = install_readonly_guard()

    result = guard.block_write("save_prediction")

    assert result["status"] == "blocked"
    assert result["reason"] == READ_ONLY_REASON
    assert result["operation"] == "save_prediction"

