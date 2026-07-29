from __future__ import annotations

from tkinter import ttk


class MetricCard(ttk.Frame):
    def __init__(self, master, title: str, value: str, detail: str = "") -> None:
        super().__init__(master, style="Panel.TFrame", padding=12)
        ttk.Label(self, text=title, style="Muted.TLabel").pack(anchor="w")
        ttk.Label(self, text=value, style="MetricValue.TLabel").pack(anchor="w", pady=(4, 0))
        if detail:
            ttk.Label(self, text=detail, style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

