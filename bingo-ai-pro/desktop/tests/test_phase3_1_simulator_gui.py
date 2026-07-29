from pathlib import Path

from desktop.core.replay_worker import ReplayWorker
import desktop.core.simulator_services as services
from desktop.core.simulator_services import dataset_status, export_current_summary, prospective_status
from desktop.ui.navigation import PAGES


MASTER = r"C:\Users\ken80297\Desktop\master_draws.csv"


def test_phase3_1_navigation_page_count_and_names():
    assert [label for _, label in PAGES] == [
        "首頁總覽",
        "歷史模擬",
        "單期模擬",
        "規則分析",
        "統計驗證",
        "前瞻驗證",
        "預測時間軸",
        "報告與輸出",
        "系統設定",
    ]


def test_phase3_1_csv_load_and_overview_data():
    data = dataset_status(MASTER)
    assert data["total_rows"] == 6090
    assert data["valid_rows"] == 6090
    assert data["last_issue"] == "115041412"
    assert data["dataset_hash"]


def test_phase3_1_pending_snapshot_page_data_and_null_metrics():
    status = prospective_status()
    pending = status["pending"]
    current = status["current"]
    assert pending["target_issue"] == "115041413"
    assert pending["missing_top1"] == [64]
    assert pending["missing_top2"] == [64, 46]
    assert pending["missing_top3"] == [64, 46, 35]
    assert pending["snapshot_hash"] == "11910e9d8c06ec746c4a727ecc04bd80b540158ff863dc5fb0beb5c75b496e8f"
    assert current["top1_cumulative_precision"] is None
    assert current["top2_cumulative_precision"] is None
    assert current["top3_cumulative_precision"] is None


def test_phase3_1_replay_worker_cancel():
    worker = ReplayWorker(MASTER, min_history=100)
    events = []
    worker.on_event(lambda event: events.append(event.name))
    worker.start()
    worker.cancel()
    worker.join(10)
    assert "cancelled" in events or "finished" in events


def test_phase3_1_report_export_and_settings_persistence(tmp_path, monkeypatch):
    paths = export_current_summary(tmp_path)
    assert Path(paths["csv"]).exists()
    assert Path(paths["json"]).exists()
    assert Path(paths["report"]).exists()
    monkeypatch.setattr(services, "SETTINGS_PATH", tmp_path / "user_settings.json")
    settings = services.load_user_settings()
    settings["auto_mode"] = False
    services.save_user_settings(settings)
    assert services.load_user_settings()["auto_mode"] is False


def test_phase3_1_startup_batch_files_exist():
    root = Path(__file__).resolve().parents[2]
    assert (root / "啟動桌面模擬器.bat").exists()
    assert (root / "安裝桌面模擬器.bat").exists()
    assert (root / "建立Windows執行檔.bat").exists()
    assert (root / "desktop" / "run_simulator.py").exists()
    assert (root / "desktop" / "build_windows_exe.py").exists()
