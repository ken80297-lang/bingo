from __future__ import annotations

from pathlib import Path


APP_NAME = "Bingo AI Pro 桌面模擬器"
DEFAULT_WINDOW_SIZE = "1280x820"
RECOMMENDED_WINDOW_SIZE = "1440x900"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
DATA_DIR = Path(__file__).resolve().parent / "data"
CACHE_JSON_PATH = DATA_DIR / "desktop_cache.json"
CACHE_SQLITE_PATH = DATA_DIR / "desktop_cache.sqlite3"
