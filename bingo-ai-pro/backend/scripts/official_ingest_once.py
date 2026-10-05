from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.official_ingest import ingest_latest_official_once


def main() -> int:
    result = ingest_latest_official_once()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str), flush=True)
    return 0 if result.get("status") in {"ok", "noop"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
