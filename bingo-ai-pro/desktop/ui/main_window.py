from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from desktop.config import APP_NAME, DEFAULT_WINDOW_SIZE
from desktop.core.data_repository import DataRepository
from desktop.core.simulator_services import dataset_status, load_user_settings
from desktop.ui.navigation import Navigation
from desktop.ui.pages.history_page import HistoryPage
from desktop.ui.pages.overview_page import OverviewPage
from desktop.ui.pages.prospective_page import ProspectivePage
from desktop.ui.pages.reports_page import ReportsPage
from desktop.ui.pages.rule_analysis_page import RuleAnalysisPage
from desktop.ui.pages.single_issue_page import SingleIssuePage
from desktop.ui.pages.statistics_page import StatisticsPage
from desktop.ui.pages.timeline_page import TimelinePage
from desktop.ui.pages.settings_page import SettingsPage
from desktop.ui.styles import apply_style


class MainWindow(tk.Tk):
    def __init__(self, repository: DataRepository | None = None) -> None:
        super().__init__()
        self.repository = repository or DataRepository()
        self.title(APP_NAME)
        self.geometry(DEFAULT_WINDOW_SIZE)
        self.minsize(1000, 680)
        self.state("normal")
        apply_style(self)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.header = ttk.Frame(self, padding=(12, 8))
        self.header.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.header.grid_columnconfigure(1, weight=1)
        self.header_title = ttk.Label(self.header, text=APP_NAME, style="Title.TLabel")
        self.header_title.grid(row=0, column=0, sticky="w", padx=(0, 14))
        self.header_status = ttk.Label(self.header, text=self._status_text(), style="Muted.TLabel")
        self.header_status.grid(row=0, column=1, sticky="w")
        self.navigation = Navigation(self, self.show_page)
        self.navigation.grid(row=1, column=0, sticky="nsw")
        self.container = ttk.Frame(self, padding=16)
        self.container.grid(row=1, column=1, sticky="nsew")
        self.container.grid_columnconfigure(0, weight=1)
        self.container.grid_rowconfigure(0, weight=1)
        self.current_page: ttk.Frame | None = None
        self.show_page("overview")

    def show_page(self, page: str) -> None:
        if self.current_page is not None:
            self.current_page.destroy()
        classes = {
            "overview": OverviewPage,
            "history": HistoryPage,
            "single": SingleIssuePage,
            "rules": RuleAnalysisPage,
            "statistics": StatisticsPage,
            "prospective": ProspectivePage,
            "timeline": TimelinePage,
            "reports": ReportsPage,
            "settings": SettingsPage,
        }
        page_class = classes.get(page, OverviewPage)
        self.current_page = page_class(self.container, self.repository)
        self.current_page.grid(row=0, column=0, sticky="nsew")
        self.header_status.configure(text=self._status_text())

    def _status_text(self) -> str:
        settings = load_user_settings()
        data = dataset_status(settings["default_csv_path"])
        return f"CSV：{data['path']} ｜ 最新合法期號：{data['last_issue']} ｜ 資料總期數：{data['total_rows']} ｜ 系統狀態：唯讀 ｜ 最後更新：即時"

    def close(self) -> None:
        worker = getattr(self.current_page, "worker", None)
        if worker and worker.is_alive():
            worker.cancel()
            worker.join(1)
        self.destroy()
