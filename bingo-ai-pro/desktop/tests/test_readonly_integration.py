from desktop.core.fingerprint import capture_database_fingerprint, fingerprints_match
from desktop.core.readonly_attack import run_readonly_attack_suite


def test_readonly_attack_suite_blocks_all_writes_and_keeps_fingerprint():
    before = capture_database_fingerprint()
    result = run_readonly_attack_suite()
    after = capture_database_fingerprint()

    assert result["select_ok"]
    assert result["all_blocked"], result
    assert fingerprints_match(before, after)

