from desktop.core.fingerprint import capture_database_fingerprint, fingerprints_match


def test_database_fingerprint_is_stable_for_read_only_capture():
    first = capture_database_fingerprint()
    second = capture_database_fingerprint()

    assert first["status"] == "ok"
    assert first["counts"].get("official_draw_history", 0) > 0
    assert fingerprints_match(first, second)

