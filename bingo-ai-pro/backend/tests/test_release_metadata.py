from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import release
from config.release import DEFAULT_GIT_COMMIT_HASH, release_payload


def test_release_payload_uses_final_phase_28_commit_metadata(monkeypatch):
    monkeypatch.delenv("GIT_COMMIT_HASH", raising=False)
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.setattr(release, "GIT_COMMIT_HASH", DEFAULT_GIT_COMMIT_HASH)

    payload = release_payload()

    assert payload["release_version"] == "v28.0.0"
    assert payload["git_commit_hash"] == DEFAULT_GIT_COMMIT_HASH
    assert payload["git_commit_short"] == DEFAULT_GIT_COMMIT_HASH[:7]
    assert len(payload["git_commit_short"]) == 7
