from __future__ import annotations

import os

RELEASE_NAME = os.getenv("RELEASE_NAME", "Phase 28 Production Reset")
RELEASE_VERSION = os.getenv("RELEASE_VERSION", "v28.0.0")
RELEASE_PHASE = os.getenv("RELEASE_PHASE", "28")
MODEL_VERSION = os.getenv("MODEL_VERSION", "v7")
FEATURE_VERSION = os.getenv("FEATURE_VERSION", "28.0")
LEARNING_ENGINE_VERSION = os.getenv("LEARNING_ENGINE_VERSION", "22.1")
OBSERVATION_VERSION = os.getenv("OBSERVATION_VERSION", "22.1.5")
RULE_LIBRARY_VERSION = os.getenv("RULE_LIBRARY_VERSION", "28.0")
DASHBOARD_VERSION = os.getenv("DASHBOARD_VERSION", "28.0")
DATABASE_SCHEMA_VERSION = os.getenv("DATABASE_SCHEMA_VERSION", "28.0")
GIT_COMMIT_HASH = os.getenv("GIT_COMMIT_HASH") or os.getenv("RENDER_GIT_COMMIT") or "pending"
GIT_BRANCH = os.getenv("GIT_BRANCH") or os.getenv("RENDER_GIT_BRANCH") or "runtime"


def release_payload() -> dict:
    return {
        "release_name": RELEASE_NAME,
        "release_version": RELEASE_VERSION,
        "phase": RELEASE_PHASE,
        "git_commit_hash": GIT_COMMIT_HASH,
        "git_commit_short": GIT_COMMIT_HASH[:7] if GIT_COMMIT_HASH and GIT_COMMIT_HASH != "pending" else "pending",
        "git_branch": GIT_BRANCH,
        "model_version": MODEL_VERSION,
        "feature_version": FEATURE_VERSION,
        "learning_engine_version": LEARNING_ENGINE_VERSION,
        "observation_version": OBSERVATION_VERSION,
        "rule_library_version": RULE_LIBRARY_VERSION,
        "dashboard_version": DASHBOARD_VERSION,
        "database_schema_version": DATABASE_SCHEMA_VERSION,
    }

