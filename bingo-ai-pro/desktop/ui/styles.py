from __future__ import annotations

import tkinter as tk
from tkinter import ttk


COLORS = {
    "bg": "#f5f7fb",
    "panel": "#ffffff",
    "line": "#d7dce5",
    "text": "#172033",
    "muted": "#627084",
    "accent": "#0f766e",
    "accent_alt": "#1d4ed8",
    "success": "#15803d",
    "warning": "#b45309",
    "danger": "#b91c1c",
    "hit": "#dcfce7",
    "missing": "#fee2e2",
    "recommend": "#dbeafe",
    "super": "#fef3c7",
}


def apply_style(root: tk.Tk) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(bg=COLORS["bg"])
    style.configure(".", font=("Segoe UI", 10), foreground=COLORS["text"])
    style.configure("TFrame", background=COLORS["bg"])
    style.configure("Panel.TFrame", background=COLORS["panel"], relief="solid", borderwidth=1)
    style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"])
    style.configure("Panel.TLabel", background=COLORS["panel"], foreground=COLORS["text"])
    style.configure("Muted.TLabel", background=COLORS["panel"], foreground=COLORS["muted"])
    style.configure("Title.TLabel", font=("Segoe UI Semibold", 16), background=COLORS["bg"])
    style.configure("MetricValue.TLabel", font=("Segoe UI Semibold", 18), background=COLORS["panel"])
    style.configure("Accent.TButton", background=COLORS["accent"], foreground="#ffffff")
    style.configure("Treeview", rowheight=28, fieldbackground=COLORS["panel"])
    style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10))

