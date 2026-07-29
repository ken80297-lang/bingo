from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from desktop.ui.widgets.number_ball import NumberBall


class NumberGrid(ttk.Frame):
    def __init__(self, master: tk.Misc, columns: int = 10) -> None:
        super().__init__(master, style="Panel.TFrame")
        self.columns = columns

    def set_numbers(self, numbers: list[int], *, hits: list[int] | None = None, super_number: int | None = None) -> None:
        for child in self.winfo_children():
            child.destroy()
        hit_set = set(hits or [])
        for index, number in enumerate(numbers or []):
            state = "hit" if number in hit_set else "recommended"
            if super_number == number:
                state = "super"
            ball = NumberBall(self, number, state=state)
            ball.grid(row=index // self.columns, column=index % self.columns, padx=3, pady=3)
        if not numbers:
            ttk.Label(self, text="\u6c92\u6709\u53ef\u986f\u793a\u7684\u865f\u78bc", style="Muted.TLabel").grid(row=0, column=0, padx=8, pady=8)

