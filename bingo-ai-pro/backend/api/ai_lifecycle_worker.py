from __future__ import annotations

import hmac
import os
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException

router = APIRouter(prefix="/api/ai-lifecycle-worker", tags=["AI Lifecycle Worker"])
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ai_lifecycle_worker_once.py"


def _authorized(token: str | None) -> None:
    expected = os.getenv("AI_LIFECYCLE_TRIGGER_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="AI lifecycle worker trigger disabled")
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


@router.post("/run")
def run_ai_lifecycle_worker(
    issue: str,
    x_ai_lifecycle_token: str | None = Header(default=None),
):
    _authorized(x_ai_lifecycle_token)
    issue = str(issue or "").strip()
    if not issue.isdigit():
        raise HTTPException(status_code=400, detail="issue must be numeric")

    env = os.environ.copy()
    env["AI_LIFECYCLE_ISSUE"] = issue
    try:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(
            status_code=504,
            detail={
                "status": "timeout",
                "issue": issue,
                "stdout_tail": (exc.stdout or "")[-2000:],
                "stderr_tail": (exc.stderr or "")[-2000:],
            },
        ) from exc

    return {
        "status": "ok" if completed.returncode == 0 else "error",
        "issue": issue,
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }
