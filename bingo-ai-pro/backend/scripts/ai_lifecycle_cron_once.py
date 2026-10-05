from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from database.official_draw_store import get_latest_official_draw

    draw = get_latest_official_draw()
    issue = str((draw or {}).get("issue") or "").strip()
    if not issue.isdigit():
        print("AI_LIFECYCLE_CRON_NOOP reason=no_latest_official_draw", flush=True)
        return 0

    env = os.environ.copy()
    env["AI_LIFECYCLE_ISSUE"] = issue
    env["AI_LIFECYCLE_CRON_CORE_ONLY"] = "1"
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ai_lifecycle_worker_once.py")],
        cwd=str(ROOT),
        env=env,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
