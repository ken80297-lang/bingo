from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def test_production_app_imports_with_rule_snapshot_router():
    import app

    api_root = pathlib.Path(__file__).resolve().parents[1] / "api"
    database_root = pathlib.Path(__file__).resolve().parents[1] / "database"
    services_root = pathlib.Path(__file__).resolve().parents[1] / "services"

    assert (api_root / "rule_snapshots.py").is_file()
    assert (database_root / "rule_snapshot_store.py").is_file()
    assert (services_root / "rule_snapshot.py").is_file()

    routes = {getattr(route, "path", None) for route in app.app.routes}
    for route in app.app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            routes.update(getattr(child, "path", None) for child in original_router.routes)

    assert "/api/rule-snapshots/health" in routes
