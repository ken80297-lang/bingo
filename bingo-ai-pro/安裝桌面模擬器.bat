@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m venv .venv-desktop
if errorlevel 1 (
  echo 建立 .venv-desktop 失敗，請確認 Python 已安裝。
  pause
  exit /b 1
)
".venv-desktop\Scripts\python.exe" -m pip install --upgrade pip
".venv-desktop\Scripts\python.exe" -m pip install -r desktop\requirements-desktop.txt
if errorlevel 1 (
  echo 安裝桌面套件失敗。
  pause
  exit /b 1
)
echo 桌面模擬器安裝成功。此腳本不讀取 .env、不安裝 backend server。
pause
