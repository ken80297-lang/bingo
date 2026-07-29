from __future__ import annotations

import tkinter as tk

from desktop.ui.styles import COLORS


STATE_COLORS = {
    "normal": ("#ffffff", COLORS["line"], COLORS["text"]),
    "recommended": (COLORS["recommend"], "#93c5fd", "#1e3a8a"),
    "hit": (COLORS["hit"], "#86efac", COLORS["success"]),
    "super": (COLORS["super"], "#facc15", "#854d0e"),
    "high_probability": ("#ccfbf1", "#5eead4", "#115e59"),
    "missing": (COLORS["missing"], "#fca5a5", COLORS["danger"]),
}


class NumberBall(tk.Canvas):
    def __init__(self, master: tk.Misc, number: int | str, state: str = "normal", size: int = 34) -> None:
        super().__init__(master, width=size, height=size, highlightthickness=0, bg=COLORS["panel"])
        self.size = size
        self.number = number
        self.state = state
        self.draw()

    def draw(self) -> None:
        fill, outline, text = STATE_COLORS.get(self.state, STATE_COLORS["normal"])
        pad = 2
        self.create_oval(pad, pad, self.size - pad, self.size - pad, fill=fill, outline=outline, width=2)
        self.create_text(self.size / 2, self.size / 2, text=str(self.number), fill=text, font=("Segoe UI Semibold", 10))

