# Bingo AI Pro Desktop Professional

Phase Desktop 1 is a Windows-oriented, read-only tkinter/ttk desktop client for the existing Bingo AI Pro backend data.

## Run from source

```powershell
cd C:\Users\ken80297\Documents\GitHub\bingo\bingo-ai-pro
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
pip install -r desktop\requirements-desktop.txt
python -m desktop.app
```

You can also run:

```powershell
python desktop\launcher.py
```

`desktop\launcher.py` adds the project root and `backend` folder to `sys.path`, so no manual `PYTHONPATH` setup is needed.

## Read-only safety

The desktop app installs these defaults before opening the UI:

```powershell
DESKTOP_READ_ONLY=true
DESKTOP_ALLOW_DATABASE_WRITE=false
DESKTOP_ALLOW_LIVE_COLLECTOR=false
DESKTOP_ALLOW_LEARNING_WRITE=false
DESKTOP_ALLOW_PREDICTION_WRITE=false
```

The desktop client does not import `backend/app.py`, does not start APScheduler, does not start collectors, and does not call backend write APIs. Write attempts through desktop adapters return:

```json
{
  "status": "blocked",
  "reason": "desktop_read_only_mode",
  "message": "Desktop read-only mode blocks writes to backend data."
}
```

## Build

```powershell
pyinstaller ^
  --noconfirm ^
  --clean ^
  --windowed ^
  --name "Bingo AI Pro Desktop" ^
  --paths . ^
  --paths backend ^
  desktop\launcher.py
```

The included `desktop\BingoAIProDesktop.spec` excludes `.env`, git metadata, pytest caches, `__pycache__`, and backend data files from the package.

## Verify

```powershell
python -m compileall desktop
pytest -q desktop/tests
pytest -q backend/tests/test_recommendation_engine_output.py backend/tests/test_rule_snapshot.py backend/tests/test_rule_snapshot_store.py backend/tests/test_prediction_service.py backend/tests/test_player_dashboard_previous_prediction.py
```

