from __future__ import annotations

import json
import os

from desktop.core.gui_smoke import run_gui_smoke
from desktop.core.readonly_guard import install_readonly_guard
from desktop.core.simulator_services import setup_logging
from desktop.ui.main_window import MainWindow


def main() -> int:
    setup_logging()
    install_readonly_guard()
    if os.getenv("DESKTOP_SMOKE_TEST") == "1":
        result = run_gui_smoke(auto_close_ms=int(os.getenv("DESKTOP_SMOKE_AUTOCLOSE_MS", "300")))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("all_pages_opened") and result.get("fingerprint_match") else 1
    app = MainWindow()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
