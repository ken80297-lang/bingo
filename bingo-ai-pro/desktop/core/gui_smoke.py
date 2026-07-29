from __future__ import annotations

import threading
import time
import tkinter as tk
from typing import Any

from desktop.core.fingerprint import capture_database_fingerprint, fingerprints_match
from desktop.ui.main_window import MainWindow


def run_gui_smoke(auto_close_ms: int = 1200) -> dict[str, Any]:
    before_threads = {thread.ident for thread in threading.enumerate()}
    before_fp = capture_database_fingerprint()
    try:
        app = MainWindow()
    except tk.TclError as exc:
        after_fp = capture_database_fingerprint()
        return {
            "root_created": False,
            "main_window_created": False,
            "pages_opened": [],
            "all_pages_opened": False,
            "process_can_exit": True,
            "background_threads_left": [],
            "fingerprint_match": fingerprints_match(before_fp, after_fp),
            "environment_blocked": True,
            "error": str(exc),
            "before_fingerprint": before_fp,
            "after_fingerprint": after_fp,
        }
    pages = ["overview", "history", "single", "rules", "statistics", "prospective", "timeline", "reports", "settings"]
    opened = []

    def cycle_pages() -> None:
        for page in pages:
            app.show_page(page)
            app.update_idletasks()
            opened.append(page)
        app.after(auto_close_ms, app.destroy)

    app.after(50, cycle_pages)
    app.mainloop()
    time.sleep(0.1)
    after_threads = {thread.ident for thread in threading.enumerate()}
    after_fp = capture_database_fingerprint()
    return {
        "root_created": True,
        "main_window_created": True,
        "pages_opened": opened,
        "all_pages_opened": opened == pages,
        "process_can_exit": True,
        "background_threads_left": sorted(str(item) for item in (after_threads - before_threads) if item is not None),
        "fingerprint_match": fingerprints_match(before_fp, after_fp),
        "before_fingerprint": before_fp,
        "after_fingerprint": after_fp,
    }
