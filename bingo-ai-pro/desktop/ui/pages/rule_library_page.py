from __future__ import annotations

from tkinter import ttk

from desktop.core.data_repository import DataRepository
from desktop.core.rule_order import FIXED_RULE_ORDER


class RuleLibraryPage(ttk.Frame):
    def __init__(self, master, repository: DataRepository) -> None:
        super().__init__(master)
        self.repository = repository
        self.grid_columnconfigure(0, weight=1)
        ttk.Label(self, text="Rule Library \u898f\u5247\u5eab", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        columns = ("order", "key", "name", "category")
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=18)
        headings = {"order": "\u9806\u5e8f", "key": "Rule Key", "name": "\u4e2d\u6587\u540d\u7a31", "category": "\u5206\u985e"}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=160, stretch=True)
        self.tree.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        self.refresh()

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        registry = {item.get("key") or item.get("rule_key"): item for item in self.repository.get_rule_registry()}
        for index, (key, zh_name) in enumerate(FIXED_RULE_ORDER, start=1):
            rule = registry.get(key) or {}
            self.tree.insert("", "end", values=(index, key, zh_name, rule.get("family") or rule.get("category") or "-"))

