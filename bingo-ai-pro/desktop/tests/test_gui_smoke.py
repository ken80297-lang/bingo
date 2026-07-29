from desktop.core.gui_smoke import run_gui_smoke


def test_gui_smoke_opens_all_pages_and_closes_cleanly():
    result = run_gui_smoke(auto_close_ms=100)

    if result.get("environment_blocked"):
        assert result["process_can_exit"]
        assert result["fingerprint_match"]
        return
    assert result["root_created"]
    assert result["main_window_created"]
    assert result["all_pages_opened"], result
    assert result["background_threads_left"] == []
    assert result["fingerprint_match"]
