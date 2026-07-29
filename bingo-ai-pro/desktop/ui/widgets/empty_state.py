from __future__ import annotations

from tkinter import ttk


class EmptyState(ttk.Frame):
    def __init__(self, master, title: str = "\u6c92\u6709\u8cc7\u6599", detail: str = "\u552f\u8b80\u8cc7\u6599\u5eab\u76ee\u524d\u6c92\u6709\u7b26\u5408\u689d\u4ef6\u7684\u7d00\u9304\u3002") -> None:
        super().__init__(master, style="Panel.TFrame", padding=18)
        ttk.Label(self, text=title, style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(anchor="w")
        ttk.Label(self, text=detail, style="Muted.TLabel").pack(anchor="w", pady=(6, 0))

