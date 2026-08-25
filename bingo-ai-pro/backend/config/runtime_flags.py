from __future__ import annotations

import os

SCHEDULER_FLAG_DEFAULTS = {
    "CATCH_UP_SCHEDULER_ENABLED": True,
    "COLLECTOR_SCHEDULER_ENABLED": True,
    "LEGACY_REFRESH_SCHEDULER_ENABLED": False,
    "DAILY_RECOVERY_ENABLED": False,
    "HISTORICAL_CATCHUP_ENABLED": False,
}

SCHEDULER_FLAG_RESPONSE_FIELDS = {
    "CATCH_UP_SCHEDULER_ENABLED": "catch_up_scheduler_enabled",
    "COLLECTOR_SCHEDULER_ENABLED": "collector_scheduler_enabled",
    "LEGACY_REFRESH_SCHEDULER_ENABLED": "legacy_refresh_scheduler_enabled",
    "DAILY_RECOVERY_ENABLED": "daily_recovery_enabled",
    "HISTORICAL_CATCHUP_ENABLED": "historical_catchup_enabled",
}


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def env_raw(name: str) -> str:
    raw = os.getenv(name)
    return "<unset>" if raw is None else str(raw)


def scheduler_flag_enabled(name: str) -> bool:
    return env_bool(name, SCHEDULER_FLAG_DEFAULTS[name])


def get_scheduler_runtime_flags() -> dict[str, bool]:
    return {
        field: scheduler_flag_enabled(name)
        for name, field in SCHEDULER_FLAG_RESPONSE_FIELDS.items()
    }
