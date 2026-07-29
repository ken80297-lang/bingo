from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "desktop" / "dist"


def main() -> int:
    DIST.mkdir(parents=True, exist_ok=True)
    if shutil.which("pyinstaller") is None:
        print("找不到 PyInstaller，請先執行：pip install pyinstaller")
        return 1
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--windowed",
        "--name",
        "BingoAIPro桌面模擬器",
        "--distpath",
        str(DIST),
        "--exclude-module",
        "backend",
        str(ROOT / "desktop" / "run_simulator.py"),
    ]
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
