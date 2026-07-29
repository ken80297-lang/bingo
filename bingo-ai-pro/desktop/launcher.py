from __future__ import annotations

from pathlib import Path
import sys


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    backend = project_root / "backend"
    for item in (str(project_root), str(backend)):
        if item not in sys.path:
            sys.path.insert(0, item)
    from desktop.app import main as app_main

    return app_main()


if __name__ == "__main__":
    raise SystemExit(main())

