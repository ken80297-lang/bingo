from __future__ import annotations

import importlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from config import release
from config.release import DEFAULT_GIT_COMMIT_HASH, release_payload
from database import release_store


def test_release_payload_uses_final_phase_28_commit_metadata(monkeypatch):
    monkeypatch.delenv("GIT_COMMIT_HASH", raising=False)
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.setattr(release, "GIT_COMMIT_HASH", DEFAULT_GIT_COMMIT_HASH)

    payload = release_payload()

    assert payload["release_version"] == "v28.0.0"
    assert payload["git_commit_hash"] == DEFAULT_GIT_COMMIT_HASH
    assert payload["git_commit_short"] == DEFAULT_GIT_COMMIT_HASH[:7]
    assert len(payload["git_commit_short"]) == 7


def test_release_payload_ignores_render_deployment_commit(monkeypatch):
    monkeypatch.delenv("RELEASE_GIT_COMMIT_HASH", raising=False)
    monkeypatch.delenv("GIT_COMMIT_HASH", raising=False)
    monkeypatch.setenv("RENDER_GIT_COMMIT", "d" * 40)

    reloaded = importlib.reload(release)
    payload = reloaded.release_payload()

    assert payload["git_commit_hash"] == DEFAULT_GIT_COMMIT_HASH
    assert payload["git_commit_short"] == DEFAULT_GIT_COMMIT_HASH[:7]


def test_register_release_preserves_existing_commit_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(release_store, "SQLITE_PATH", tmp_path / "release.db")
    monkeypatch.setattr(release_store, "_cloud_enabled", lambda: False)

    release_store.init_release_tables()
    release_store.register_release(
        {
            "release_version": "v28.0.0",
            "git_commit_hash": DEFAULT_GIT_COMMIT_HASH,
            "git_commit_short": DEFAULT_GIT_COMMIT_HASH[:7],
            "git_commit_message": "chore: finalize v28.0.0 release metadata",
        },
        activate=True,
    )
    release_store.register_release(
        {
            "release_version": "v28.0.0",
            "git_commit_hash": "d" * 40,
            "git_commit_short": "ddddddd",
            "git_commit_message": "Render deploy commit",
        },
        activate=True,
    )

    current = release_store.get_current_release()

    assert current["release_version"] == "v28.0.0"
    assert current["git_commit_hash"] == DEFAULT_GIT_COMMIT_HASH
    assert current["git_commit_short"] == DEFAULT_GIT_COMMIT_HASH[:7]
    assert current["git_commit_message"] == "chore: finalize v28.0.0 release metadata"


def test_ensure_default_release_repairs_phase_28_canonical_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(release_store, "SQLITE_PATH", tmp_path / "release.db")
    monkeypatch.setattr(release_store, "_cloud_enabled", lambda: False)

    release_store.init_release_tables()
    with release_store._sqlite_connection() as conn:
        conn.execute(
            """
            update production_release_registry
            set git_commit_hash = ?,
                git_commit_short = ?,
                git_commit_message = ?
            where release_version = ?
            """,
            ("d" * 40, "ddddddd", "Render deploy commit", "v28.0.0"),
        )

    release_store.ensure_default_release()

    current = release_store.get_current_release()

    assert current["release_version"] == "v28.0.0"
    assert current["git_commit_hash"] == DEFAULT_GIT_COMMIT_HASH
    assert current["git_commit_short"] == DEFAULT_GIT_COMMIT_HASH[:7]
    assert current["git_commit_message"] == "chore: finalize v28.0.0 release metadata"
