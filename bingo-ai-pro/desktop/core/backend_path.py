from __future__ import annotations

import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def backend_root() -> Path:
    return project_root() / "backend"


def ensure_backend_path() -> Path:
    backend = backend_root()
    for item in (str(project_root()), str(backend)):
        if item not in sys.path:
            sys.path.insert(0, item)
    return backend

