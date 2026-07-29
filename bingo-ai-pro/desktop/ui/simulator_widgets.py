from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Iterable

from desktop.ui.widgets.number_ball import NumberBall


class ScrollPage(ttk.Frame):
    def __init__(self, master) -> None:
        super().__init__(master)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.body = ttk.Frame(self.canvas, padding=8)
        self.body.grid_columnconfigure(0, weight=1)
        self.window_id = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.body.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.window_id, width=event.width))


def title(parent, text: str, row: int = 0) -> ttk.Label:
    label = ttk.Label(parent, text=text, style="Title.TLabel")
    label.grid(row=row, column=0, sticky="w", pady=(0, 10))
    return label


def kv_table(parent, rows: Iterable[tuple[str, object]], row: int, columns: int = 2) -> ttk.Frame:
    frame = ttk.Frame(parent)
    frame.grid(row=row, column=0, sticky="ew", pady=6)
    for index in range(columns * 2):
        frame.grid_columnconfigure(index, weight=1 if index % 2 else 0)
    for index, (key, value) in enumerate(rows):
        r = index // columns
        c = (index % columns) * 2
        ttk.Label(frame, text=str(key), style="Muted.TLabel").grid(row=r, column=c, sticky="w", padx=(0, 6), pady=2)
        ttk.Label(frame, text=_display(value), wraplength=420).grid(row=r, column=c + 1, sticky="w", pady=2)
    return frame


def number_row(parent, numbers: Iterable[int], row: int, states: dict[int, str] | None = None) -> ttk.Frame:
    frame = ttk.Frame(parent)
    frame.grid(row=row, column=0, sticky="w", pady=4)
    states = states or {}
    for index, number in enumerate(numbers):
        NumberBall(frame, f"{int(number):02d}", state=states.get(int(number), "normal"), size=32).grid(row=0, column=index, padx=2)
    return frame


def data_tree(parent, columns: list[tuple[str, str, int]], row: int, height: int = 12) -> ttk.Treeview:
    tree = ttk.Treeview(parent, columns=[key for key, _, _ in columns], show="headings", height=height)
    for key, label, width in columns:
        tree.heading(key, text=label)
        tree.column(key, width=width, anchor="w")
    tree.grid(row=row, column=0, sticky="nsew", pady=6)
    parent.grid_rowconfigure(row, weight=1)
    return tree


def _display(value: object) -> str:
    if value is None or value == "":
        return "尚未驗證"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)
