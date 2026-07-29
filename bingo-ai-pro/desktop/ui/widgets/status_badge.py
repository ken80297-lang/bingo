from __future__ import annotations

from tkinter import ttk


class StatusBadge(ttk.Label):
    def __init__(self, master, text: str, tone: str = "ok") -> None:
        prefix = {"ok": "OK", "warning": "WARN", "blocked": "BLOCKED", "empty": "EMPTY"}.get(tone, "INFO")
        super().__init__(master, text=f"{prefix}: {text}", style="Panel.TLabel")

